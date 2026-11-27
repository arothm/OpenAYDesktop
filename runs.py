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
The engine is created by server.py with the server module as `srv`, which provides
storage, tools, approvals and the Ollama Cloud connection.
"""

import collections
import copy
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
UNTRUSTED_WHY = ("This chat has read web content, which can contain text written to steer the model. "
                 "So file changes in this chat need your approval each time.")

MAX_STEPS = 25
APPROVAL_WAIT = 1800          # seconds an approval waits before it counts as denied
EVENT_BUFFER = 5000
MAX_IMAGES = 10
MAX_IMAGE_CHARS = 20_000_000  # base64 characters per image
MAX_SUBTASKS_PER_REPLY = 6
MAX_DOCUMENTS = 10
CHANGING_TOOLS = {"write_file", "edit_file", "create_folder", "copy_path", "move_path", "delete_path", "restore_path"}
SUBTASK_RESULT_CHARS = 20_000
HISTORY = 30                  # finished replies kept for the Activity view
BALANCE_RECHECK = 60          # seconds between balance checks while paused
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

    def publish(self, event):
        with self.cond:
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
        return {cid: r.status for cid, r in list(self.runs.items()) if r.alive}

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
        return bool(run)

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
        self.approval = None
        self.response = None
        self.use_tools = True
        self.plan = bool(options.get("plan"))      # plan mode: read-only tools and a plan as the answer
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
        return {"type": "snapshot", "chat": copy.deepcopy(self.chat), "status": self.status, "approval": self.public_approval(),
                "pause": copy.deepcopy(self.pause)}

    def publish(self, event):
        self.engine.channel(self.chat_id).publish(event)

    def add(self, message):
        with self.lock:
            self.chat["messages"].append(message)
            idx = len(self.chat["messages"]) - 1
            self.publish({"type": "message", "index": idx, "message": copy.deepcopy(message)})
            return idx

    def update(self, idx, **fields):
        """Set fields on a message; a value of None removes that field."""
        with self.lock:
            m = self.chat["messages"][idx]
            for key, value in fields.items():
                if value is None:
                    m.pop(key, None)
                else:
                    m[key] = value
            self.publish({"type": "message", "index": idx, "message": copy.deepcopy(m)})

    def delta(self, idx, content, thinking):
        with self.lock:
            m = self.chat["messages"][idx]
            m["content"] = (m.get("content") or "") + content
            m["thinking"] = (m.get("thinking") or "") + thinking
            self.publish({"type": "delta", "index": idx, "content": content, "thinking": thinking})

    def set_status(self, status):
        with self.lock:
            self.status = status
            self.publish({"type": "status", "status": status, "model": self.chat.get("model")})

    def save(self):
        """Write messages, model and usage; folders/permissions/title keep what is on disk."""
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
            return {"chatId": self.chat_id, "title": self.chat.get("title", ""), "parentId": self.chat.get("parentId"),
                    "model": self.chat.get("model", ""), "status": self.status if self.alive else (self.outcome or "done"),
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
            for step in range(MAX_STEPS):
                if self.cancelled.is_set() or not self.balance_gate():
                    break
                idx = self.add({"role": "assistant", "content": "", "thinking": "", "model": self.chat["model"], "_live": True})
                self.stream_step(idx)
                reply = self.chat["messages"][idx]
                if self.cancelled.is_set() or reply.get("error") or not reply.get("tool_calls"):
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
                for idx_child in started:
                    self.finish_subtask(*idx_child)
                self.save()
            else:
                self.add({"role": "assistant", "content": "", "model": self.chat["model"],
                          "error": "Stopped after %d tool steps. Send a message to continue." % MAX_STEPS})
        except Exception as e:  # noqa: BLE001 - never leave a run stuck in "running"
            self.add({"role": "assistant", "content": "", "model": self.chat.get("model", ""), "error": "Unexpected error: %s" % e})
        finally:
            with self.lock:
                for m in self.chat["messages"]:
                    for flag in ("_live", "_running", "_approval", "_modelApproval", "_actionApproval"):
                        m.pop(flag, None)
                last = self.chat["messages"][-1] if self.chat["messages"] else {}
                self.outcome = "stopped" if self.cancelled.is_set() else "error" if last.get("error") else "done"
                self.ended = time.time()
            try:
                self.save()  # before the run counts as finished, so a new reply can't start from an older file
            finally:
                with self.lock:
                    self.alive = False
                self.engine.finished(self)
                self.set_status("idle")

    def build_request(self, model):
        """The request for one model step. Its start (instructions, tools, summary, history) stays the same
        from step to step and turn to turn, so Ollama can bill it at the cached-input rate; anything that
        changes per request (the wrap-up note) goes at the end."""
        srv = self.srv
        fcfg = srv.saved_tool_config(self.chat_id)  # fresh from disk: permissions/skills may change mid-run
        specs, where = srv.tool_specs(fcfg, plan=self.plan) if self.use_tools else ([], "")
        names = {x["function"]["name"] for x in specs}
        system = [srv.instructions_prompt(fcfg), skills.prompt(srv.skills_root(), fcfg.get("skills") or [],
                                                                "read_skill_file" in names, "run_command" in names)]
        if specs:
            system.append(srv.tool_system_note(fcfg, specs, where))
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
    def compact(self, model, history, compaction, context_tokens, budget_name, overhead):
        """Summarize older turns with the chat's model so the request fits the context budget.

        Big histories are folded chunk by chunk: each request gets the summary so far plus the next
        chunk. On any failure the request falls back to leaving out the oldest turns."""
        srv = self.srv
        plan = context_budget.plan_compaction(history, compaction, context_tokens, budget_name, overhead)
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
        while True:
            body, info = self.build_request(model)
            try:
                r = srv.upstream("/api/chat", body)
                break
            except urllib.error.HTTPError as e:
                msg = srv.http_error_text(e)
                if self.use_tools and "does not support tools" in msg.lower():
                    self.use_tools = False  # retry this step without any tools
                    self.update(idx, notice="This model doesn't support tools, so folder access is off for this reply.")
                    continue
                self.update(idx, error=msg, _live=None, thinking=None)
                return
            except Exception as e:  # noqa: BLE001
                self.update(idx, error="Could not reach Ollama Cloud: %s" % e, _live=None, thinking=None)
                return
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
        except Exception as e:  # noqa: BLE001 - includes the read failing after Stop closed it
            if not self.cancelled.is_set():
                error = str(e)
        finally:
            with self.lock:
                self.response = None
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
            return idx, child_id
        except Denied as e:
            self.update(idx, content=str(e), isError=True, _running=None)
        except (srv.ToolError, RunError, ValueError, OSError) as e:
            self.update(idx, content="Error: %s" % e, isError=True, _running=None)
        return None

    def finish_subtask(self, idx, child_id):
        """Wait for a child chat's reply and return its final answer as the tool result."""
        srv = self.srv
        child = self.engine.runs.get(child_id)
        while child and child.alive:
            child.thread.join(0.5)  # Stop cancels the children too, so this always ends
        try:
            chat = srv.load_chat(child_id)
        except (OSError, ValueError):
            self.update(idx, content="The subtask chat was deleted.", isError=True, _running=None)
            return
        answer = next((m for m in reversed(chat.get("messages") or [])
                       if m.get("role") == "assistant" and (m.get("content") or m.get("error")) and not m.get("tool_calls")), {})
        title = chat.get("title", "Subtask")
        if child and child.outcome == "stopped":
            text, failed = 'Subtask "%s" was stopped before it finished.' % title, True
        elif answer.get("error") and not answer.get("content"):
            text, failed = 'Subtask "%s" failed: %s' % (title, answer["error"]), True
        else:
            body = answer.get("content") or "(no answer)"
            if len(body) > SUBTASK_RESULT_CHARS:
                body = body[:SUBTASK_RESULT_CHARS] + "\n[... %d more characters in the subtask chat]" % (len(body) - SUBTASK_RESULT_CHARS)
            text, failed = 'Subtask "%s" finished. Its answer:\n\n%s' % (title, body), False
        wt = chat.get("worktree")
        if wt:  # commit what the helper changed on its branch, and tell the parent how to merge it
            try:
                text += "\n\n" + worktrees.finish(wt["path"], wt["branch"], wt["base"], title)
            except gittools.GitError as e:
                text += "\n\nIts worktree changes couldn't be committed: %s" % e
        if chat.get("untrusted"):  # what it read can carry untrusted content back into this chat
            flag = srv.mark_untrusted(self.chat_id, "subtask: %s" % title[:120], chat.get("webUrls") or [])
            with self.lock:
                self.chat["untrusted"] = flag
                self.publish({"type": "untrusted", "untrusted": flag})
        self.update(idx, content=text, isError=True if failed else None, _running=None)

    def run_tool_call(self, call):
        srv = self.srv
        name, args = parse_call(call)
        display = (args.get("path") or args.get("source") or args.get("model") or args.get("query") or args.get("url")
                   or args.get("pattern") or args.get("contains") or args.get("title") or args.get("message") or args.get("repo") or "")
        if name == "read_skill_file":
            display = "%s/%s" % (args.get("skill") or "?", str(args.get("path") or ".").lstrip("./") or ".")
        if name == "search_files" and args.get("contains"):
            display = '"%s"%s' % (args["contains"], (" in " + args["path"]) if args.get("path") else "")
        idx = self.add({"role": "tool", "tool_name": name, "content": "", "display": str(display), "_running": True})
        undo.set_context(self.chat_id, self.run_id)  # file changes made now are journaled for "Undo this reply"
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
                    if pol["untrusted"] or self.plan:  # a web page must not plant lasting instructions
                        self.ask(idx, {"title": "Save this to memory?", "detail": str(args.get("note") or ""),
                                       "why": "This chat has read web content, so saving memory needs your approval."},
                                 "The user didn't allow saving this note.")
                    out = projects.add_note(srv.data_root(), scope, args.get("note"), args.get("replace"))
            elif name == "run_command":
                chat = srv.saved_tool_config(self.chat_id)
                plan = commands.prepare(chat, args, srv.load_config().get("allowedCommands") or [])
                self.update(idx, display=plan["line"])
                if not plan["allowedBy"] or capabilities.policy(chat)["untrusted"]:
                    why = ("This chat has read web content, so every command needs your approval." if plan["allowedBy"] else
                           "This command isn't on your allowed list (Settings). It runs with your Windows account and can reach anything you can.")
                    self.ask(idx, {"title": "Run this command?", "detail": "Folder: %s\nCommand: %s\nTime limit: %ds" % (plan["cwd"], plan["line"], plan["timeout"]),
                                   "why": why}, "The user didn't allow this command.")
                out = commands.execute(plan, self.cancelled)
                undo.record(srv.data_root(), "command", path=plan["line"])
            elif name in gittools.REVIEWED:  # commit / merge / push / pull request: the user approves each one
                data, record = gittools.prepare(srv.saved_tool_config(self.chat_id), name, args)
                self.ask(idx, data, "The user didn't approve this.")
                out = gittools.execute(srv.saved_tool_config(self.chat_id), name, args, record)
                undo.record(srv.data_root(), "git", path=name.replace("_", " "))
            else:
                chat = srv.saved_tool_config(self.chat_id)
                if name in capabilities.GATED_WHEN_UNTRUSTED and capabilities.policy(chat)["untrusted"]:
                    detail = "\n".join("%s: %s" % (k, v) for k, v in args.items() if k in ("path", "source", "destination", "trash_id"))
                    self.ask(idx, {"title": ACTION_TITLES.get(name, "Allow this action?"), "detail": detail, "why": UNTRUSTED_WHY},
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
            url = web.valid_url(args.get("url"))
            user_texts = [m.get("content") or "" for m in chat.get("messages") or [] if m.get("role") == "user"]
            if not web.known_url(url, chat.get("webUrls") or [], user_texts):
                self.ask(idx, {"title": "Open this web page?", "detail": url,
                               "why": "This address didn't come from a web search or from your messages. A page address can carry "
                                      "information out of this chat, so opening it needs your approval."},
                         "The user didn't allow opening this page.")
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
