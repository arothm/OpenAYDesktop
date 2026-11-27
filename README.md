# Open AY Desktop

A lightweight desktop chat app for **Ollama Cloud**. It runs as an **Electron** desktop app, with a local Python 3 server (standard library only) doing the work. It can also still open in an Edge or Chrome app window without Electron. There is no frontend framework or build step for the interface itself.

- **Application:** Open AY Desktop
- **Version:** 1.12.0
- **Repository:** https://github.com/arothm/OpenAYDesktop
- **Ollama connection:** direct to https://ollama.com, using an Ollama API key

The application was previously named Ollama Desk. Existing chats, folder grants, cached browser data, and API keys remain in the existing `data/` directory. The first time configuration is read, the former last-selected model is preserved as the explicit default model. Legacy local-mode, system-prompt, and temperature options are ignored and removed when settings are next saved.

## Run
### Electron app (recommended)
- **First time:** open a terminal in this folder and run `npm install`.
  - On npm 11 or newer, Electron's binary isn't downloaded automatically. If `node_modules\electron\dist\electron.exe` is missing afterwards, run `node node_modules\electron\install.js` once.
- **Start:** double-click **`Open AY Desktop (Electron).cmd`**, or run `npm start`. It uses this folder's `data/`, so your existing chats and settings carry over.
- **Closing the window:**
  - with no replies running, it quits;
  - with replies running, it asks whether to **keep them running in the background**, from a system-tray icon, or **stop them and quit**.
- **Shutdown:** quitting shuts the local server down. If Electron itself crashes, the server notices and stops within a second.
- **One copy at a time:** starting the app again brings up the open window. In development it also reuses a server already running from this folder, so two servers never write to the same `data/`.
- **Locked-down window:** the page has no access to Node or the file system. Only Windows' own folder picker (Browse…) is exposed. Navigation stays inside the app, and https links open in your normal browser.
- **Startup check:** `npm run smoke` starts the app hidden, checks the window and server, and quits.

### Installer
`npm run dist` builds `dist\Open AY Desktop Setup <version>.exe`, an unsigned NSIS installer of about 127 MB. **It includes its own Python**, so the PC doesn't need Python installed:
- **Download:** `scripts/fetch-python.js` downloads the official Windows embeddable Python (currently 3.15.2) from python.org, about 14 MB. It caches it in `build/`.
- **Check:** it verifies the download against the SHA-256 pinned in the script, then unpacks it into the installer. To update Python, change `VERSION` and `SHA256` there.
- **Choice of Python:** the installed app uses its bundled Python first. You can point `OPENAY_PYTHON` at a different `python.exe` to override it, and a system Python is used only if the bundled one is missing.
- **Tested:** the full test suite also passes on the bundled Python (`build\python\python.exe -m unittest discover -s tests`).
- **Running from source:** `npm start` still uses the Python installed on your PC.

- **Data:** the installed app keeps its data in `%APPDATA%\Open AY Desktop\data`, not in Program Files. To bring existing chats and settings over, copy this folder's `data\config.json` and `data\chats\` into it.
- **Signing:** Windows SmartScreen may warn about the unsigned installer.

### Without Electron
- Double-click **`Open AY Desktop.cmd`**. The app opens in its own window. The background server stops about 3 minutes after the last window closes.
- To create or migrate Desktop and Start-menu shortcuts, run:

  ```powershell
  powershell -ExecutionPolicy Bypass -File ".\Create Shortcuts.ps1"
  ```

  This creates **Open AY Desktop** shortcuts and removes the old matching **Ollama Desk** shortcuts. It does not read or change chat/configuration data.
- To run only the server: `python server.py --no-window`, then open http://127.0.0.1:8765.

## Settings
The Settings dialog contains only:

1. **Ollama API Key** — a password field, saved-key indication, and Clear button. Leaving the field blank preserves the key; **Clear saved API key** takes effect when you click Save. Create a key at https://ollama.com/settings/keys. Alternatively, set the `OLLAMA_API_KEY` environment variable.
2. **Default Model** — populated from Ollama Cloud's available models. It applies to new chats. Existing chats retain their selected model, and the header picker can change the current chat's model.
3. **Limits**:
   - **Tokens per day** and **Tokens per chat** count tokens sent and generated, as Ollama reports them; 0 turns a limit off. When a limit is reached, replies stop with a message until you raise the limit.
   - **Replies at the same time** (default 4) caps how many replies, subtasks included, can run at once.

   - **Allowed commands**: see *Running commands* below.
   - **Pause at balance ($)** (default 1, 0 = off): see *Balance guard* below.

   The usage line under the message box shows your Ollama balance and today's tokens against the daily limit.
4. **Check for Updates** — checks the latest release at https://github.com/arothm/OpenAYDesktop/releases using GitHub's public API. It reports the installed version, update availability, no-published-release state, or a connection error. If an update exists, **Open Latest Release** opens that release page. Nothing is downloaded or installed automatically.

The app no longer supports local Ollama, a custom Ollama URL, or local model discovery. The Ollama desktop app is not required. The header still exposes the model picker and thinking/reasoning effort.

## Folder access
Folder access is set per chat. Click the folder icon next to the message box and add folders with **Browse…** or by pasting a path. Only that chat can use them, and new chats start with none. The model gets tools to list, search and read files in those folders and nowhere else.

The server—not the browser—loads folder permissions from the saved chat on every tool operation. It checks canonical paths, including `..`, symbolic links, and files found by recursive searches. Tool requests cannot grant themselves extra folders by supplying a folder list.

Tick **Allow edits** to let the model change text files. It has two tools for this:
- **`edit_file`** changes part of an existing file by exact find-and-replace. The text to replace must match the file exactly and be unique unless `replace_all` is set. Line endings are preserved. The model is told to prefer this for changes to existing files.
- **`write_file`** creates a new file or replaces a whole file.

Before any existing file is overwritten or edited, in either review or auto-edit mode, its previous content is kept in the app **Trash**. Every change can be undone from there.

In review mode, every change needs explicit approval:
- The dialog shows the **complete replacement content**, not a truncated preview.
- For an existing file, **Show full diff** displays a complete unified diff against the current file.
- The server issues a random, single-use approval token tied to that chat, canonical path, and exact content.
- The reply waits on the server for your answer, for up to 30 minutes, after which it counts as denied. Permissions are checked again immediately before writing.
- **Later** (or Escape) closes the dialog without answering. The card in the chat, and the amber dot in the sidebar, show that a reply is waiting, and **Review change** reopens the dialog. You can answer from any open window.
- Denying, cancelling, or changing an approval does not write the file.
- If the file changes on disk after you reviewed it, the approved write is refused instead of overwriting the newer content.

## Trash
The 🗑 button in the sidebar opens the Trash. It holds two kinds of items:
- **Deleted:** files or folders a session removed with recoverable deletion.
- **Earlier version:** the content a file had before a session overwrote or edited it.

**Restore** puts an item back. Restoring an earlier version keeps the current file in the Trash too, so a restore can itself be undone. A deleted item is only restored if nothing now exists at its original path. **Delete** and **Empty trash** remove items permanently, after a confirmation. Items are stored in `data/trash/` and stay until you remove them. Files over 20 MB are not versioned.

The number on the folder icon shows how many folders the current chat can access. Tool calling needs a compatible model; small models sometimes say they did something without actually calling the tool.

## Session settings
The **Session** button in the header opens settings for the current chat only. The server stores them with the chat and checks them on every tool call.

A chat's folders, permissions and name are **server-owned**. Normal chat saves from the window can't change them, even from an outdated copy. They change only through the validated `/api/session/permissions` and `/api/session/rename` endpoints. A new chat keeps the folders you picked before its first message, but always starts with the default permissions below.

- **Session name:** rename the chat. Its ID and history are kept.
- **File editing:** *Review every file write* (default) or *Auto-edit*. Auto-edit writes without a review dialog, and only inside folders with **Allow edits**.
- **File/folder creation, copy, move and rename:** off by default. Copy and move never overwrite an existing item.
- **Recoverable deletion:** off by default. Deleted items go to the app's private trash in `data/trash/` and can be restored by ID. Nothing is permanently deleted.
- **Session model control:**
  - *Ask me before each change* (default): when you ask in chat, for example "switch to gpt-oss:120b", an approval dialog shows the current model, the new model and the reason the model gave.
  - *Change when I ask in chat, without confirming*: switches without the dialog.
  - *Fixed*: the session can't change its model.

- **Web search and reading web pages:** off by default. See *Web search* below.
- **Skills for this chat:** tick the skills this chat should follow. See *Skills* below.
- **Git and GitHub:** off by default. See *Git and GitHub* below.
- **Allow running commands:** off by default. See *Running commands* below.
- **Allow subtasks:** off by default. See *Subtasks* below.

  The model is told to switch only when you explicitly ask. Before switching, the server checks that the model exists in Ollama Cloud. It refuses a model without image support if the chat contains images, and warns when the new model doesn't support tools. The new model answers from the next step of the same reply. Your default model in Settings is not changed.

Chats saved before these settings existed get the defaults above. A folder with **Allow edits** still allows reviewed writes, but file management and deletion stay off until you enable them.

## Web search
Turn on **Allow web search and reading web pages** in a chat's Session settings. The model gets two tools, which use Ollama's web API with your API key:
- **`web_search`** returns titles, addresses and snippets.
- **`web_fetch`** reads a page.

The model is told to cite the sources it uses as clickable Markdown links.

Web pages are written by strangers and can contain text aimed at steering the model. The app protects against that in four ways:
- **Results are marked as data.** Everything from the web is wrapped in clear "untrusted content" markers, which a page can't fake, and the model is told never to follow instructions inside them.
- **The chat becomes untrusted.** The first time a chat reads web content, a **⚠ Web content** badge appears in the header. From then on in that chat, no matter what its settings say:
  - every file write or edit needs your review, even with auto-edit;
  - model switching set to "allowed" asks you first;
  - every create, copy, move, delete or restore needs your approval.
- **Opening a page can need approval.** A page address can carry information out of the chat, for example file contents written into a link. So `web_fetch` opens a page without asking only if its address came from a web search in this chat or from your own messages. Links found on a page always need approval.
- **You decide when to trust it again.** In Session settings, the notice lists what the chat read, and **Trust this chat again** clears the flag. The web content stays in the conversation.

One gap remains: the model could still put private information into a search query. The model is told not to, but the app can't check it, so turn on web search only for chats where that risk is acceptable.

## Skills
A skill is a set of instructions you write once and turn on per chat, for example how to review code or the style to write in. Open **✦** in the sidebar to:
- create, edit or delete skills;
- import a Markdown file. An optional `name:` and `description:` header between `---` lines is read.

Turn skills on for a chat in its Session settings. Enabled skills are added to the chat's system instructions on every request, up to 30,000 characters in total and 20,000 per skill. A skill is only text: it can't run code, call tools by itself, or change what the chat is allowed to do. Skills are stored as Markdown files in `data/skills/`.

## Git and GitHub
The **Git and GitHub** session setting gives the model Git tools for repositories in the chat's shared folders. It has four levels:

| Level | Tools | Your involvement |
|---|---|---|
| Off (default) | none | |
| Read only | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branches`, `gh_pr_list` | none |
| Read and commit | adds `git_commit` | You review every commit: message, files and full diff. |
| Read, commit, push and pull requests | adds `git_push` and `gh_pr_create` | You approve every push (remote, branch and commits) and every pull request (title, branches and description). |

Safety rules, enforced by the server (`gittools.py`):
- **Whole repositories only.** A repository is usable only if its **top folder** is inside a shared folder, so sharing a subfolder doesn't open up the whole repository. Committing needs **Allow edits** on that folder.
- **No smuggled options.** Git runs without a shell. Branch, remote and commit names are checked so they can't be read as options, and file paths are passed after `--` and must stay inside the repository.
- **No hidden programs.** External diff programs, text conversion filters and `core.fsmonitor` are switched off, and Git and GitHub CLI prompts are disabled, so nothing can hang waiting for input.
- **Nothing changes after review.** If the files change after you've reviewed a commit, or the branch moves after you've reviewed a push or pull request, it's refused and nothing happens.
- **No force-push.** It isn't offered at all, and the model is told never to rewrite history.
- **Your credentials stay with Git.** Commits use your Git identity, and pushes and pull requests use your existing Git and GitHub CLI (`gh`) sign-in. The app never sees them.
- **Subtasks** get read-only Git at most.

Pull requests need the [GitHub CLI](https://cli.github.com) installed and signed in (`gh auth login`), and the branch pushed first.

## Running commands
With **Allow running commands** on, the model gets `run_command`: one command at a time, run in a shared folder with **Allow edits**, with its output returned.

**Allowed commands** (Settings) are one per line:
- `npm test` matches exactly `npm test`. The program name is compared without path or extension, so `npm.cmd test` also matches.
- `python -m pytest *` matches `python -m pytest` followed by any extra arguments.

A command on the list runs without asking. **Anything else needs your approval**, in a dialog showing the folder, the exact command and the time limit. **In a chat that has read web content, every command needs approval**, even ones on the list.

How commands run (`commands.py`):
- **No shell.** The line is split the way Windows splits it. Arguments that are shell operators (`&&`, `|`, `;`, `>`, `<`, …) are refused with a clear message, so the model can't expect pipes or chaining to work.
- **Batch-file guard.** Programs that are `.cmd`/`.bat` files, such as `npm`, are run by cmd.exe. Arguments containing characters cmd.exe treats specially (`& | < > ^ % ! "`) are refused for them.
- **Your API key stays out.** The Ollama API key is removed from the command's environment.
- **Time limit.** It defaults to 2 minutes, and the model can ask for up to 10. **Stop** or the time limit ends the command *and everything it started*.
- **Output.** The start and end are kept (about 60,000 characters), along with the exit code and duration.
- **Subtasks never get commands.**

**Important:** a command runs with your Windows account, so it can read or change anything you can, not just the shared folders. Keep the allowed list to commands you'd be happy to run without looking, such as tests, builds and linters.

## Subtasks
With **Allow subtasks** on, the model can hand self-contained pieces of work to helper chats with `start_subtask`:
- **Parallel.** Subtasks requested in the same reply run at the same time. The chat waits for them, then gets each one's final answer and continues.
- **Visible.** Each subtask is a normal chat, shown under its parent in the sidebar and marked "subtask of …". Its card in the parent chat has an **Open** button, and its approvals appear in its own chat, with an amber dot in the sidebar.
- **Never more permission than its parent.** A subtask gets the parent's folders and skills and at most its effective permissions. Its model is fixed, and it can't start subtasks of its own. If the parent has read web content, the subtask starts untrusted. If a subtask reads web content, the parent becomes untrusted when the result comes back.
- **Bounded.** At most 6 subtasks per reply, and they count toward the limits in Settings.
- **Stopped and deleted with the parent.** Stopping the parent stops its subtasks, and deleting the parent deletes them.

## Balance guard
The app reads your Ollama account balance from `GET https://ollama.com/api/balance`, using your API key. The balance is your remaining plan allowance plus purchased credits, and it's shown in the usage line under the message box, in amber when it's within $0.50 of your pause amount and in red at or below it. The server re-checks it before every model request. The value is cached for 30 seconds and shared by all replies, because Ollama allows about 10 balance checks a minute.

When the balance is **at or below the pause amount** set in Settings (default $1):
- **Every running reply wraps up.** It finishes the step it's on. A reply that has already done some work then makes **one last short request without tools**, asking the model to summarise what it did and what's left. That summary is marked *wrap-up*. A reply that hasn't started yet doesn't spend anything.
- **The reply pauses.** The chat shows a banner with **Recharge…**, **Check now** and **Stop**, the sidebar shows a red dot, and Activity lists it as *paused: low balance*. New messages also pause until the balance is back up.
- **It continues by itself.** It re-checks about once a minute, or at once when you click **Check now**. When the balance is above the pause amount again, it adds an automatic note ("continue the work from where you stopped") and carries on.

If the balance can't be read, for example because Ollama changes its API, replies keep working and the usage line says *Balance unavailable*. The wrap-up request itself costs a little, so the pause amount works as a reserve, not a hard cap.

## Activity
**⚡** in the sidebar lists every running reply across all chats, subtasks included. For each one it shows the status (running, or waiting for you), model, time, steps and tokens, with **Open** and **Stop** buttons. Below that are the most recently finished replies and how they ended (done, stopped, or error).

## Replies run on the server
Each reply runs on the app's local server (`runs.py`): the model answers, calls tools, gets their results, and continues, up to 25 steps. The window shows it live through an event stream. As a result:
- **Switching chats or closing the window doesn't stop a reply.** Reopen the chat to see where it got to. The background server keeps running while any reply is in progress.
- **Several chats can reply at the same time.** A pulsing dot in the sidebar marks chats that are replying, and an amber dot marks chats waiting for your approval.
- **Stop** (the ■ button, or Escape) ends the current chat's reply, including one that's waiting for approval.
- **Edit and resend** shows a banner. The earlier message and everything after it are replaced only when you send.
- While a reply runs, its chat's messages and model can't be changed from elsewhere. Folders and permissions can still change, and they take effect at the next tool call.

## Context, tokens and cost
Ollama Cloud bills per token, and input it has seen before at the start of a request ("cached input") costs far less, often about a tenth. So the app keeps the start of every request the same from step to step and turn to turn: instructions and tools first, then the history, and anything that changes per request at the end. Ollama reports how much was cached, and the usage line shows it.

Before each request the server fits the conversation into the **context budget** (Settings > Usage & context):
- **Full** (default) uses the model's whole window. **Balanced** (64k tokens) and **Economy** (32k) send less per request; they suit models without cached-input pricing.
- Everything sent counts: messages, tool definitions and instructions.
- Tool results from earlier turns are shortened, and the model can call the tool again.
- Past the budget, the older turns are replaced by a summary the chat's model writes, kept until the next one. Your own earlier messages stay next to it, shortened and in order, so details you gave aren't lost. A "View summary" divider shows where it starts. If a summary can't be made, the oldest turns are left out instead.
- A file is read up to 800 lines (about 28,000 characters) at a time, with a note saying where to continue.
- Pictures are shrunk to at most 1600 pixels on the long side before sending. Models that bill by resolution use far fewer tokens: one test screenshot went from 5,069 to 1,579 tokens with the same answer.

The line under the message box shows how full the context was, the share of this chat's input that came from the cache, and today's totals (kept in `data/usage.json` for 90 days).

## Projects and memory
- **Projects** (the switcher under the search box, or Manage projects) group chats. A project has folders, starting permissions, skills, written instructions and its own memory. New chats in the project start with all of them.
- **Memory**: notes chats can use later. Global memory (Settings > Memory) applies everywhere; project memory only in that project. Ask the model to remember something and it saves a note; you can edit or delete notes at any time. A chat that has read web content asks you before saving, so a web page can't plant lasting instructions.
- **Instruction files**: `AGENTS.md`, `CLAUDE.md` or `.openay/instructions.md` at the top of a shared folder are followed by the chat (Session settings can turn this off). They can't change permissions.

## Plan mode, undo and documents
- **Plan** (next to the folder button) asks for a plan first: the model only gets tools that read, and the server refuses anything else. Then **Carry out this plan**, or reply with changes.
- **Undo changes** under a reply puts back every file it created, edited, moved or deleted. Files changed again afterwards are skipped and listed, and what undo replaces goes to the Trash, so an undo can be undone. Commands and Git actions can't be undone and are listed as such.
- **Documents**: attach PDF, Word, Excel, PowerPoint, CSV, JSON, code or text files with the paperclip, by pasting, or by dropping. Their text goes to the model with your message. Scanned PDFs have no text: attach page images instead.

## Scheduled tasks, terminal and preview
- **Scheduled tasks** (clock icon) start a new chat with a prompt every day, on weekdays, weekly, every few hours, or once, in a project if you like. They run while the app is open or in the tray. A run missed while the app was closed happens once when it next starts.
- **Terminal and preview** (Ctrl+J or the panel button): run commands you type in a writable shared folder, and see the commands the model ran. Preview shows files the chat changed: HTML pages run in a sandbox with no access to the app, Markdown is rendered, and images and text are shown.
- **Search** under New chat looks through every chat's messages, not just titles, and jumps to the matching message.
- **Export** (header button) saves the chat as Markdown.
- **Quick entry**: Ctrl+Alt+Space opens the app on a new chat from anywhere (Settings > App & updates).
- **Dictation** (microphone button) uses Windows speech recognition, offline. It needs a speech language installed and microphone access for desktop apps.

## Software updates
The installed app checks GitHub Releases at start and every 6 hours (Settings > App & updates > Check now). With **Install updates automatically** on, a new version downloads in the background and installs when the app closes. Otherwise Settings offers Download and Restart. Chats and settings are kept. Version 1.12.0 is the first with the updater, so moving from 1.11 or older needs one manual install.

## Features
Streaming replies, Markdown and code highlighting, collapsible thinking/reasoning panels, image and document attachments, stop, regenerate, edit-and-resend, full-text search, projects, memory, plan mode, undo, scheduled tasks, a terminal and preview panel, export, and server-approved folder tools.

**Isolated subtasks**: with Git set to commit or more and subtasks on, the model can start a helper with `isolated: true`. The helper works in its own Git worktree and branch (`openay/<chat id>`), so several helpers can change code at once without touching your working tree. Its work is committed on that branch when it finishes, and `git_merge` brings it in after you approve. Deleting the helper chat removes the worktree; the branch stays.

## Persistence and reliability
- Chats and configuration are written atomically with unique temporary files.
- Per-chat server locks protect reads, writes, and deletion.
- The server owns each chat's messages. A running reply saves after every tool step and when it ends, keeping folders, permissions and name as stored on disk.
- Live-only state, such as a message still streaming or a pending approval, is never written to disk.
- Deleting a chat stops its reply first.
- An unexpected server error returns a readable error instead of dropping the connection.

## Local interface libraries
The browser loads Marked, DOMPurify, Highlight.js, and both syntax themes from this app's own `/vendor/` routes, not from CDN script tags. The server permits only five fixed, versioned assets: Marked 12.0.2, DOMPurify 3.1.6, Highlight.js 11.9.0, and its two 11.9.0 themes.

On the first request for each asset, the server downloads its fixed HTTPS URL into `vendor/`. After all five have been cached successfully, the complete interface works offline without contacting cdnjs. If a first download fails, plain-text Markdown fallback remains available. See `vendor/README.md` for upstream source URLs and licensing information.

## Publishing a release for the updater
1. Update `APP_VERSION` in `server.py`, `version` in `package.json` (`verify_static.py` checks they match), the version shown here, and `RELEASE_NOTES.md` (the new version's notes first).
2. Commit, tag `vX.Y.Z`, and push the branch and the tag.
3. Run `npm run release`. It builds the installer and publishes a GitHub Release with the installer, its `.blockmap`, and `latest.yml` (what installed apps read), using the GitHub CLI.
4. Installed apps find the new version within 6 hours, or at once with Check now. A browser window without Electron only links to the release page.

## Source layout
- `server.py` — HTTP API, storage, folder tools, write approvals, model info, usage totals, update checker, local assets, and launcher.
- `runs.py` — server-side replies: the model/tool loop, live events, approvals, stop, and per-chat usage.
- `context_budget.py` — the context budget, summaries (compaction), cache-friendly requests and attached documents' text.
- `projects.py` — projects, memory notes and instruction files.
- `undo.py` — the journal behind "Undo changes".
- `documents.py` — text from PDF, Word, Excel, PowerPoint and text files.
- `scheduler.py` — scheduled tasks.
- `worktrees.py` — isolated subtasks in Git worktrees.
- `capabilities.py` — session permission policy, file management, trash and versions.
- `session_actions.py` — session renaming.
- `web.py` — web search and page reading, with untrusted-content markers.
- `commands.py` — running commands: parsing, the allowed list, the batch-file guard, time limits and Stop.
- `gittools.py` — Git and GitHub tools: repository checks, the safe command runner, and commit, push and pull request reviews.
- `skills.py` — skill files: storage, validation and the per-chat prompt.
- `index.html` — application and dialog markup.
- `styles.css` — the Graphite look: colour tokens for dark and light (it follows the Windows setting), components and motion. The page's security policy blocks inline styles, so every look is a class here.
- `app.js` — the window: live event stream, rendering, approvals, sessions, trash, settings, plan mode, undo and documents.
- `features.js` — projects, scheduled tasks, the terminal and preview panel, export, dictation and quick entry.
- `electron/` — the Electron shell: `main.js` (server lifecycle, window, tray, quit), `preload.js` (folder picker, export, dictation, quick entry and updates), `icon.png` (app icon), `tray.png` (simplified tray icon).
- `package.json` — Electron and electron-builder setup (`npm start`, `npm run smoke`, `npm run dist`, `npm run release`).
- `scripts/fetch-python.js` — downloads and verifies the embeddable Python that the installer bundles.
- `scripts/release.js` — publishes the built installer as a GitHub Release for the updater (`npm run release`).
- `tests/` — isolated standard-library tests (`test_server.py`, `test_capabilities.py`, `test_runs.py`, `test_phase3.py`, `test_phase4.py`, `test_balance.py`, `test_git.py`, `test_commands.py`, `test_tokens.py`, `test_features.py`).
- `verify_static.py` — optional static consistency checks.
- `backups/pre-approved-changes/` — original source copies from before the first approved change set.

## Verification
Run the isolated regression tests with:

```bat
python -m unittest discover -s tests -v
python verify_static.py
```

The tests use temporary folders and a temporary local HTTP server. They do not use the app's real chats or configuration. Some symbolic-link tests may be skipped on Windows when the current account cannot create symbolic links.

## Data
Chats and settings, including the API key, are saved under `data/` next to `server.py`, or in `%APPDATA%\Open AY Desktop\data` for the installed app. The `OPENAY_DATA_DIR` environment variable overrides the location. The server listens only on 127.0.0.1, checks request origins, sets a restrictive content-security policy, and rejects requests from other websites.
