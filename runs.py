"""Server-side chat runs.

A run is one reply: the model -> tool -> model loop for a single chat, on its own thread.
Runs keep going when the window switches chats or closes, several chats can run at once,
and approvals wait here until the user answers in any open window.

Each chat has a Channel of numbered events. A window subscribes over Server-Sent Events,
gets a full "snapshot" first, then live events:
  snapshot  {chat, status, approval}       full state (also sent when a run starts)
  message   {index, message}               a message was added or replaced
  delta     {index, content, thinking}     streamed text appended to a message
  status    {status, model}                "running" | "waiting" | "idle"
  approval  {approval}                     a write/model change needs the user's answer
  approval_done {id}                       that question was answered or withdrawn
  context   {...}                          what the context budget did for this request
  usage     {session, today, context}      token counts after a model response
  untrusted {untrusted}                    the chat read web content (see capabilities.policy)
  balance_wait {balance, minimumUsd}       the reply paused because the Ollama balance is too low

Balance guard: before every model request a reply checks the Ollama balance (cached, shared).
At or below the minimum in Settings, a reply that has already worked makes one last request
without tools to summarize its progress ("wrap-up"), then pauses with status "paused". It
re-checks about once a minute (or at once on "Check now") and continues when the balance is
above the minimum again. If the balance can't be read, replies keep working.

Subtasks: a reply can start child chats (start_subtask). Each runs as its own Run; the
parent starts all subtasks requested in one step, then waits for them and returns each
child's final answer as that tool call's result. Stopping a parent stops its children.

Messages between a parent and its subtasks:
  message_parent (child)    a note to the parent, or a question (wait_for_reply) that pauses
                            the child until the parent answers
  message_subtask (parent)  an answer to a child's question, or a follow-up the child reads
                            before its next step; a finished child continues with it as a new reply
  wait_subtasks (parent)    wait for results and messages
A parent waiting for its subtasks stops waiting as soon as one asks a question, so the model can
answer it. A reply that ends while its subtasks are still running or unread gets a reminder
(at most SUBTASK_REMINDERS times); subtasks still running when it ends are stopped. Messages
from a subtask that read web content make the parent untrusted, and the other way round.
The engine is created by server.py with the server module as `srv`, which provides
storage, tools, approvals and the Ollama Cloud connection.

Stop: cancel() sets the run's Stop signal. Model requests, approvals, the balance pause, commands
and Git calls watch it and end at once. If the reply's thread still hasn't finished STOP_GRACE
seconds later (a step that can't be interrupted), the engine ends the reply anyway (abandon()):
the stuck step is marked as stopped, the chat is saved and free again, and anything that thread
does afterwards is ignored.
"""

import collections
import copy
import itertools
import json
import os
import secrets
import threading
import time
import urllib.error

import capabilities
import commands
import context_budget
import gittools
import projects
import undo
import worktrees
import skills
import web

ACTION_TITLES = {"create_folder": "Create a folder?", "copy_path": "Copy this item?", "move_path": "Move or rename this item?",
                 "delete_path": "Move this item to the trash?", "restore_path": "Restore this item from the trash?"}
MANUAL_WHY = "This chat is in Manual mode, so file operations ask first. Change the mode in Session settings."

MAX_STEER_CHARS = 100_000  # a message typed while a reply runs
MAX_STEPS = 100  # model requests per reply when the config has no maxSteps (Settings > Usage & limits; 0 = no limit)
APPROVAL_WAIT = 1800          # seconds an approval waits before it counts as denied
EVENT_BUFFER = 5000
MAX_IMAGES = 10
MAX_IMAGE_CHARS = 20_000_000  # base64 characters per image
MAX_SUBTASKS_PER_REPLY = 6
MAX_DOCUMENTS = 10
CHANGING_TOOLS = {"write_file", "edit_file", "create_folder", "copy_path", "move_path", "delete_path", "restore_path"}
SUBTASK_RESULT_CHARS = 20_000
MAX_SUBTASK_MESSAGE = 8_000   # characters in one message between a parent and a subtask
MAX_MESSAGES = 30             # messages one reply can send to its parent or subtasks
QUESTION_WAIT = 1800          # seconds a subtask waits for the parent's answer
WAIT_SUBTASKS = 600           # default and
MAX_WAIT_SUBTASKS = 1800      # longest wait_subtasks call, in seconds
SUBTASK_REMINDERS = 2         # times a reply is reminded of unread subtasks before it may end
HISTORY = 30                  # finished replies kept for the Activity view
BALANCE_RECHECK = 60          # seconds between balance checks while paused
STOP_GRACE = 3                # seconds after Stop before a reply that is still busy is ended anyway
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504, 529}  # Ollama errors that usually pass: the step is tried again
RETRY_DELAYS = (3, 10, 30)    # seconds before each retry
MANUAL_KEEP_TOKENS = 4_000    # "Compact" keeps about this much of the most recent conversation word for word
IDLE_FORGET = 600             # seconds after a reply ends before the engine forgets it (and its live events)
SESSION_FIELDS = ("title", "folders", "capabilities", "skills", "projectId", "untrusted", "webUrls")  # changed by the user, not the reply
STUCK_STEP = "Stopped: this step didn't finish, so the reply was ended when you pressed Stop."
WRAP_UP_NOTE = ("The user's Ollama balance is nearly used up, so this work must pause now. Do not call any tools. "
                "Write a short summary: what you have done so far, what is left to do, and anything the user should know. "
                "The work will continue later from this point.")
SUMMARY_INSTRUCTIONS = (
    "You compress a conversation between a user and an AI assistant so the assistant can continue the work with only "
    "your summary instead of the older messages. Write concise Markdown bullet points under these headings: Goal, "
    "Facts the user gave (in the order they were given), Decisions, Work done (in order), Open items. Keep every detail "
    "the assistant may need later: the user's requests and preferences, names, numbers, dates, file paths, code "
    "identifiers, commands run and their results, errors, and anything promised or still to do. Quote short exact "
    "values instead of paraphrasing them, and keep the order in which things happened. Leave out small talk and text "
    "that carries no information. If a summary so far is given, merge it with the new part into one updated summary, "
    "keeping its order. Never invent anything. At most about 800 words.")
PLAN_NOTE = (
    "Plan mode: the user wants a plan before anything changes. Investigate with the read-only tools you have, then "
    "reply with a clear, numbered step-by-step plan (files to change, commands to run, risks, and questions for the "
    "user). Do not make any changes. The user will review the plan and then ask you to carry it out.")


class _Done:
    alive = False
    question = None
    status = "idle"


_DONE = _Done()  # stands in for a subtask that has no running reply


class RunError(Exception):
    """A run could not start (bad input, or this chat is already running)."""


class Denied(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


class Channel:
    """Numbered events for one chat, with waiting subscribers."""

    def __init__(self):
        self.cond = threading.Condition()
        self.events = []
        self.seq = 0
        self.subscribers = 0       # open windows following this chat (see server._events)
        self.last = time.time()    # last event, for forgetting idle chats (Engine.prune)

    def publish(self, event):
        with self.cond:
            self.last = time.time()
            self.seq += 1
            self.events.append((self.seq, event))
            if len(self.events) > EVENT_BUFFER:
                del self.events[:len(self.events) - EVENT_BUFFER]
            self.cond.notify_all()
            return self.seq

    def since(self, after, timeout):
        """Events newer than `after`. `gap` is True when some were already dropped from the buffer."""
        with self.cond:
            if self.seq <= after:
                self.cond.wait(timeout)
            gap = bool(self.events) and self.events[0][0] > after + 1 and after < self.seq
            return [e for e in self.events if e[0] > after], gap


class Engine:
    def __init__(self, srv):
        self.srv = srv
        self.lock = threading.Lock()
        self.runs = {}
        self.channels = {}
        self.history = collections.deque(maxlen=HISTORY)
        self.balance_event = threading.Event()

    def channel(self, chat_id):
        with self.lock:
            return self.channels.setdefault(chat_id, Channel())

    def active(self, chat_id):
        run = self.runs.get(chat_id)
        return run if run and run.alive else None

    def busy(self, chat_id):
        return self.active(chat_id) is not None

    def any_active(self):
        return any(r.alive for r in list(self.runs.values()))

    def statuses(self):
        """Every running reply's status: running, waiting (for you), paused (low balance) or wrapping (its last
        request before pausing)."""
        self.prune()
        return {cid: ("wrapping" if r.wrapping_up and r.status == "running" else r.status)
                for cid, r in list(self.runs.items()) if r.alive}

    def snapshot(self, chat_id):
        """(snapshot event, channel seq it is current as of)."""
        ch = self.channel(chat_id)
        run = self.active(chat_id)
        if run:
            with run.lock:
                return run.snapshot_event(), ch.seq
        with ch.cond:
            seq = ch.seq
        try:
            chat = self.srv.load_chat(chat_id)
        except (OSError, ValueError):
            return {"type": "gone"}, seq
        chat.pop("_path", None)
        return {"type": "snapshot", "chat": chat, "status": "idle", "approval": None}, seq

    def start(self, chat_id, options, parent=None):
        limit = int(self.srv.load_config().get("maxConcurrentRuns") or 4)
        with self.lock:
            if self.active(chat_id):
                raise RunError("A reply is already running in this session.")
            running = sum(1 for r in self.runs.values() if r.alive)
            if running >= limit:
                raise RunError("%d replies are already running, which is the limit. Wait for one to finish, or raise "
                               "\"Replies at the same time\" in Settings." % running)
            run = Run(self, chat_id, options, parent)  # validates input and saves the user's message
            self.runs[chat_id] = run
        run.thread.start()
        return run

    def steer(self, chat_id, text):
        """A message the user sent while a reply runs in this chat: the reply reads it before its next step.
        Returns True when queued, False when no reply takes it (none is running, or it is just finishing)."""
        text = str(text or "").strip()
        if not text:
            raise RunError("The message is empty.")
        if len(text) > MAX_STEER_CHARS:
            raise RunError("A message sent during a reply can be at most %d characters." % MAX_STEER_CHARS)
        run = self.active(chat_id)
        if run is None:
            return False
        with run.lock:
            if run.inbox_closed:
                return False
            run.inbox.append({"user": text})
        with run.news:  # wakes a reply that is waiting for its subtasks
            run.news.notify_all()
        run.publish({"type": "queued", "count": sum(1 for m in run.inbox if isinstance(m, dict))})
        return True

    def running_count(self):
        return sum(1 for r in list(self.runs.values()) if r.alive)

    def prune(self):
        """Forget replies that ended a while ago, and the live events of chats no window follows, so memory
        doesn't grow with every chat opened since the app started. A finished subtask is kept while its parent runs."""
        now = time.time()
        with self.lock:
            for cid, r in list(self.runs.items()):
                if r.alive or not r.ended or now - r.ended < IDLE_FORGET or (r.parent is not None and r.parent.alive):
                    continue
                del self.runs[cid]
            for cid, ch in list(self.channels.items()):
                if cid not in self.runs and ch.subscribers <= 0 and now - ch.last > IDLE_FORGET:
                    del self.channels[cid]

    def stop_all(self, wait=5):
        """Stop every reply at once (quitting), then wait up to `wait` seconds in total for them to end."""
        stopping = [r for r in list(self.runs.values()) if r.alive]
        for r in stopping:
            r.cancel()
        end = time.time() + wait
        for r in stopping:
            r.thread.join(max(0, end - time.time()))
        for r in stopping:
            if r.thread.is_alive():
                r.abandon()
        return len(stopping)

    def balance_changed(self):
        """Wake paused replies so they re-check the balance now."""
        self.balance_event.set()
        self.balance_event = threading.Event()

    def finished(self, run):
        self.history.appendleft(run.summary())

    def activity(self):
        active = [r.summary() for r in list(self.runs.values()) if r.alive]
        return {"active": sorted(active, key=lambda a: a["started"]), "recent": list(self.history)}

    def stop(self, chat_id, wait=0):
        run = self.active(chat_id)
        if run:
            run.cancel()
            if wait:
                run.thread.join(wait)
            threading.Thread(target=self._end_if_stuck, args=(run,), daemon=True, name="stop-%s" % chat_id).start()
        return bool(run)

    def _end_if_stuck(self, run):
        run.thread.join(STOP_GRACE)
        if run.thread.is_alive():
            run.abandon()

    def decide(self, chat_id, approval_id, approve):
        run = self.active(chat_id)
        return bool(run and run.decide(approval_id, approve))


class Run:
    def __init__(self, engine, chat_id, options, parent=None):
        self.engine, self.srv, self.chat_id = engine, engine.srv, chat_id
        self.parent = parent
        self.children = []
        self.started = time.time()
        self.ended = None
        self.steps = 0
        self.tokens = 0
        self.outcome = None
        self.pause = None          # {"balance", "minimumUsd"} while waiting for the balance
        self.wrapping_up = False
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.status = "running"
        self.alive = True
        self.finishing = False     # the reply is being ended (normally, or by abandon())
        self.detached = False      # ended by abandon(): this run's thread may still be stuck; ignore what it does
        self.news = threading.Condition()  # subtasks' messages and finishes wake this run when it waits for them
        self.updates = []          # messages from subtasks the model hasn't seen yet
        self.pending = {}          # subtask chat id -> title: subtasks whose final result the model hasn't seen
        self.inbox = []            # messages delivered before this run's next model step: text from the parent chat,
                                   # or {"user": text} the user sent while this reply was running (Engine.steer)
        self.inbox_closed = False  # this reply is ending: new messages from the parent start a new reply instead
        self.question = None       # this subtask's question while it waits for the parent's answer
        self.sent = 0              # messages this reply sent to its parent or subtasks
        self.reminders = 0
        self.paused_seen = set()   # subtasks whose low-balance pause the model has been told about
        self.approval = None
        self.response = None
        self.use_tools = True
        self.plan = bool(options.get("plan"))      # plan mode: read-only tools and a plan as the answer
        self.compact_only = bool(options.get("compact"))  # "Compact": summarize older messages now, no reply
        self.compaction_failed = False             # summarize at most once per failed attempt per reply
        self.run_id = secrets.token_hex(6)         # tags file versions this reply kept, for "Undo this reply"
        think = options.get("think")
        self.think = think if think in (True, False, "low", "medium", "high") else None
        self.chat = self._prepare(options)
        self.thread = threading.Thread(target=self._main, daemon=True, name="run-%s" % chat_id)

    # ------------------------------------------------------------ setup
    def _prepare(self, options):
        srv = self.srv
        with srv.chat_lock(self.chat_id):
            try:
                chat = srv.load_chat(self.chat_id)
            except FileNotFoundError:
                raise RunError("This chat doesn't exist any more.")
            chat.pop("_path", None)
            msgs = chat.setdefault("messages", [])
            if options.get("regenerate"):
                while msgs and msgs[-1].get("role") != "user":
                    msgs.pop()
                if not msgs:
                    raise RunError("There is nothing to regenerate.")
            elif options.get("compact"):
                if sum(1 for m in msgs if m.get("role") == "user" and not m.get("auto")) < 2:
                    raise RunError("There is nothing to compact yet: this chat has only one message from you.")
            else:
                at = options.get("truncateAt")
                if at is not None:  # edit and resend: drop that user message and everything after it
                    if not isinstance(at, int) or not 0 <= at < len(msgs) or msgs[at].get("role") != "user":
                        raise RunError("That message can't be edited.")
                    del msgs[at:]
                text = options.get("text") or ""
                images = options.get("images") or []
                docs = options.get("documents") or []
                if not isinstance(text, str) or not isinstance(images, list) or not isinstance(docs, list):
                    raise RunError("Invalid message.")
                if len(images) > MAX_IMAGES or not all(isinstance(i, str) and 0 < len(i) <= MAX_IMAGE_CHARS for i in images):
                    raise RunError("Attach at most %d images." % MAX_IMAGES)
                if len(docs) > MAX_DOCUMENTS or not all(isinstance(d, dict) and isinstance(d.get("text"), str) and len(d["text"]) <= 300_000
                                                         for d in docs):
                    raise RunError("Attach at most %d documents." % MAX_DOCUMENTS)
                if not text.strip() and not images and not docs:
                    raise RunError("The message is empty.")
                message = {"role": "user", "content": text}
                if images:
                    message["images"] = images
                if docs:
                    message["attachments"] = [{"name": str(d.get("name") or "file")[:200], "text": d["text"],
                                               "chars": len(d["text"]), "note": str(d.get("note") or "")[:300]} for d in docs]
                if options.get("plan"):
                    message["plan"] = True
                if options.get("fromParent"):  # a message from the parent chat continues a finished subtask
                    message["auto"] = message["fromParent"] = True
                msgs.append(message)
            if not chat.get("model"):
                raise RunError("Pick a model for this chat first.")
            c = chat.get("compaction")  # a summary of messages that an edit or regenerate removed is stale
            if isinstance(c, dict) and (not isinstance(c.get("upTo"), int) or c["upTo"] >= len(msgs)):
                chat.pop("compaction", None)
            chat["updated"] = now_ms()
            srv.save_chat(self.chat_id, chat)
        return chat

    # ------------------------------------------------------------ events and state
    def public_approval(self):
        a = self.approval
        return {"id": a["id"], "kind": a["kind"], "data": a["data"]} if a else None

    def snapshot_event(self):
        chat = copy.deepcopy(self.chat)
        try:  # the name, folders and permissions the user changed while this reply runs live on disk
            disk = self.srv.load_chat(self.chat_id)
            for key in SESSION_FIELDS:
                if key in disk:
                    chat[key] = disk[key]
                else:
                    chat.pop(key, None)
        except (OSError, ValueError):
            pass
        return {"type": "snapshot", "chat": chat, "status": self.status, "approval": self.public_approval(),
                "pause": copy.deepcopy(self.pause), "started": int(self.started * 1000)}

    def refresh_session(self, disk):
        """The user renamed the chat or changed its settings while this reply runs: keep this copy current and
        tell open windows."""
        with self.lock:
            changed = {}
            for key in SESSION_FIELDS:
                if key in disk:
                    self.chat[key] = changed[key] = copy.deepcopy(disk[key])
                elif key in self.chat:
                    self.chat.pop(key, None)
                    changed[key] = None
            self.publish({"type": "session", "chat": changed})

    def publish(self, event):
        if not self.detached:
            self.engine.channel(self.chat_id).publish(event)

    def add(self, message):
        with self.lock:
            if self.detached:  # a stuck step returned after Stop ended the reply: keep it out of the chat
                return len(self.chat["messages"]) - 1
            self.chat["messages"].append(message)
            idx = len(self.chat["messages"]) - 1
            self.publish({"type": "message", "index": idx, "message": copy.deepcopy(message)})
            return idx

    def update(self, idx, **fields):
        """Set fields on a message; a value of None removes that field."""
        with self.lock:
            if self.detached:
                return
            m = self.chat["messages"][idx]
            for key, value in fields.items():
                if value is None:
                    m.pop(key, None)
                else:
                    m[key] = value
            self.publish({"type": "message", "index": idx, "message": copy.deepcopy(m)})

    def delta(self, idx, content, thinking):
        with self.lock:
            if self.detached:
                return
            m = self.chat["messages"][idx]
            m["content"] = (m.get("content") or "") + content
            m["thinking"] = (m.get("thinking") or "") + thinking
            self.publish({"type": "delta", "index": idx, "content": content, "thinking": thinking})

    def set_status(self, status):
        with self.lock:
            if self.detached:
                return
            self.status = status
            self.publish({"type": "status", "status": status, "model": self.chat.get("model"), "started": int(self.started * 1000)})

    def save(self):
        """Write messages, model and usage; folders/permissions/title keep what is on disk."""
        if not self.detached:
            self._write()

    def _write(self):
        srv = self.srv
        with self.lock:
            msgs = copy.deepcopy(self.chat["messages"])
            model, usage, context = self.chat.get("model"), copy.deepcopy(self.chat.get("usage")), copy.deepcopy(self.chat.get("context"))
            compaction = copy.deepcopy(self.chat.get("compaction"))
        with srv.chat_lock(self.chat_id):
            try:
                disk = srv.load_chat(self.chat_id)
            except (OSError, ValueError):
                return  # deleted meanwhile
            disk["messages"], disk["model"], disk["updated"] = msgs, model, now_ms()
            if usage: disk["usage"] = usage
            if context: disk["context"] = context
            if compaction: disk["compaction"] = compaction
            else: disk.pop("compaction", None)
            srv.save_chat(self.chat_id, disk)

    def summary(self):
        with self.lock:
            status = self.status if self.alive else (self.outcome or "done")
            if self.alive and status == "running":
                status = "wrapping" if self.wrapping_up else "asking" if self.question is not None else status
            return {"chatId": self.chat_id, "title": self.chat.get("title", ""), "parentId": self.chat.get("parentId"),
                    "model": self.chat.get("model", ""), "status": status,
                    "approval": self.approval["kind"] if self.approval else None, "started": int(self.started * 1000),
                    "pause": copy.deepcopy(self.pause),
                    "seconds": int((self.ended or time.time()) - self.started), "steps": self.steps, "tokens": self.tokens,
                    "children": list(self.children)}

    # ------------------------------------------------------------ control from other threads
    def cancel(self):
        self.cancelled.set()
        for child in list(self.children):  # stopping a reply stops its subtasks
            self.engine.stop(child)
        with self.lock:
            a, r = self.approval, self.response
        if a:
            a["event"].set()  # result stays False -> denied
        self.engine.balance_changed()  # a paused reply notices the stop at once (others just re-check once)
        if r is not None:
            try: r.close()  # unblocks the streaming read
            except Exception: pass  # noqa: BLE001

    def decide(self, approval_id, approve):
        with self.lock:
            a = self.approval
            if not a or a["id"] != approval_id or a["event"].is_set():
                return False
            a["result"] = bool(approve)
            a["event"].set()
            return True

    def wait_approval(self, kind, data):
        a = {"id": secrets.token_hex(8), "kind": kind, "data": data, "event": threading.Event(), "result": False}
        with self.lock:
            self.approval = a
            self.publish({"type": "approval", "approval": self.public_approval()})
        self.set_status("waiting")
        a["event"].wait(APPROVAL_WAIT)
        with self.lock:
            self.approval = None
            self.publish({"type": "approval_done", "id": a["id"]})
        if not self.cancelled.is_set():
            self.set_status("running")
        return a["result"] and not self.cancelled.is_set()

    # ------------------------------------------------------------ the loop
    def _main(self):
        srv = self.srv
        try:
            self.publish(self.snapshot_event())
            if not srv.api_key(srv.load_config()):
                self.add({"role": "assistant", "content": "", "model": self.chat.get("model", ""),
                          "error": "Add your Ollama API key in Settings to use Ollama Cloud."})
                return
            if self.compact_only:
                self.manual_compact()
                return
            cfg = srv.load_config(); limit = int(cfg.get("maxSteps", MAX_STEPS) or 0)
            for step in (range(limit) if limit else itertools.count()):
                if self.cancelled.is_set() or not self.balance_gate():
                    break
                self.read_inbox()
                idx = self.add({"role": "assistant", "content": "", "thinking": "", "model": self.chat["model"], "_live": True})
                self.stream_step(idx)
                reply = self.chat["messages"][idx]
                if self.cancelled.is_set() or reply.get("error"):
                    break
                if not reply.get("tool_calls"):
                    if self.remind_subtasks():
                        continue
                    with self.lock:  # a message from the parent that came in during this step gets one more step
                        if self.inbox:
                            continue
                        self.inbox_closed = True
                    break
                started = []  # subtasks requested in this step run at the same time
                for call in reply["tool_calls"]:
                    if self.cancelled.is_set():
                        break
                    name, args = parse_call(call)
                    if name == "start_subtask":
                        launched = self.start_subtask(args)
                        if launched:
                            started.append(launched)
                    else:
                        self.run_tool_call(call)
                if started:
                    self.finish_subtasks(started)
                self.save()
            else:
                self.add({"role": "assistant", "content": "", "model": self.chat["model"],
                          "error": "Paused after %d steps, the limit for one reply (Settings > Usage & limits). "
                                   "Continue to carry on where it stopped." % limit, "stepLimit": True})
        except Exception as e:  # noqa: BLE001 - never leave a run stuck in "running"
            self.add({"role": "assistant", "content": "", "model": self.chat.get("model", ""), "error": "Unexpected error: %s" % e})
        finally:
            with self.lock:
                ending = not self.finishing  # False when Stop already ended this reply (abandon)
                self.finishing = True
                self.inbox_closed = True
            if ending:
                self.stop_own_subtasks()
                self._finish()

    def abandon(self):
        """End a stopped reply whose thread is stuck in a step that can't be interrupted. The step is marked as
        stopped and the chat is saved and freed; whatever the stuck thread does later is ignored."""
        with self.lock:
            if self.finishing or not self.alive:
                return False
            self.finishing = True
            for m in self.chat["messages"]:
                if m.get("_running"):
                    m.update(content=STUCK_STEP, isError=True)
        self._finish(detach=True)
        return True

    def _finish(self, detach=False):
        """Clear live flags, save, and mark the reply finished (status "idle")."""
        with self.lock:
            for m in self.chat["messages"]:
                for flag in ("_live", "_running", "_approval", "_modelApproval", "_actionApproval"):
                    m.pop(flag, None)
            last = self.chat["messages"][-1] if self.chat["messages"] else {}
            self.outcome = "stopped" if self.cancelled.is_set() else "error" if last.get("error") else "done"
            self.ended = time.time()
            self.approval = None
            if detach:
                self.detached = True  # from here on, the stuck thread's writes are ignored
        channel = self.engine.channel(self.chat_id)
        if detach:
            with self.lock:
                channel.publish(self.snapshot_event())  # windows drop the stuck step's live state
        try:
            self._write()  # before the run counts as finished, so a new reply can't start from an older file
        finally:
            with self.lock:
                self.alive = False
                self.status = "idle"
            self.engine.finished(self)
            channel.publish({"type": "status", "status": "idle", "model": self.chat.get("model")})
            if self.parent is not None:  # a parent waiting for this subtask wakes up
                with self.parent.news:
                    self.parent.news.notify_all()

    def build_request(self, model):
        """The request for one model step. Its start (instructions, tools, summary, history) stays the same
        from step to step and turn to turn, so Ollama can bill it at the cached-input rate; anything that
        changes per request (the wrap-up note) goes at the end."""
        srv = self.srv
        fcfg = srv.saved_tool_config(self.chat_id)  # fresh from disk: permissions/skills may change mid-run
        specs, where = srv.tool_specs(fcfg, plan=self.plan) if self.use_tools else ([], "")
        if self.parent is None:  # message_parent only reaches a parent whose reply started this one
            specs = [x for x in specs if x["function"]["name"] != "message_parent"]
        names = {x["function"]["name"] for x in specs}
        system = [srv.instructions_prompt(fcfg), skills.prompt(srv.skills_root(), fcfg.get("skills") or [],
                                                                "read_skill_file" in names, "run_command" in names)]
        if specs:
            system.append(srv.tool_system_note(fcfg, specs, where))
        elif fcfg.get("parentId"):  # no tools (a wrap-up, or a model without tools): it's still a helper
            system.append("You are a subtask helper: do the task you were given and finish with a complete, self-contained answer.")
        if self.plan:
            system.append(PLAN_NOTE)
        note = "\n\n".join(x for x in system if x)
        overhead = (len(note) + len(json.dumps(specs))) // context_budget.CHARS_PER_TOKEN if (note or specs) else 0
        context_tokens = srv.model_info(model).get("context_length")
        budget_name = srv.load_config().get("contextBudget")
        for attempt in range(2):
            with self.lock:
                history = [m for m in copy.deepcopy(self.chat["messages"]) if not m.get("_live")]
                compaction = copy.deepcopy(self.chat.get("compaction"))
            msgs, info = context_budget.build(history, context_tokens, budget_name, overhead, compaction)
            if not info["needsCompaction"] or attempt or self.compaction_failed:
                break
            self.compact(model, history, compaction, context_tokens, budget_name, overhead)
        body = {"model": model, "messages": msgs, "stream": True}
        if self.think is not None:
            body["think"] = self.think
        if specs:
            body["tools"] = specs
        if note:
            if msgs and msgs[0]["role"] == "system" and not msgs[0]["content"].startswith(context_budget.SUMMARY_PREFIX):
                msgs[0] = dict(msgs[0], content=note + "\n\n" + msgs[0]["content"])
            else:
                msgs.insert(0, {"role": "system", "content": note})
        if self.wrapping_up:
            msgs.append({"role": "system", "content": WRAP_UP_NOTE})
        self.publish(dict(info, type="context"))
        return body, info

    # ------------------------------------------------------------ compaction (see context_budget.py)
    def compact(self, model, history, compaction, context_tokens, budget_name, overhead, keep_budget=None):
        """Summarize older turns with the chat's model so the request fits the context budget.

        Big histories are folded chunk by chunk: each request gets the summary so far plus the next
        chunk. On any failure the request falls back to leaving out the oldest turns."""
        srv = self.srv
        plan = context_budget.plan_compaction(history, compaction, context_tokens, budget_name, overhead, keep_budget=keep_budget)
        if not plan:
            self.compaction_failed = True
            return
        start, split = plan
        summary = (compaction or {}).get("summary") if context_budget.valid_compaction(history, compaction) else ""
        text = context_budget.transcript(history[start:split])
        # One request when possible: the summarizer is limited by the model's window, not the user's budget.
        window = int(context_tokens or context_budget.DEFAULT_CONTEXT)
        chunk_chars = max(8_000, int(min(window, 200_000) * context_budget.CHARS_PER_TOKEN * 0.6))
        self.publish({"type": "compacting", "active": True})
        try:
            for at in range(0, max(1, len(text)), chunk_chars):
                if self.cancelled.is_set():
                    raise RunError("stopped")
                summary = self.summarize(model, summary, text[at:at + chunk_chars])
            record = {"upTo": split, "summary": summary, "at": now_ms(), "model": model, "messages": split}
            with self.lock:
                self.chat["compaction"] = record
                self.publish({"type": "compaction", "compaction": copy.deepcopy(record)})
            self.save()
        except Exception:  # noqa: BLE001 - the request still goes out, with the oldest turns left out
            self.compaction_failed = True
        finally:
            self.publish({"type": "compacting", "active": False})

    def manual_compact(self):
        """The user pressed Compact: summarize everything but the latest messages now, so later requests are smaller."""
        model = self.chat["model"]
        with self.lock:
            history = [m for m in copy.deepcopy(self.chat["messages"]) if not m.get("_live")]
            compaction = copy.deepcopy(self.chat.get("compaction"))
        before = (context_budget.valid_compaction(history, compaction) or {}).get("upTo", 0)
        context_tokens = self.srv.model_info(model).get("context_length")
        budget_name = self.srv.load_config().get("contextBudget")
        possible = context_budget.plan_compaction(history, compaction, context_tokens, budget_name, 0, keep_budget=MANUAL_KEEP_TOKENS)
        if not possible:
            self.publish({"type": "compact_done", "ok": False, "error": "Nothing older to summarize yet: only the latest messages are left."})
            return
        self.compaction_failed = False
        self.compact(model, history, compaction, context_tokens, budget_name, 0, keep_budget=MANUAL_KEEP_TOKENS)
        after = (self.chat.get("compaction") or {}).get("upTo", 0)
        if after > before and not self.compaction_failed:
            self.publish({"type": "compact_done", "ok": True, "messages": after})
        else:
            self.publish({"type": "compact_done", "ok": False, "error": "Couldn't summarize this chat just now. Try again in a moment."})

    def summarize(self, model, previous, chunk):
        srv = self.srv
        prompt = (("Summary so far:\n" + previous + "\n\n") if previous else "") + "Conversation to add to the summary:\n\n" + chunk
        body = {"model": model, "stream": False, "messages": [{"role": "system", "content": SUMMARY_INSTRUCTIONS},
                                                               {"role": "user", "content": prompt}]}
        caps = srv.model_info(model).get("capabilities") or []
        if "thinking" in caps:  # summarizing needs no long reasoning, and thinking tokens bill as output
            body["think"] = "low" if model.startswith("gpt-oss") else False
        with srv.upstream("/api/chat", body, timeout=300) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.count_usage(int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0), int(data.get("prompt_eval_cached_count") or 0))
        out = ((data.get("message") or {}).get("content") or "").strip()
        if not out:
            raise RunError("empty summary")
        return out[:context_budget.SUMMARY_CHARS]

    def stream_step(self, idx):
        srv = self.srv
        model = self.chat["model"]
        with self.lock:
            reason = srv.limit_reached(self.chat)
        if reason:
            self.update(idx, error=reason, _live=None, thinking=None)
            return
        self.steps += 1
        attempt = 0
        while True:
            body, info = self.build_request(model)
            try:
                r = srv.upstream("/api/chat", body)
            except urllib.error.HTTPError as e:
                msg = srv.http_error_text(e)
                if self.use_tools and "does not support tools" in msg.lower():
                    self.use_tools = False  # retry this step without any tools
                    self.update(idx, notice="This model doesn't support tools, so folder access is off for this reply.")
                    continue
                if e.code in RETRY_CODES and self.retry_wait(idx, attempt, "Ollama Cloud answered %d (%s)" % (e.code, msg[:120])):
                    attempt += 1
                    continue
                self.update(idx, error=msg, _live=None, thinking=None, notice=None)
                return
            except Exception as e:  # noqa: BLE001
                if not self.cancelled.is_set() and self.retry_wait(idx, attempt, "Couldn't reach Ollama Cloud (%s)" % e):
                    attempt += 1
                    continue
                self.update(idx, error="Could not reach Ollama Cloud: %s" % e, _live=None, thinking=None, notice=None)
                return
            tool_calls, final, error, broken = self.read_stream(idx, r)
            if broken and not self.cancelled.is_set() and not tool_calls:
                # The connection ended before the reply finished: start this step again from the beginning.
                if self.retry_wait(idx, attempt, "The connection to Ollama ended before the reply finished", reset=True):
                    attempt += 1
                    continue
                error = "The connection to Ollama ended before the reply finished. Send a message to continue."
            break
        if attempt:
            self.update(idx, notice=None)
        fields = {"_live": None}
        stats = []
        if final:
            stats.append(self.record_usage(final, info))
        if self.cancelled.is_set():
            stats.append("stopped")
        if error:
            fields["error"] = error
        if tool_calls and not self.cancelled.is_set():
            fields["tool_calls"] = tool_calls
        if stats and any(stats):
            fields["stats"] = " · ".join(s for s in stats if s)
        with self.lock:
            if not self.chat["messages"][idx].get("thinking"):
                fields["thinking"] = None
        self.update(idx, **fields)

    def read_stream(self, idx, r):
        """Stream one model response into message idx. Returns (tool calls, final chunk, error, broken):
        broken means the connection ended without Ollama's final "done" chunk."""
        with self.lock:
            self.response = r
        tool_calls, final, error = [], None, None
        try:
            with r:
                while not self.cancelled.is_set():
                    line = r.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get("error"):
                        error = str(j["error"])
                        break
                    msg = j.get("message") or {}
                    content, thinking = msg.get("content") or "", msg.get("thinking") or ""
                    if content or thinking:
                        self.delta(idx, content, thinking)
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if j.get("done"):
                        final = j
                        break
        except Exception as e:  # noqa: BLE001 - includes the read failing after Stop closed it
            if not self.cancelled.is_set():
                return tool_calls, None, None, True
        finally:
            with self.lock:
                self.response = None
        return tool_calls, final, error, final is None and error is None and not self.cancelled.is_set()

    def retry_wait(self, idx, attempt, why, reset=False):
        """Wait before trying a model request again (temporary errors). False when out of retries or stopped."""
        if attempt >= len(RETRY_DELAYS) or self.cancelled.is_set():
            return False
        delay = RETRY_DELAYS[attempt]
        if reset:  # drop what the broken response streamed, so the retry starts clean
            with self.lock:
                if not self.detached:
                    self.chat["messages"][idx].update(content="", thinking="")
        self.update(idx, notice="%s. Trying again in %d s (attempt %d of %d)…" % (why, delay, attempt + 2, len(RETRY_DELAYS) + 1))
        self.cancelled.wait(delay)
        return not self.cancelled.is_set()

    def count_usage(self, prompt, output, cached=0):
        """Add tokens to this chat and today's totals. Returns (chat usage, today).
        cached: input tokens Ollama served from its cache (billed at the cheaper cached-input rate)."""
        with self.lock:
            u = self.chat.setdefault("usage", {"promptTokens": 0, "outputTokens": 0, "requests": 0})
            u["promptTokens"] += prompt
            u["outputTokens"] += output
            u["requests"] += 1
            if cached: u["cachedTokens"] = u.get("cachedTokens", 0) + cached
            self.tokens += prompt + output
            session = dict(u)
        return session, self.srv.record_daily_usage(prompt, output, cached)

    def record_usage(self, final, info):
        prompt, output = int(final.get("prompt_eval_count") or 0), int(final.get("eval_count") or 0)
        session, today = self.count_usage(prompt, output, int(final.get("prompt_eval_cached_count") or 0))
        with self.lock:
            context = {"used": prompt + output, "limit": info["contextTokens"], "budget": info["budget"],
                       "omittedMessages": info["omittedMessages"], "clippedToolResults": info["clippedToolResults"],
                       "summarizedMessages": info.get("summarizedMessages", 0)}
            self.chat["context"] = context
        self.publish({"type": "usage", "session": session, "today": today, "context": context})
        speed = ""
        if output and final.get("eval_duration"):
            speed = " · %.1f tok/s" % (output / (final["eval_duration"] / 1e9))
        return "%d tokens%s" % (output, speed) if output else ""

    # ------------------------------------------------------------ balance guard
    def balance_gate(self):
        """Pause while the Ollama balance is at or below the minimum. False if the reply was stopped meanwhile."""
        srv = self.srv
        low, info, minimum = srv.balance_low()
        if not low:
            return True
        if self.steps:  # it has done some work: one last request, without tools, to summarize progress
            self.wrap_up()
            if self.cancelled.is_set():
                return False
        with self.lock:
            self.pause = {"balance": info, "minimumUsd": minimum}
            self.publish({"type": "balance_wait", "balance": info, "minimumUsd": minimum})
        self.save()
        self.set_status("paused")
        while not self.cancelled.is_set():
            self.engine.balance_event.wait(BALANCE_RECHECK)
            if self.cancelled.is_set():
                break
            low, info, minimum = srv.balance_low(max_age=BALANCE_RECHECK - 10)
            with self.lock:
                self.pause = {"balance": info, "minimumUsd": minimum}
                self.publish({"type": "balance_wait", "balance": info, "minimumUsd": minimum})
            if not low:
                break
        with self.lock:
            self.pause = None
            self.publish({"type": "balance_wait", "balance": None, "minimumUsd": minimum})
        if self.cancelled.is_set():
            return False
        if self.steps:  # tell the model why it's continuing after its summary
            self.add({"role": "user", "auto": True, "content": "[Automatic message] The Ollama balance is above $%.2f again. "
                      "Continue the work from where you stopped." % minimum})
        self.set_status("running")
        return True

    def wrap_up(self):
        idx = self.add({"role": "assistant", "content": "", "thinking": "", "model": self.chat["model"], "_live": True, "wrapUp": True})
        tools, self.use_tools, self.wrapping_up = self.use_tools, False, True
        try:
            self.stream_step(idx)
        finally:
            self.use_tools, self.wrapping_up = tools, False
        self.save()

    # ------------------------------------------------------------ subtasks
    def start_subtask(self, args):
        """Create and start a child chat. Returns (tool message index, child id), or None if it couldn't start."""
        srv = self.srv
        task = str(args.get("task") or "").strip()
        title = str(args.get("title") or task[:60] or "Subtask").strip()
        idx = self.add({"role": "tool", "tool_name": "start_subtask", "content": "", "display": title, "_running": True})
        try:
            chat = srv.saved_tool_config(self.chat_id)
            pol = capabilities.policy(chat)
            if not pol["subtasks"]:
                raise srv.ToolError("Subtasks are off for this session.")
            if not task or len(task) > 20_000:
                raise srv.ToolError("A task of 1-20,000 characters is required.")
            if len(self.children) >= MAX_SUBTASKS_PER_REPLY:
                raise srv.ToolError("This reply already started %d subtasks, the most allowed." % MAX_SUBTASKS_PER_REPLY)
            model = str(args.get("model") or "").strip() or None
            if model and model != chat.get("model"):
                if pol["modelControl"] == "fixed":
                    raise srv.ToolError("This session's model is fixed, so subtasks use %s." % chat.get("model"))
                if model not in [m["name"] for m in srv.get_models()[0]]:
                    raise srv.ToolError("Model is not currently available in Ollama Cloud: %s" % model)
                if pol["modelControl"] == "ask":
                    self.ask(idx, {"title": "Use a different model for this subtask?", "detail": "Subtask: %s\nModel: %s" % (title, model),
                                   "why": "This session asks you before changing models."}, "The user didn't allow the other model.")
            child_id = srv.create_subtask_chat(self.chat_id, title, model)
            if args.get("isolated"):  # its own Git worktree and branch (see worktrees.py)
                if gittools.level(pol) < 2:
                    srv.delete_chat(child_id)
                    raise srv.ToolError("Isolated subtasks need Git set to commit or more in Session settings.")
                try:
                    top, path, branch, base = worktrees.create(srv.data_root(), chat, child_id)
                except (worktrees.WorktreeError, gittools.GitError) as e:
                    srv.delete_chat(child_id)
                    raise srv.ToolError(str(e))
                with srv.chat_lock(child_id):
                    child = srv.load_chat(child_id)
                    child["folders"] = [f for f in child.get("folders") or []
                                        if os.path.normcase(os.path.realpath(f["path"])) != os.path.normcase(top)] + [{"path": path, "write": True}]
                    child["worktree"] = {"top": top, "path": path, "branch": branch, "base": base}
                    srv.save_chat(child_id, child)
                self.update(idx, display="%s (branch %s)" % (title, branch))
            self.update(idx, subtaskId=child_id)
            try:
                self.engine.start(child_id, {"text": task, "think": self.think}, parent=self)
            except RunError:
                srv.delete_chat(child_id)
                raise
            with self.lock:
                self.children.append(child_id)
            self.pending[child_id] = title
            return idx, child_id
        except Denied as e:
            self.update(idx, content=str(e), isError=True, _running=None)
        except (srv.ToolError, RunError, ValueError, OSError) as e:
            self.update(idx, content="Error: %s" % e, isError=True, _running=None)
        return None

    def finish_subtasks(self, started):
        """After a step that started subtasks: wait for them (or for a question from one), then give each
        start_subtask call its result."""
        ids = [cid for _, cid in started if cid in self.pending]
        reports, extra = self.collect(ids, None) if ids else ({}, "")
        for n, (idx, cid) in enumerate(started):
            text, failed = reports.get(cid, ("Started. Its result came back through wait_subtasks.", False))
            if extra and n == len(started) - 1:
                text += "\n\n" + extra
            self.update(idx, content=text, isError=True if failed else None, _running=None)

    def child_run(self, child_id):
        run = self.engine.runs.get(child_id)
        return run if run is not None and run.parent is self else None

    def collect(self, ids, timeout):
        """Wait until every subtask in ids has finished, one of this reply's subtasks asks a question, the
        timeout passes or Stop is pressed. Returns ({id: (text, failed)}, text of messages from other subtasks)."""
        end = None if timeout is None else time.time() + timeout
        with self.news:
            while not self.cancelled.is_set():
                running = [c for c in ids if (self.child_run(c) or _DONE).alive]
                asked = any(u["question"] for u in self.updates) or any((self.child_run(c) or _DONE).question for c in running)
                for c in running:
                    if (self.child_run(c) or _DONE).status != "paused":
                        self.paused_seen.discard(c)  # it continued: a later pause is news again
                paused = [c for c in running if (self.child_run(c) or _DONE).status == "paused" and c not in self.paused_seen]
                # every one still running is paused for the balance: waiting changes nothing, so this reply can pause too
                all_paused = running and all((self.child_run(c) or _DONE).status == "paused" for c in running)
                if not running or asked or paused or all_paused or self.user_waiting() or (end is not None and time.time() >= end):
                    break
                self.news.wait(0.5)
            news, self.updates = self.updates, []
        reports = {}
        for cid in ids:
            notes = [self.describe(u) for u in news if u["child"] == cid]
            run = self.child_run(cid)
            if run is not None and run.alive and not self.cancelled.is_set():
                title = self.pending.get(cid) or run.chat.get("title", "Subtask")
                text = 'Subtask "%s" (id %s) is still running.' % (title, cid)
                q = run.question
                if q is not None and not any(u is q for u in news):  # asked earlier and still unanswered
                    notes.append(self.describe(q))
                if run.status == "paused":
                    self.paused_seen.add(cid)
                    text = ('Subtask "%s" (id %s) is paused: the Ollama balance is at or below the minimum in Settings. It '
                            "continues by itself when the balance is topped up; call wait_subtasks then for its result." % (title, cid))
                    summary = next((m.get("content") for m in reversed(run.chat["messages"]) if m.get("wrapUp") and m.get("content")), "")
                    if summary:
                        text += " Its progress so far:\n\n" + summary[:SUBTASK_RESULT_CHARS]
                elif q is not None or any(u["question"] and u["child"] == cid for u in news):
                    text += (" It is waiting for your answer: reply with message_subtask (subtask \"%s\"), then call "
                             "wait_subtasks for its result." % cid)
                else:
                    text += " Call wait_subtasks again for its result."
                reports[cid] = ("\n\n".join(notes + [text]), False)
            else:
                text, failed = self.subtask_result(cid, run)
                self.pending.pop(cid, None)
                reports[cid] = ("\n\n".join(notes + [text]), failed)
        extra = "\n\n".join(self.describe(u) for u in news if u["child"] not in ids)
        for cid in {u["child"] for u in news}:
            self.absorb_untrusted(cid)
        return reports, extra

    def describe(self, u):
        if u["question"]:
            return 'Question from subtask "%s" (id %s), waiting for your answer:\n%s' % (u["title"], u["child"], u["text"])
        return 'Message from subtask "%s" (id %s):\n%s' % (u["title"], u["child"], u["text"])

    def absorb_untrusted(self, child_id):
        """What a subtask read on the web can reach this chat through its messages and results."""
        srv = self.srv
        try:
            chat = srv.load_chat(child_id)
        except (OSError, ValueError):
            return
        if chat.get("untrusted"):
            flag = srv.mark_untrusted(self.chat_id, "subtask: %s" % chat.get("title", "")[:120], chat.get("webUrls") or [])
            with self.lock:
                self.chat["untrusted"] = flag
                self.publish({"type": "untrusted", "untrusted": flag})

    def remind_subtasks(self):
        """The model ended its reply with subtasks still running or unread: remind it (a limited number of times)."""
        if not self.pending or self.reminders >= SUBTASK_REMINDERS or self.cancelled.is_set():
            return False
        self.reminders += 1
        names = ", ".join('"%s" (id %s)' % (t, c) for c, t in self.pending.items())
        self.add({"role": "user", "auto": True, "content": "[Automatic message] You haven't read the result of these subtasks yet: "
                  "%s. Call wait_subtasks to get them (answer any question with message_subtask first), then finish your "
                  "answer. Subtasks still running when your reply ends are stopped." % names})
        return True

    def stop_own_subtasks(self):
        for cid in list(self.children):
            run = self.child_run(cid)
            if run is not None and run.alive:
                self.engine.stop(cid)

    def read_inbox(self):
        """Messages sent while this reply was working, added before its next step: the user's own (as their
        messages), and the parent chat's (as automatic messages)."""
        with self.lock:
            msgs, self.inbox = self.inbox, []
        for item in msgs:
            if isinstance(item, dict):
                self.add({"role": "user", "content": item["user"], "sentWhileRunning": True})
            else:
                self.add({"role": "user", "auto": True, "fromParent": True, "content": "[Message from the parent chat]\n" + item})

    def user_waiting(self):
        """The user sent a message this reply hasn't read yet."""
        with self.lock:
            return any(isinstance(m, dict) for m in self.inbox)

    def _message_text(self, args):
        text = str(args.get("message") or "").strip()
        if not text:
            raise self.srv.ToolError("The message is empty.")
        if len(text) > MAX_SUBTASK_MESSAGE:
            raise self.srv.ToolError("Messages can be at most %d characters." % MAX_SUBTASK_MESSAGE)
        if self.sent >= MAX_MESSAGES:
            raise self.srv.ToolError("This reply already sent %d messages, the most allowed." % MAX_MESSAGES)
        return text

    def message_parent(self, idx, args):
        """A subtask's note or question to the chat that started it."""
        parent = self.parent
        text = self._message_text(args)
        if parent is None or not parent.alive or parent.finishing:
            raise self.srv.ToolError("The chat that gave you this task isn't waiting for you any more. Put this in your final answer.")
        wait = args.get("wait_for_reply") in (True, "true", "True", 1)
        u = {"child": self.chat_id, "title": self.chat.get("title", "Subtask"), "text": text, "question": wait,
             "event": threading.Event(), "answer": None}
        self.sent += 1
        if wait:
            self.question = u
            self.update(idx, display="Asked: " + text[:160], _waitingParent=True)
        with parent.news:
            parent.updates.append(u)
            parent.news.notify_all()
        if not wait:
            return "Sent to the parent chat. It sees your message the next time it checks on you."
        end = time.time() + QUESTION_WAIT
        try:
            while not u["event"].is_set() and not self.cancelled.is_set() and time.time() < end:
                if not parent.alive or parent.finishing:
                    break
                u["event"].wait(0.5)
        finally:
            self.question = None
            self.update(idx, _waitingParent=None)
        if u["answer"] is not None:
            return "The parent chat answered:\n\n" + u["answer"]
        if self.cancelled.is_set():
            raise Denied("Stopped before the parent chat answered.")
        return ("The parent chat didn't answer (its reply ended or it waited too long). Continue with your best judgement "
                "and say in your final answer what you assumed.")

    def set_subtask_mode(self, idx, args):
        """The parent changes one of its subtasks' permission mode, never above its own (see capabilities.MODES)."""
        srv = self.srv
        pol = capabilities.policy(srv.saved_tool_config(self.chat_id))
        if not pol["subtasks"]:
            raise srv.ToolError("Subtasks are off for this session.")
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in capabilities.MODES:
            raise srv.ToolError("mode must be manual, auto or bypass.")
        child_id, title = self.find_subtask(args.get("subtask"))
        self.update(idx, display="%s: %s" % (title, mode), subtaskId=child_id)
        if capabilities.MODES.index(mode) > capabilities.MODES.index(pol["mode"]):
            raise srv.ToolError("A subtask can't have more permissions than this chat (%s)." % pol["mode"])
        srv.update_permissions(child_id, {"capabilities": {"mode": mode}})
        srv.refresh_running(child_id)  # its next tool call follows the new mode
        return '"%s" is now in %s mode.' % (title, {"manual": "Manual approve", "auto": "Auto edits", "bypass": "Bypass permissions"}[mode])

    def find_subtask(self, ref):
        """A subtask chat of this chat, by id or title (this reply's subtasks first)."""
        ref = str(ref or "").strip()
        if not ref:
            raise self.srv.ToolError("Name the subtask by its id or title.")
        kids = {c["id"]: c.get("title", "") for c in self.srv.list_chats() if c.get("parentId") == self.chat_id}
        if ref in kids:
            return ref, kids[ref]
        matches = [cid for cid, t in kids.items() if t.strip().lower() == ref.lower()]
        mine = [cid for cid in matches if cid in self.children]
        if len(mine) == 1 or len(matches) == 1:
            cid = (mine or matches)[0]
            return cid, kids[cid]
        if matches:
            raise self.srv.ToolError('Several subtasks are called "%s"; use its id: %s.' % (ref, ", ".join(matches)))
        raise self.srv.ToolError("No subtask of this chat is called %s." % ref)

    def message_subtask(self, idx, args):
        """The parent's answer or follow-up to one of its subtasks."""
        srv = self.srv
        if not capabilities.policy(srv.saved_tool_config(self.chat_id))["subtasks"]:
            raise srv.ToolError("Subtasks are off for this session.")
        child_id, title = self.find_subtask(args.get("subtask"))
        text = self._message_text(args)
        self.update(idx, display='%s: %s' % (title, text[:160]), subtaskId=child_id)
        if self.chat.get("untrusted") or capabilities.policy(srv.saved_tool_config(self.chat_id))["untrusted"]:
            srv.mark_untrusted(child_id, "parent chat", list(self.chat.get("webUrls") or []))
        run = self.child_run(child_id) or self.engine.active(child_id)
        self.sent += 1
        if run is not None and run.alive:
            if run.parent is not self:
                raise srv.ToolError("That subtask is busy with another reply. Try again when it has finished.")
            self.pending[child_id] = title
            q = run.question
            if q is not None and not q["event"].is_set():
                q["answer"] = text
                q["event"].set()
                return 'Answered the question from "%s". It continues now; call wait_subtasks for its result.' % title
            with run.lock:
                if not run.inbox_closed:
                    run.inbox.append(text)
                    return 'Sent to "%s". It reads your message before its next step; call wait_subtasks for its result.' % title
            end = time.time() + 10  # it is just finishing its answer: continue it with a new reply
            while run.alive and time.time() < end:
                run.thread.join(0.2)
            if run.alive:
                raise srv.ToolError('"%s" is finishing its answer. Call wait_subtasks, then send this again.' % title)
        if len(self.children) >= MAX_SUBTASKS_PER_REPLY:
            raise srv.ToolError("This reply already used %d subtasks, the most allowed." % MAX_SUBTASKS_PER_REPLY)
        try:
            self.engine.start(child_id, {"text": "[Message from the parent chat]\n" + text, "think": self.think, "fromParent": True},
                              parent=self)
        except RunError as e:
            raise srv.ToolError(str(e))
        with self.lock:
            self.children.append(child_id)
        self.pending[child_id] = title
        return ('"%s" had finished, so it continues with your message as a new reply. Call wait_subtasks for its answer.' % title)

    def wait_subtasks(self, args):
        refs = args.get("subtasks")
        if isinstance(refs, str):
            refs = [refs] if refs.strip() else []
        ids = [self.find_subtask(r)[0] for r in refs or []] or list(self.pending)
        try:
            timeout = max(5, min(int(args.get("timeout_seconds") or WAIT_SUBTASKS), MAX_WAIT_SUBTASKS))
        except (TypeError, ValueError):
            timeout = WAIT_SUBTASKS
        if not ids and not self.updates:
            return "None of your subtasks is running or unread."
        reports, extra = self.collect(ids, timeout)
        parts = [reports[c][0] for c in ids] + ([extra] if extra else [])
        if self.user_waiting():
            parts.append("The user sent you a message (next). Read it before waiting again.")
        if self.cancelled.is_set():
            raise Denied("Stopped.")
        return "\n\n---\n\n".join(p for p in parts if p)

    def subtask_result(self, child_id, child):
        """The final answer of a finished subtask, as text for the parent (and whether it failed)."""
        srv = self.srv
        try:
            chat = srv.load_chat(child_id)
        except (OSError, ValueError):
            return "The subtask chat was deleted.", True
        answer = next((m for m in reversed(chat.get("messages") or [])
                       if m.get("role") == "assistant" and (m.get("content") or m.get("error")) and not m.get("tool_calls")), {})
        title = chat.get("title", "Subtask")
        if child is not None and child.outcome == "stopped":
            text, failed = 'Subtask "%s" was stopped before it finished.' % title, True
        elif answer.get("error") and not answer.get("content"):
            text, failed = 'Subtask "%s" failed: %s' % (title, answer["error"]), True
        else:
            body = answer.get("content") or "(no answer)"
            if len(body) > SUBTASK_RESULT_CHARS:
                body = body[:SUBTASK_RESULT_CHARS] + "\n[... %d more characters in the subtask chat]" % (len(body) - SUBTASK_RESULT_CHARS)
            text, failed = 'Subtask "%s" (id %s) finished. Its answer:\n\n%s' % (title, child_id, body), False
        wt = chat.get("worktree")
        if wt:  # commit what the helper changed on its branch, and tell the parent how to merge it
            try:
                text += "\n\n" + worktrees.finish(wt["path"], wt["branch"], wt["base"], title)
            except gittools.GitError as e:
                text += "\n\nIts worktree changes couldn't be committed: %s" % e
        if chat.get("untrusted"):  # what it read can carry untrusted content back into this chat
            self.absorb_untrusted(child_id)
        return text, failed

    def run_tool_call(self, call):
        srv = self.srv
        name, args = parse_call(call)
        display = (args.get("path") or args.get("source") or args.get("model") or args.get("query") or args.get("url")
                   or args.get("pattern") or args.get("contains") or args.get("title") or args.get("message") or args.get("repo") or "")
        if name == "read_skill_file":
            display = "%s/%s" % (args.get("skill") or "?", str(args.get("path") or ".").lstrip("./") or ".")
        if name == "search_files" and args.get("contains"):
            display = '"%s"%s' % (args["contains"], (" in " + args["path"]) if args.get("path") else "")
        message = {"role": "tool", "tool_name": name, "content": "", "display": str(display), "_running": True}
        if name in ("copy_path", "move_path") and args.get("destination"):  # where the file is now (for Preview)
            message.update(display="%s → %s" % (args.get("source") or "", args["destination"]), target=str(args["destination"]))
        idx = self.add(message)
        undo.set_context(self.chat_id, self.run_id)  # file changes made now are journaled for "Undo this reply"
        gittools.set_cancel(self.cancelled)  # Git calls end as soon as Stop is pressed
        try:
            if self.plan and name not in srv.READ_ONLY_TOOLS:
                raise srv.ToolError("Plan mode: nothing can be changed yet. Finish the plan; the user will ask you to carry it out.")
            if name in srv.REVIEWED_TOOLS:
                chat = srv.saved_tool_config(self.chat_id)
                if capabilities.policy(chat)["editMode"] == "auto":
                    out = srv.run_tool(chat, name, args)
                else:
                    prepared = srv.prepare_write(self.chat_id, name, args)
                    self.update(idx, display=prepared["path"], _approval={"path": prepared["path"]})
                    data = {k: v for k, v in prepared.items() if k != "token"}
                    ok = self.wait_approval("write", data)
                    self.update(idx, _approval=None)
                    if not ok:
                        srv.cancel_write(self.chat_id, prepared["token"])
                        raise Denied("The write was cancelled." if self.cancelled.is_set() else "The user denied this change.")
                    out = srv.run_approved_write(self.chat_id, prepared["token"])
            elif name == "set_session_model":
                r = srv.change_session_model(self.chat_id, args)
                if r.get("needsApproval"):
                    self.update(idx, _modelApproval={"model": r["model"]})
                    data = {k: r.get(k) for k in ("model", "previous", "reason", "capabilities", "warnings")}
                    ok = self.wait_approval("model", data)
                    self.update(idx, _modelApproval=None)
                    if not ok:
                        srv.cancel_write(self.chat_id, r["approvalToken"])
                        raise Denied("The model change was cancelled." if self.cancelled.is_set() else "The user denied the model change.")
                    r = srv.change_session_model(self.chat_id, args, r["approvalToken"])
                out = r["content"]
                if r.get("model"):
                    with self.lock:
                        self.chat["model"] = r["model"]
                    self.set_status(self.status)  # tells windows about the new model
            elif name in ("web_search", "web_fetch"):
                out = self.run_web_tool(idx, name, args)
            elif name == "message_parent":
                out = self.message_parent(idx, args)
            elif name == "message_subtask":
                out = self.message_subtask(idx, args)
            elif name == "set_subtask_mode":
                out = self.set_subtask_mode(idx, args)
            elif name == "wait_subtasks":
                self.update(idx, display="Waiting for subtasks")
                out = self.wait_subtasks(args)
            elif name in ("read_memory", "save_memory"):
                chat = srv.saved_tool_config(self.chat_id)
                pol = capabilities.policy(chat)
                if not pol["memory"]:
                    raise srv.ToolError("Memory is off for this session.")
                if name == "read_memory":
                    out = srv.read_memory_tool(chat)
                else:
                    scope = srv.memory_scope(chat, args.get("scope"))
                    self.update(idx, display=str(args.get("note") or "")[:200])
                    if pol["askMemory"] or self.plan:  # a web page must not plant lasting instructions
                        self.ask(idx, {"title": "Save this to memory?", "detail": str(args.get("note") or ""),
                                       "why": "This chat has read web content, so saving memory needs your approval."},
                                 "The user didn't allow saving this note.")
                    out = projects.add_note(srv.data_root(), scope, args.get("note"), args.get("replace"))
            elif name == "run_command":
                chat = srv.saved_tool_config(self.chat_id)
                plan = commands.prepare(chat, args, srv.load_config().get("allowedCommands") or [])
                self.update(idx, display=plan["line"])
                pol = capabilities.policy(chat)
                if pol["mode"] != "bypass" and (not plan["allowedBy"] or pol["askEveryCommand"]):
                    why = ("This command isn't on your allowed list (Settings > Commands). It runs with your Windows account and can reach anything you can."
                           if not plan["allowedBy"] else "This chat is in Manual mode, so every command asks first." if pol["mode"] == "manual"
                           else "This chat has read web content, so every command needs your approval.")
                    self.ask(idx, {"title": "Run this command?", "detail": "Folder: %s\nCommand: %s\nTime limit: %ds" % (plan["cwd"], plan["line"], plan["timeout"]),
                                   "why": why}, "The user didn't allow this command.")
                out = commands.execute(plan, self.cancelled)
                undo.record(srv.data_root(), "command", path=plan["line"])
            elif name in gittools.REVIEWED:  # commit / merge / push / pull request: the user approves each one
                data, record = gittools.prepare(srv.saved_tool_config(self.chat_id), name, args)
                if capabilities.policy(srv.saved_tool_config(self.chat_id))["askGit"]:  # Bypass: no approval
                    self.ask(idx, data, "The user didn't approve this.")
                out = gittools.execute(srv.saved_tool_config(self.chat_id), name, args, record)
                undo.record(srv.data_root(), "git", path=name.replace("_", " "))
            else:
                chat = srv.saved_tool_config(self.chat_id)
                if name in capabilities.GATED_FILE_OPS and capabilities.policy(chat)["askFileOps"]:
                    detail = "\n".join("%s: %s" % (k, v) for k, v in args.items() if k in ("path", "source", "destination", "trash_id"))
                    self.ask(idx, {"title": ACTION_TITLES.get(name, "Allow this action?"), "detail": detail, "why": MANUAL_WHY},
                             "The user denied this action.")
                    chat = srv.saved_tool_config(self.chat_id)  # permissions may have changed while waiting
                out = srv.run_tool(chat, name, args)
            if name in CHANGING_TOOLS:
                self.update(idx, content=out, _running=None, runId=self.run_id)  # the window offers "Undo this reply"
            else:
                self.update(idx, content=out, _running=None)
        except Denied as e:
            self.update(idx, content=str(e), isError=True, _running=None)
        except (srv.ToolError, web.WebError, gittools.GitError, commands.CommandError, projects.ProjectError, ValueError, OSError) as e:
            self.update(idx, content="Error: %s" % e, isError=True, _running=None)
        finally:
            undo.set_context(None, None)
            gittools.set_cancel(None)

    def ask(self, idx, data, denied_text):
        """Ask the user to approve a gated action; raises Denied unless they allow it."""
        self.update(idx, _actionApproval={"title": data["title"]})
        ok = self.wait_approval("action", data)
        self.update(idx, _actionApproval=None)
        if not ok:
            raise Denied("The action was cancelled." if self.cancelled.is_set() else denied_text)

    def run_web_tool(self, idx, name, args):
        srv = self.srv
        chat = srv.saved_tool_config(self.chat_id)
        if not capabilities.policy(chat)["webSearch"]:
            raise srv.ToolError("Web search is off for this session.")
        if name == "web_search":
            out, urls = web.search(srv.cloud_post, args.get("query"), args.get("max_results", 5))
            source = 'search: "%s"' % str(args.get("query"))[:120]
        else:
            url = web.valid_url(args.get("url"))  # web search on: any page opens without asking
            out, urls = web.fetch(srv.cloud_post, url)
            urls = [url]  # links found on the page stay unapproved
            source = "page: %s" % url[:200]
        flag = srv.mark_untrusted(self.chat_id, source, urls)
        with self.lock:
            self.chat["untrusted"] = flag
            self.publish({"type": "untrusted", "untrusted": flag})
        return out


def parse_call(call):
    """(tool name, arguments dict) from an Ollama tool call, tolerating JSON-string arguments."""
    fn = (call or {}).get("function") or {}
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try: args = json.loads(args)
        except ValueError: args = {}
    return str(fn.get("name") or ""), (args if isinstance(args, dict) else {})
