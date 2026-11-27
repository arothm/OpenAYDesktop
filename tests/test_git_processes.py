"""Git must never hang a reply: no inherited stdin, and a timeout stops the whole process tree.

Run with:  python -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import gittools  # noqa: E402

# A server like the installed app's: its stdin is a pipe that a thread keeps reading (--exit-with-stdin).
SERVER_LIKE = r"""
import sys, threading, time
sys.path.insert(0, sys.argv[1])
import gittools
threading.Thread(target=sys.stdin.read, daemon=True).start()
time.sleep(0.3)
print(gittools.run(sys.argv[2], "rev-parse", "--show-toplevel").strip(), flush=True)
"""


@unittest.skipUnless(shutil.which("git"), "Git isn't installed")
class GitProcessTests(unittest.TestCase):
    def test_git_works_while_the_server_reads_its_stdin(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
            server = subprocess.Popen([sys.executable, "-c", SERVER_LIKE, str(APP), repo], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # Keep the server's stdin open, as Electron does (communicate() would close it and end the read).
            out = []
            reader = threading.Thread(target=lambda: out.append(server.stdout.readline()), daemon=True); reader.start()
            reader.join(30)
            hung = reader.is_alive()
            if os.name == "nt": subprocess.run(["taskkill", "/T", "/F", "/PID", str(server.pid)], capture_output=True)
            else: server.kill()
            server.wait(10)
            self.assertFalse(hung, "git hung while the server was reading its stdin")
            self.assertEqual(os.path.realpath(repo).lower(), os.path.realpath(out[0].strip()).lower(), server.stderr.read())

    def test_timeout_stops_children_that_hold_the_output_pipes(self):
        # Like Git's launcher: the program starts a child that keeps stdout open after the parent is killed.
        code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); time.sleep(40)"
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            gittools.call([sys.executable, "-c", code], timeout=1)
        self.assertLess(time.time() - started, 15)


if __name__ == "__main__":
    unittest.main()
