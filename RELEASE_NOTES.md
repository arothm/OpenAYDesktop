# Open AY Desktop 1.16.1

## New
- **The main chat can rename and delete its subtasks.** Ask it to, for example "rename the helper to Price research" or "delete the scratch subtasks":
  - **Rename** happens without asking.
  - **Delete** removes the subtask chat and its messages, stopping it first if it's still running. Outside **Bypass permissions** you approve each deletion; files the subtask changed stay as they are.

## Verification
- 210 automated tests (3 new).
- The packaged-app smoke test.

---

# Open AY Desktop 1.16.0

Simpler permissions, writing to a reply while it works, session defaults, and no more stop at 25 steps.

## Permission modes
Session settings now has two options instead of ten:
- **Permission mode:**
  - **Manual approve** (default) asks before every file change, command, commit, push and model change.
  - **Auto edits** writes, edits, creates, moves and deletes files without asking (deleted files go to Trash). Commands not on your allowed list, commits, pushes and model changes still ask.
  - **Bypass permissions** asks for nothing.
- **Web search:** the chat searches and opens any page without asking.

Every chat can now use Git, commands, subtasks, memory and instruction files; the mode decides what asks first. A chip under the message box shows the mode and switches it, also before a new chat's first message. After a chat reads web content, Auto edits asks before every command and memory note; Bypass never asks. Chats from earlier versions get Auto edits if they had auto-edit on, Manual approve otherwise.

## New
- **Write while a reply works.** Type and press Enter (the stop button becomes send while there's text). The reply reads your message before its next step; a reply waiting for its subtasks reads it straight away.
- **Settings > Session defaults:** the mode, web search and skills every new chat starts with. **Use defaults** in Session settings applies them to one chat. Projects can set their own mode.
- **The main chat can change a subtask's mode** (`set_subtask_mode`), never above its own.
- **Steps per reply** (Settings > Usage & limits, default 100, 0 = no limit). A reply that reaches it pauses with a **Continue** button.

## Changed
- A reply no longer shows a blinking cursor and model name before it has written anything; the working line above the message box covers that. Replies name their model only when it differs from the chat's current one.

## Fixed
- A reply stopped after 25 tool steps and needed you to type "continue".
- You couldn't send a message to a chat while its reply was running.

## Verification
- 207 automated tests (24 new).
- A live check in the window with a fake model: the mode chip and menu, Session settings, Session defaults, projects, a message sent during a reply, and the hidden placeholder.
- The packaged-app smoke test.

---

# Open AY Desktop 1.15.0

A new layout, a working indicator, Compact, background mode, and fixes from a full review.

## New layout
- **Sidebar:** New chat, search, projects and your sessions; at the bottom, your Ollama balance and Settings.
- **Top:** the chat's name, with a menu (rename, session settings, terminal and preview, delete), and **Export** on the right.
- **Message box:** attach, **Act / Plan** mode, dictation and send.
- **Under the message box:** the chat's folders, Git level, a context gauge (used / window), **Compact**, tokens in and out, and the model. Click the model to change it; the menu also holds the Thinking setting.
- **Settings** has one page per tab. Skills, Projects, Scheduled tasks, Activity and Trash now live there.

## New
- **Working line:** while a reply runs, a line above the message box shows what it's doing (thinking, writing, reading a file, running a command, waiting for you) and for how long.
- **Compact:** summarize the older part of a chat now, keeping the latest messages word for word.
- **Background mode** (Settings > App & updates):
  - **Keep running in the tray** when you close the window;
  - **Start with Windows**, in the tray, so scheduled tasks run with the window closed.
- **Terminal:** command output appears as it runs, and a Stop button ends it.
- **Retries:** when Ollama is busy, has a server error, or the connection drops before a reply finishes, the step is tried again (after 3, 10 and 30 seconds) instead of ending the reply.

## Fixed
- **Escape stopped a running reply** when you closed the right-click menu or cancelled a rename. Escape now closes only the menu, dialog or rename; it stops a reply only from the message box.
- **Security: Git hooks.** The model could write a Git hook and have it run through a commit or a subtask's worktree, without "Run commands". Hooks no longer run for Git commands the app runs, and the file tools can't change files inside `.git`.
- **Reading a one-line file** (minified code, one-line JSON) returned all of it into the chat. It's now cut at the normal read limit.
- **Edit and resend** dropped the message's attached documents and its Plan mode.
- **Preview** showed the old location of a copied or moved file.
- **Scheduled tasks** that came due while the most replies were running left an empty chat and didn't run. They now wait and start when a reply finishes.
- **Activity** didn't show "wrapping up" or a subtask waiting for its parent's answer.
- **The chat didn't scroll** to a new reply when it started.

## Faster
- Long chats redraw only what changed, without re-rendering every message.
- The chat list re-reads only chats that changed.
- Quitting stops all replies at once.
- Replies that ended a while ago, and their live updates, are cleared from memory.

## Verification
- 183 automated tests (16 new).
- A live check in the window with a slow fake model: the working line, Escape with a menu open, every Settings tab, Compact, the model menu, terminal output and Stop, dark and light.
- The packaged-app smoke test.

---

# Open AY Desktop 1.14.2

A more reliable update check.

## Fixed
- **"Couldn't check for updates … 504".** The update check first opened GitHub's "latest release" page, which sometimes times out on GitHub's side even while downloads work.
  - The app now reads the newest release's update file straight from its download link.
  - If that fails, it tries the old way once. You only see an error when both fail.
- **Note:** the first update through the new route may download the whole installer instead of only the changed parts. Later updates are small again.

## Verification
- The update check against the live release, from a 1.14.0 starting point: the direct link, and the fallback when the direct link fails.
- 167 automated tests.
- The packaged-app smoke test.

---

# Open AY Desktop 1.14.1

Fixes for subtasks, renaming and right-click.

## Fixed
- **Subtasks didn't show in the sidebar while running.** The list now updates as soon as a subtask starts, under its parent and in its project.
- **Renaming a chat while it was replying didn't stick.** The running reply kept the old name and showed it again when you came back. Renames and settings changes now reach a running reply at once.
- **No right-click menu.** Right-clicking text or a text box now offers Cut, Copy, Paste and Select all, with spelling suggestions and Copy link.
- **You couldn't tell whether subtasks were wrapping up.**
  - A subtask's step now shows "wrapping up…" or "paused: low balance", and the sidebar dot changes to match.
  - When a subtask pauses for the balance, the parent stops waiting, reads its progress summary, and pauses too. Both continue when you top up.
- **Expanding a running subtask showed an empty box.** It now says the subtask works in its own chat and that its answer appears when it finishes.

## New
- **Rename in place:**
  - double-click a chat's name in the header or the sidebar;
  - or right-click it in the sidebar for Open, Rename and Delete.

## Verification
- 167 automated tests (3 new).
- A live check in the window: two subtasks under a parent in a project, one pausing for low balance, renames while running.
- The packaged-app smoke test.

---

# Open AY Desktop 1.14.0

Parent chats and their subtasks can now message each other while they work.

## Messages between a chat and its subtasks
- **A subtask can ask a question** with `message_parent` (wait for a reply) and pause until the parent chat answers. It can also send a short progress note.
- **The parent answers or follows up** with `message_subtask`:
  - a running subtask reads the message before its next step;
  - a finished subtask continues with it as a new reply, so the parent can ask for more without starting over.
- **`wait_subtasks`** gets results and messages. A parent waiting for its subtasks stops waiting as soon as one asks something, so its model can answer at once.
- **In the window:**
  - In the parent, the steps show as "Messaged subtask" and "Waited for subtasks", with an Open button.
  - In the subtask chat, the parent's messages show as "From the parent chat", and a subtask waiting for an answer shows "waiting for the parent…".
- **Safety:**
  - Messages count as coming from the other chat, never from you.
  - Reading web content on either side marks the other side untrusted, in both directions.
  - At most 30 messages per reply, 8,000 characters each.
  - A reply that ends with unread subtasks is reminded twice to read them; any still running when it ends are stopped.

## Also
- Tool descriptions are shorter, so each request stays under its size budget with the new tools.
- "Read skill file" steps now have their own name and icon.

## Verification
- 164 automated tests (7 new). One caught a race, now fixed: a message sent just as a subtask finished could be lost.
- A real run on gpt-oss:120b: a subtask asked which metal the shop uses, the parent answered, and the answer showed up in the subtask's tagline.
- A check of both chats in the window, and the packaged-app smoke test.

---

# Open AY Desktop 1.13.1

Stop now always works.

## Fixed
- **Stop did nothing while a step was stuck.** It only took effect between steps, so a step that never finished (like the Git status hang fixed in 1.13.0) kept the chat busy for good.
  - Git and GitHub CLI calls now end within a fraction of a second when you press Stop.
  - If any step is still busy 3 seconds after Stop, the reply is ended anyway. The step shows "Stopped: this step didn't finish", the chat is saved, and you can send a new message straight away. A late result from that step is ignored.

## Verification
- 157 automated tests (4 new). The stuck-step test fails without the fix.
- A live check in the window: a Git status step that never returns, ended with Stop.
- The packaged-app smoke test.

---

# Open AY Desktop 1.13.0

Skill folders in the open Agent Skills format, a fix for Git steps that never finished, and a new icon set.

## Skill folders
- **Import a whole skill**, the same format Claude, Codex, Cursor and others use:
  - **Import folder** takes a folder with a `SKILL.md`, or a repository that holds one;
  - **Import file** takes a `.skill` or `.zip` package.
- **The skill is copied** into the app's data, so it keeps working if the original moves. **Update from folder** copies it again after you change the original.
- **Chats read its files on demand** with a new read-only `read_skill_file` tool, so long reference files cost nothing until they're needed. It also works in plan mode.
- **Scripts run only through Run commands**, under the usual approval rules.
- **Imports are checked:** unsafe paths and links are refused, sizes are limited, and the app asks before replacing a skill.
- Descriptions can be up to 1,024 characters.

## Fixed
- **Git steps could hang forever** (for example "Git status ... running"). Git inherited an input the app keeps open, which can block Git for Windows at start-up. When the 60-second limit stopped Git's launcher, the real Git process kept running, so the reply never continued.
  - Git and the GitHub CLI no longer get that input.
  - A time limit now stops the whole process tree.
- **Skills window:** the file list no longer overlaps the instructions.

## New look
- All 41 icons redrawn on one grid, with a soft fill that strengthens on hover, selection and badges.

## Verification
- 153 automated tests (17 new). The new Git test reproduces the hang without the fix.
- `python verify_static.py`.
- A real chat on gpt-oss:120b with AYS imported as a skill folder: it read the right reference file and cited 10 real rule IDs.

---

# Open AY Desktop 1.12.0

Fewer tokens and lower cost, projects and memory, plan mode, undo, documents, scheduled tasks, and in-app updates.

## Saving tokens
- **Requests that reuse Ollama's cache.** The start of every request stays identical between steps and turns, so Ollama bills it at the cached-input rate (often about a tenth of normal input). The usage line shows the share of each chat's input that came from the cache.
- **Context budget** (Settings > Usage & context): Full (default), Balanced 64k or Economy 32k.
  - Past the budget, older turns are replaced by a summary, with your own earlier messages kept next to it, shortened and in order.
  - The summary stays the same until the next one, so the cache keeps working.
- **Shorter tool definitions:** 21% smaller for the same tools.
- **Smaller tool results:**
  - `read_file` reads 800 lines (about 28,000 characters) at a time and says where to continue;
  - one result in the current reply is capped at 30,000 characters, down from 60,000;
  - Git output is capped at 30,000 characters.
- **Pictures shrink to at most 1600 px** before sending.
- **Fixed:**
  - the size estimate now counts tool definitions and instructions, so requests no longer overflow the window;
  - garbled characters in the web-citation instruction and the saved-key hint.

## Quality check (old 1.11 vs new, real requests on gpt-oss:120b and two vision models, 3 runs each)
- **40k-token conversation, forced to summarize (Economy):**
  - 12 of 12 facts recalled and the order of events kept, the same as 1.11;
  - 23% fewer input tokens per run.
  - With caching, the one-off cost of writing a summary is only repaid over later turns, which is why Full is the default.
- **Finding a function in a 1,900-line file:** the same accuracy, with 67% fewer input tokens and half the cost.
- **Fixing a bug with edit tools:** 3 of 3 correct (1.11: 2 of 3), with 11% fewer input tokens.
- **Reading an invoice screenshot:**
  - correct in every run on both models;
  - 69% fewer image tokens on a model that bills by resolution;
  - no change on gemma4, which uses a fixed image size.

## New
- **Projects:** chats share folders, starting permissions, skills, instructions and memory.
- **Memory:**
  - global and per-project notes the model can save when you ask, and that you can edit;
  - a chat that has read web content asks before saving.
- **Instruction files:** AGENTS.md, CLAUDE.md or .openay/instructions.md in a shared folder are followed.
- **Plan mode:** a plan first, with read-only tools enforced by the server, then "Carry out this plan".
- **Undo changes:** under a reply, puts back the files it changed (edits, new files, moves, deletions). Later edits are respected, and what undo replaces goes to Trash.
- **Documents:** attach PDF, Word, Excel, PowerPoint, CSV, JSON, code and text files.
- **Isolated subtasks:** helpers can work in their own Git worktree and branch. The new `git_merge` tool brings their work in after you approve.
- **Scheduled tasks:** daily, weekdays, weekly, every few hours, or once.
- **Terminal and preview panel** (Ctrl+J):
  - run your own commands in a shared folder;
  - preview files the chat changed (HTML runs sandboxed).
- **Full-text search** across all chats' messages, jumping to the match.
- **Export** a chat as Markdown.
- **Quick entry:** Ctrl+Alt+Space opens a new chat from anywhere.
- **Dictation:** Windows speech recognition, offline.
- **Software updates in Settings:**
  - the installed app checks GitHub Releases, downloads in the background and installs on restart;
  - you can turn automatic installs off.
  - This is the first version with the updater: moving from 1.11 needs one manual install.

## Verification
- 136 automated tests (24 new).
- `python verify_static.py`.
- The A/B quality runs above.
- A real end-to-end run of plan mode, a document and memory across chats.
- A live check of every new screen.
- The packaged-app smoke test.

---

# Open AY Desktop 1.11.0

New look: the Graphite redesign.

## New
- **Graphite theme:**
  - graphite surfaces with one periwinkle accent; amber and red are kept for status;
  - a matching light theme that follows the Windows setting;
  - line icons everywhere instead of emoji.
- **Sidebar:**
  - chats grouped by Today, Yesterday, This week and Earlier;
  - pulsing, breathing or still dots for running, paused and waiting chats;
  - your Ollama balance as a coloured badge (click to check now).
- **Header:** shows the chat's model, folders and Git level under its title.
- **New chat screen:** three starting suggestions.
- **Messages:**
  - tool steps are cards with an icon per tool;
  - approval cards show the kind of risk at a glance;
  - new messages slide in;
  - live thinking shimmers.
- **Review file edit:** shows a coloured diff with added and removed line counts, plus a Changes / Full file switch.
- **Session settings:** opens as a drawer, grouped into Files, Model, Tools and Skills, with switches.
- **Settings:** has a section list, a balance meter, and a clearer API key state.
- **Activity:** shows totals for running, waiting, replies at once and tokens today.
- **Trash:** groups items by day.
- **Skills:** gives each skill a coloured monogram and shows a character count.
- **Motion:**
  - presses shrink slightly;
  - dialogs rise in and the session drawer slides in;
  - the sidebar collapses smoothly;
  - with Windows animations turned off, everything is instant.
- **New app icon** (an arch with two eyes) and a simplified tray icon.

## Unchanged
- Every permission, approval and server check works as before. This release changes only the interface files and the icons.

## Removed
- The legacy `Ollama Desk.cmd` launcher from before the rename. Use `Open AY Desktop.cmd` or the installed app.

## Verification
- 112 automated tests.
- `python verify_static.py`.
- A live check of every screen in dark, light and phone width on demo data.
- A simulated reply streaming in.

---

# Open AY Desktop 1.10.0

Running commands (completes Phase 5).

## New
- **Allow running commands** session permission (off by default). The model can run one command at a time in a shared folder with Allow edits and read its output.
- **Allowed commands** in Settings: entries match exactly, or with extra arguments when they end in ` *`. These run without asking. Anything else needs your approval, and so does every command in a chat that has read web content.
- **Safety:**
  - Commands run without a shell, and arguments that are shell operators are refused.
  - `.cmd`/`.bat` programs refuse cmd.exe special characters in their arguments.
  - The Ollama API key is removed from the command's environment.
  - The time limit and Stop end the whole process tree.
  - Subtasks never get commands.

## Verification
- 112 automated tests (11 new, running real processes) on Python 3.12 and the bundled 3.15.2.
- `python verify_static.py`.
- A live run of an allowed command in the app.

---

# Open AY Desktop 1.9.0

Git and GitHub tools (Phase 5).

## New
- **Git and GitHub** session setting with four levels: Off, Read only, Read and commit, and Read, commit, push and pull requests.
  - **Reading** (status, diff, log, show, branches, open pull requests) runs automatically.
  - **Every commit** opens a review with the message, files and full diff.
  - **Every push and pull request** needs your approval, and force-push is never offered.
- **Safety:**
  - Git works only on repositories whose top folder is shared with the chat.
  - Git runs without a shell, and names can't be read as extra options.
  - External diff programs and filesystem-monitor hooks are disabled.
  - Commits and pushes are refused if anything changed after you reviewed them.
  - Subtasks get read-only Git at most.
- Your Git identity and Git and GitHub CLI sign-in are used as they are, and the app never sees credentials.

## Verification
- 101 automated tests (11 new, on real temporary repositories with a local bare repository as the remote), on Python 3.12 and on the bundled 3.15.2.
- `python verify_static.py`, which now also checks that every app module ships in the installer.

---

# Open AY Desktop 1.8.0

The installer includes Python.

## New
- **No Python needed on the PC:** the Windows installer bundles the official embeddable Python 3.15.2 from python.org. The build downloads it once, verifies it against a pinned SHA-256, and the installed app uses it automatically. `OPENAY_PYTHON` can still point at a different Python.
- `npm run dist` fetches Python as part of the build, and `npm run fetch-python` fetches it on its own.

## Fixes
- `server.py` now adds its own folder to Python's import path, which the embeddable Python doesn't do.

## Verification
- 90 automated tests on Python 3.12 and on the bundled Python 3.15.2.
- HTTPS to ollama.com from the bundled Python.
- A smoke run of the packaged app with no Python on the PATH.
- `python verify_static.py`, which also checks that the Python download is pinned and bundled.

---

# Open AY Desktop 1.7.0

Electron desktop app.

## New
- **Electron app:**
  - Start with `npm start` or `Open AY Desktop (Electron).cmd`. Electron runs the local Python server and shows the app in its own window.
  - Closing the window while replies run lets you keep them running from the system tray, or stop them and quit.
  - Only one copy runs at a time.
  - **Browse…** uses Windows' native folder picker.
- **Locked-down window:** the page has no access to Node, navigation stays inside the app, and https links open in your browser.
- **Windows installer:** `npm run dist`. The installed app keeps its data in `%APPDATA%\Open AY Desktop\data`. It still needs Python 3.9+ on the PC.
- The server stops by itself if the Electron app quits or crashes.

## Verification
90 automated tests, `python verify_static.py` (now also checking the Electron window settings and the packaged files), `npm run smoke`, and a smoke run of the built app.

---

# Open AY Desktop 1.6.0

Balance guard.

## New
- **Your Ollama balance** (plan allowance plus purchased credits) shows under the message box, read from Ollama's balance API.
- **Pause at balance** (Settings, default $1): at or below it, every running reply finishes its current step, writes a short wrap-up of what it did and what's left, and pauses. New messages also pause. Replies continue by themselves once the balance is above the pause amount again. **Check now** re-checks at once.
- Paused chats show a banner, a red sidebar dot, and "paused: low balance" in Activity.

## Verification
90 automated tests (7 new for reading the balance, wrap-up, pause, resume and stop) and `python verify_static.py`.

---

# Open AY Desktop 1.5.0

Subtasks, limits and an Activity view (Phase 4).

## New
- **Subtasks:** with "Allow subtasks" on, the model can start helper chats that run in parallel and report their answers back. Subtasks appear under their parent in the sidebar, never get more permission than the parent, and are stopped and deleted with it.
- **Limits** in Settings: tokens per day, tokens per chat, and how many replies can run at the same time. They're checked before every model request.
- **Activity (⚡):** every running reply in every chat, with Open and Stop, plus recently finished replies.

## Fixes
- A finished reply could briefly count as finished before its final save, so a reply started at that moment could lose the previous reply's last message. It now saves first.

## Verification
83 automated tests (10 new for subtasks, limits and activity) and `python verify_static.py`.

---

# Open AY Desktop 1.4.0

Web search, skills, and protection against untrusted content (Phase 3).

## New
- **Web search:** turn it on per chat in Session settings. The model can search the web and read pages through Ollama's web API, and is told to cite its sources as clickable links.
- **Untrusted-content protection:** once a chat reads web content, it shows a ⚠ Web content badge. From then on, in that chat:
  - every file change needs your review, even with auto-edit;
  - every create, copy, move, delete or restore needs your approval;
  - model switching set to "allowed" asks you first.

  Web results are wrapped in markers a page can't fake. **Trust this chat again** in Session settings clears the flag.
- **Link approval:** the model can open a page without asking only if its address came from a web search or from your messages. Any other address, including links found on a page, needs your approval, because addresses can carry information out of the chat.
- **Skills (✦ in the sidebar):** write or import Markdown instructions and turn them on per chat. They're added to the chat's system instructions and can't change permissions.
- **A general approval dialog** for actions that need your approval, with Later, Deny and Allow.

## Verification
73 automated tests (14 new for web, untrusted rules, link approval, gated actions and skills) and `python verify_static.py`.

---

# Open AY Desktop 1.3.0

Replies run on the server, with context management and usage (Phase 2).

## New
- **Replies run on the local server**, not in the window. Switching chats or closing the window no longer stops a reply, and several chats can reply at once. The sidebar shows which chats are replying (pulsing dot) or waiting for your approval (amber dot).
- **Approvals wait on the server** for up to 30 minutes. **Later** closes the dialog without answering, and the card in the chat reopens it. You can answer from any open window.
- **Context budget:** each request is fitted into the model's context window. Older tool results are shortened, and if needed the oldest whole turns are left out, with a note to the model. The saved history is never changed.
- **Usage line** under the message box: context used in the last request, this chat's tokens, and today's tokens and requests. Daily totals are kept in `data/usage.json`.
- **Edit and resend** shows a banner and replaces the earlier message only when you send.

## Changed
- The window talks to the server through `/api/chats`, `/api/runs` and a live event stream (`/api/chats/<id>/events`). The old `/api/chat` and `/api/tool*` endpoints are gone.
- Escape on an approval dialog now means **Later**, not **Deny**.
- The background server stays alive while any reply is running, even with no window open.

## Verification
59 automated tests (16 new for runs, events, approvals, stop and the context budget) and `python verify_static.py`.

---

# Open AY Desktop 1.2.0

Undoable edits, a Trash screen and server-owned permissions (Phase 1.6).

## New
- **`edit_file`:** the model can change part of a file by exact find-and-replace, with the same full-diff review as `write_file`, instead of resending the whole file. Line endings are preserved.
- **Every overwrite can be undone:** before an existing file is edited or replaced, its previous content is kept in the app Trash, in both review and auto-edit mode.
- **Trash screen** (🗑 in the sidebar): see deleted files and earlier versions, restore them, delete single items, or empty the trash. Restoring a version keeps the current file, so restores can be undone too.

## Changed
- A chat's folders, permissions and name are now server-owned. Normal chat saves can't change them, even from an outdated copy in the window. They change only through validated endpoints.
- A reviewed write is refused if the file changed on disk after you reviewed it, instead of overwriting the newer content.

## Fixes
- An unexpected server error now returns a readable error instead of dropping the connection.

## Verification
43 automated tests and `python verify_static.py`.

---

# Open AY Desktop 1.1.0

Session permissions, file management and model switching (Phase 1).

## New
- **Session settings** per chat: rename, file-editing mode (review or auto-edit), file/folder creation, copy, move and rename, recoverable deletion with restore, and model control.
- **Model switching on request:** ask in chat to switch models. By default an in-app dialog asks you to approve each change. The server checks that the model exists and that it can handle the chat's images, and warns when it doesn't support tools.

## Fixes
- Models without tool support could not chat at all. The app now retries without tools, and that retry carries no tools.
- Renaming a session or changing its model could hang the app and leave the chat locked.
- The Session settings dialog was always shown inside the page instead of opening as a dialog.
- Chats without saved permissions no longer gain file management and deletion just because a folder allows edits.
- The model only switches when you explicitly ask. It no longer switches on its own initiative.

## Verification
34 automated tests (`python -m unittest discover -s tests -v`) and `python verify_static.py`.

---

# Open AY Desktop 1.0.0

Initial Open AY Desktop release, renamed from Ollama Desk.

## Highlights
- Direct Ollama Cloud chat with an API key.
- Simplified Settings: Ollama API Key, Default Model, and Check for Updates.
- GitHub Releases update notifications for `arothm/OpenAYDesktop`.
- Streaming chat, reasoning controls, Markdown/code highlighting, image attachments, and chat history.
- Per-chat folder access with server-enforced permissions.
- Full-content and full-diff review before every file write.
- Atomic chat persistence and ordered browser saves.
- Locally served frontend libraries with a fixed-version cache.

## Running
Requires Python 3 and Edge or Chrome. Launch `Open AY Desktop.cmd`.

## Update checking
This is the baseline version. Publishing a later GitHub Release with a higher `vX.Y.Z` tag will cause the update checker to offer its release page. The app never installs files automatically.

## Verification status
Static source and regression-test coverage is included. Runtime/browser verification must be performed after restarting the backend; the file-editing environment used for implementation cannot execute Python or launch a browser.
