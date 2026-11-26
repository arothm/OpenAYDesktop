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

Subtasks: a reply can start child chats (start_subtask). Each runs as its own Run; the
parent starts all subtasks requested in one step, then waits for them and returns each
child's final answer as that tool call's result. Stopping a parent stops its children.
The engine is created by server.py with the server module as `srv`, which provides
storage, tools, approvals and the Ollama Cloud connection.
"""

import collections
import copy
import json
import secrets
import threading
import time
import urllib.error

import capabilities
import context_budget
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
SUBTASK_RESULT_CHARS = 20_000
HISTORY = 30                  # finished replies kept for the Activity view


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
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.status = "running"
        self.alive = True
        self.approval = None
        self.response = None
        self.use_tools = True
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
                if not isinstance(text, str) or not isinstance(images, list):
                    raise RunError("Invalid message.")
                if len(images) > MAX_IMAGES or not all(isinstance(i, str) and 0 < len(i) <= MAX_IMAGE_CHARS for i in images):
                    raise RunError("Attach at most %d images." % MAX_IMAGES)
                if not text.strip() and not images:
                    raise RunError("The message is empty.")
                message = {"role": "user", "content": text}
                if images:
                    message["images"] = images
                msgs.append(message)
            if not chat.get("model"):
                raise RunError("Pick a model for this chat first.")
            chat["updated"] = now_ms()
            srv.save_chat(self.chat_id, chat)
        return chat

    # ------------------------------------------------------------ events and state
    def public_approval(self):
        a = self.approval
        return {"id": a["id"], "kind": a["kind"], "data": a["data"]} if a else None

    def snapshot_event(self):
        return {"type": "snapshot", "chat": copy.deepcopy(self.chat), "status": self.status, "approval": self.public_approval()}

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
        with srv.chat_lock(self.chat_id):
            try:
                disk = srv.load_chat(self.chat_id)
            except (OSError, ValueError):
                return  # deleted meanwhile
            disk["messages"], disk["model"], disk["updated"] = msgs, model, now_ms()
            if usage: disk["usage"] = usage
            if context: disk["context"] = context
            srv.save_chat(self.chat_id, disk)

    def summary(self):
        with self.lock:
            return {"chatId": self.chat_id, "title": self.chat.get("title", ""), "parentId": self.chat.get("parentId"),
                    "model": self.chat.get("model", ""), "status": self.status if self.alive else (self.outcome or "done"),
                    "approval": self.approval["kind"] if self.approval else None, "started": int(self.started * 1000),
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
                if self.cancelled.is_set():
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
        srv = self.srv
        with self.lock:
            history = [m for m in copy.deepcopy(self.chat["messages"]) if not m.get("_live")]
        info_model = srv.model_info(model)
        msgs, info = context_budget.build(history, info_model.get("context_length"))
        body = {"model": model, "messages": msgs, "stream": True}
        if self.think is not None:
            body["think"] = self.think
        fcfg = srv.saved_tool_config(self.chat_id)  # fresh from disk: permissions/skills may change mid-run
        system = [skills.prompt(srv.skills_root(), fcfg.get("skills") or [])]
        if self.use_tools:
            specs, where = srv.tool_specs(fcfg)
            if specs:
                body["tools"] = specs
                system.append(srv.tool_system_note(fcfg, specs, where))
        note = "\n\n".join(x for x in system if x)
        if note:
            if msgs and msgs[0]["role"] == "system":
                msgs[0] = dict(msgs[0], content=msgs[0]["content"] + "\n\n" + note)
            else:
                msgs.insert(0, {"role": "system", "content": note})
        self.publish(dict(info, type="context"))
        return body, info

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

    def record_usage(self, final, info):
        prompt, output = int(final.get("prompt_eval_count") or 0), int(final.get("eval_count") or 0)
        with self.lock:
            u = self.chat.setdefault("usage", {"promptTokens": 0, "outputTokens": 0, "requests": 0})
            u["promptTokens"] += prompt
            u["outputTokens"] += output
            u["requests"] += 1
            self.tokens += prompt + output
            context = {"used": prompt + output, "limit": info["contextTokens"], "omittedMessages": info["omittedMessages"],
                       "clippedToolResults": info["clippedToolResults"]}
            self.chat["context"] = context
            session = dict(u)
        today = self.srv.record_daily_usage(prompt, output)
        self.publish({"type": "usage", "session": session, "today": today, "context": context})
        speed = ""
        if output and final.get("eval_duration"):
            speed = " · %.1f tok/s" % (output / (final["eval_duration"] / 1e9))
        return "%d tokens%s" % (output, speed) if output else ""

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
                   or args.get("pattern") or args.get("contains") or "")
        if name == "search_files" and args.get("contains"):
            display = '"%s"%s' % (args["contains"], (" in " + args["path"]) if args.get("path") else "")
        idx = self.add({"role": "tool", "tool_name": name, "content": "", "display": str(display), "_running": True})
        try:
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
            else:
                chat = srv.saved_tool_config(self.chat_id)
                if name in capabilities.GATED_WHEN_UNTRUSTED and capabilities.policy(chat)["untrusted"]:
                    detail = "\n".join("%s: %s" % (k, v) for k, v in args.items() if k in ("path", "source", "destination", "trash_id"))
                    self.ask(idx, {"title": ACTION_TITLES.get(name, "Allow this action?"), "detail": detail, "why": UNTRUSTED_WHY},
                             "The user denied this action.")
                    chat = srv.saved_tool_config(self.chat_id)  # permissions may have changed while waiting
                out = srv.run_tool(chat, name, args)
            self.update(idx, content=out, _running=None)
        except Denied as e:
            self.update(idx, content=str(e), isError=True, _running=None)
        except (srv.ToolError, web.WebError, ValueError, OSError) as e:
            self.update(idx, content="Error: %s" % e, isError=True, _running=None)

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
