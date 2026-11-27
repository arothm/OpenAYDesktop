"""Running commands for a chat: an allowed list you control, your approval for everything else.

Matching (Settings > Allowed commands, one entry per line):
    npm test            matches exactly "npm test"
    python -m pytest *  matches "python -m pytest" followed by any extra arguments
Anything else needs the user's approval each time, and so does every command in a chat that has
read untrusted web content.

How a command runs:
  - No shell: the line is split into arguments; shell operators (&& | ; > < ` $( ) are refused.
  - .cmd/.bat programs (npm, yarn, ...) are run by cmd.exe on Windows, so arguments containing
    cmd.exe special characters are refused for them.
  - It runs in a shared folder with "Allow edits"; the Ollama API key is removed from its
    environment; it has a time limit; Stop ends it with everything it started.
  - It runs with your user account, so it can reach anything you can: that's what the allowed
    list and approvals are for.
"""

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

import capabilities

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
HEAD, TAIL = 15_000, 45_000            # output kept: the start and the (usually more useful) end
# There is no shell, so operators would just be passed along as text. Refuse arguments that are (or
# start with) one, so a model expecting "a && b" or "x > file" gets a clear error instead of a surprise.
OPERATOR_ARGS = ("&&", "||", "|", ";", ">", ">>", "<", "&", "2>", "2>&1", "1>")
CMD_SPECIAL = set('&|<>^%!"\n\r')      # meaningful to cmd.exe, which runs .cmd/.bat files
SECRET_ENV = ("OLLAMA_API_KEY", "OPENAY_DATA_DIR")
MAX_ENTRIES, MAX_ENTRY = 100, 200


class CommandError(Exception):
    pass


def _split_windows(line):
    """Split a command line exactly as Windows programs do (keeps C:\\paths\\intact)."""
    import ctypes
    from ctypes import wintypes
    to_argv = ctypes.windll.shell32.CommandLineToArgvW
    to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = to_argv(line, ctypes.byref(count))
    if not argv:
        raise ValueError("unreadable command line")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def split(line):
    line = str(line or "").strip()
    if not line:
        raise CommandError("A command is required.")
    if len(line) > 2000:
        raise CommandError("The command is too long.")
    if "\n" in line or "\r" in line:
        raise CommandError("Run one command at a time (one line).")
    try:
        argv = _split_windows(line) if os.name == "nt" else shlex.split(line, posix=True)
    except ValueError as e:
        raise CommandError("Couldn't read the command: %s" % e)
    if not argv:
        raise CommandError("A command is required.")
    for i, arg in enumerate(argv):
        operator = arg.endswith((";", "&&", "||", "|")) or (i > 0 and (arg in OPERATOR_ARGS or arg.startswith(("&&", "||", "|", ">", "<"))))
        if operator:
            raise CommandError("Shell operators like %r aren't supported (commands run without a shell): run one command "
                               "at a time, without pipes or redirection." % arg)
    return argv


def clean_allowed(entries):
    """Validate the Settings list: [str], each a single command, optionally ending in " *"."""
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise CommandError("Allowed commands must be a list of at most %d entries." % MAX_ENTRIES)
    out = []
    for e in entries:
        e = " ".join(str(e or "").split())
        if not e:
            continue
        if len(e) > MAX_ENTRY:
            raise CommandError("An allowed command is longer than %d characters." % MAX_ENTRY)
        split(e[:-2] if e.endswith(" *") else e)  # must be a valid single command
        if e not in out:
            out.append(e)
    return out


def _norm(arg):
    """Program names compare without path or extension, case-insensitively on Windows."""
    return arg.lower() if os.name == "nt" else arg


def _program(name):
    base = os.path.basename(name)
    root, ext = os.path.splitext(base)
    return _norm(root if ext.lower() in (".exe", ".cmd", ".bat", ".com") else base)


def allowed_by(argv, entries):
    """The allowed-list entry that matches argv, or None."""
    for entry in entries or []:
        wildcard = entry.endswith(" *")
        pattern = split(entry[:-2] if wildcard else entry)
        if len(argv) < len(pattern) or (not wildcard and len(argv) != len(pattern)):
            continue
        if _program(argv[0]) != _program(pattern[0]):
            continue
        if argv[1:len(pattern)] == pattern[1:]:
            return entry
    return None


def resolve_program(argv):
    exe = shutil.which(argv[0])
    if not exe:
        raise CommandError("Program not found: %s" % argv[0])
    if os.name == "nt" and os.path.splitext(exe)[1].lower() in (".cmd", ".bat"):
        bad = [a for a in argv[1:] if CMD_SPECIAL & set(a)]
        if bad:
            raise CommandError("%s is a batch file, and these arguments contain characters cmd.exe would treat as commands: %s"
                               % (os.path.basename(exe), ", ".join(repr(b) for b in bad)))
    return exe


def prepare(chat, args, allowed_entries):
    """Validate a run_command request. Returns a plan; plan["allowedBy"] is None when it needs approval."""
    if not capabilities.policy(chat).get("commands"):
        raise CommandError("This session doesn't allow running commands. Turn it on in Session settings.")
    argv = split(args.get("command"))
    grants = capabilities.grants(chat)
    raw_cwd = args.get("cwd") or (grants[0]["path"] if len(grants) == 1 else "")
    try:
        cwd = capabilities.resolve(chat, raw_cwd, write=True)
    except capabilities.CapabilityError as e:
        raise CommandError("%s Commands run in a shared folder with Allow edits." % e)
    if not os.path.isdir(cwd):
        raise CommandError("Not a folder: %s" % cwd)
    try:
        timeout = max(5, min(int(args.get("timeout_seconds") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    exe = resolve_program(argv)
    return {"argv": argv, "exe": exe, "cwd": cwd, "timeout": timeout, "line": subprocess.list2cmdline(argv),
            "allowedBy": allowed_by(argv, allowed_entries)}


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()


def execute(plan, cancelled=None):
    """Run a prepared command. Returns a text report: exit code, time, and output (start and end kept)."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(CI="1", NO_COLOR="1", FORCE_COLOR="0", PYTHONIOENCODING="utf-8")
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} \
        if os.name == "nt" else {"start_new_session": True}
    started = time.time()
    proc = subprocess.Popen([plan["exe"], *plan["argv"][1:]], cwd=plan["cwd"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    chunks, size = [], [0]

    def read():
        for block in iter(lambda: proc.stdout.read(4096), b""):
            chunks.append(block); size[0] += len(block)
            if size[0] > 4 * (HEAD + TAIL):  # keep memory bounded: first part plus a rolling tail
                head = b"".join(chunks)[:HEAD]; tail = b"".join(chunks)[-TAIL:]
                chunks[:] = [head, b"\n[...]\n", tail]; size[0] = len(head) + len(tail)
    reader = threading.Thread(target=read, daemon=True); reader.start()
    outcome = None
    while proc.poll() is None:
        if cancelled is not None and cancelled.is_set():
            outcome = "stopped"
        elif time.time() - started > plan["timeout"]:
            outcome = "timed out after %d seconds" % plan["timeout"]
        if outcome:
            _kill_tree(proc); break
        time.sleep(0.1)
    proc.wait(); reader.join(5); proc.stdout.close()
    text = b"".join(chunks).decode("utf-8", "replace")
    if len(text) > HEAD + TAIL:
        text = text[:HEAD] + "\n[... %d characters not shown ...]\n" % (len(text) - HEAD - TAIL) + text[-TAIL:]
    status = outcome or ("exit code %d" % proc.returncode)
    return "$ %s\n(in %s, %s, %.1fs)\n\n%s" % (plan["line"], plan["cwd"], status, time.time() - started, text.strip() or "(no output)")
