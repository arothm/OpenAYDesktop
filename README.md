# Open AY Desktop

A lightweight desktop chat app for **Ollama Cloud**. It runs as an **Electron** desktop app, with a local Python 3 server (standard library only) doing the work. It can also still open in an Edge or Chrome app window without Electron. There is no frontend framework or build step for the interface itself.

- **Application:** Open AY Desktop
- **Version:** 1.17.0
- **Repository:** https://github.com/arothm/OpenAYDesktop
- **Ollama connection:** direct to https://ollama.com, using an Ollama API key

The application was previously named Ollama Desk. Existing chats, folder grants, cached browser data, and API keys remain in the existing `data/` directory. The first time configuration is read, the former last-selected model is preserved as the explicit default model. Legacy local-mode, system-prompt, and temperature options are ignored and removed when settings are next saved.

## Run
### Electron app (recommended)
- **First time:** open a terminal in this folder and run `npm install`.
  - On npm 11 or newer, Electron's binary isn't downloaded automatically. If `node_modules\electron\dist\electron.exe` is missing afterwards, run `node node_modules\electron\install.js` once.
- **Start:** double-click **`Open AY Desktop (Electron).cmd`**, or run `npm start`. It uses this folder's `data/`, so your existing chats and settings carry over.
- **Closing the window:**
  - with **Keep running in the tray** on (Settings > App & updates), it hides to the system tray: replies and scheduled tasks keep running, and the tray icon's menu quits;
  - otherwise, with no replies running, it quits;
  - with replies running, it asks whether to **keep them running in the background**, from a system-tray icon, or **stop them and quit**.
- **Start with Windows** (Settings > App & updates) opens the app in the tray when you sign in, so scheduled tasks run without opening the window.
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
2. **Default Model** — populated from Ollama Cloud's available models. It applies to new chats. Existing chats retain their selected model, and the model picker under the message box changes the current chat's model.
3. **Limits**:
   - **Tokens per day** and **Tokens per chat** count tokens sent and generated, as Ollama reports them; 0 turns a limit off. When a limit is reached, replies stop with a message until you raise the limit.
   - **Replies at the same time** (default 4) caps how many replies, subtasks included, can run at once.
   - **Steps per reply** (default 100; 0 = no limit) is how many requests to the model one reply can make. A reply that reaches it pauses with a **Continue** button.

   - **Allowed commands**: see *Running commands* below.
   - **Pause at balance ($)** (default 1, 0 = off): see *Balance guard* below.

   The sidebar shows your Ollama balance; Settings > Usage & limits shows today's tokens against the daily limit.
4. **Check for Updates** — checks the latest release at https://github.com/arothm/OpenAYDesktop/releases using GitHub's public API. It reports the installed version, update availability, no-published-release state, or a connection error. If an update exists, **Open Latest Release** opens that release page. Nothing is downloaded or installed automatically.

The app no longer supports local Ollama, a custom Ollama URL, or local model discovery. The Ollama desktop app is not required. The model picker (under the message box) also sets thinking/reasoning effort.

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
Settings > Trash lists it. It holds two kinds of items:
- **Deleted:** files or folders a session removed with recoverable deletion.
- **Earlier version:** the content a file had before a session overwrote or edited it.

**Restore** puts an item back. Restoring an earlier version keeps the current file in the Trash too, so a restore can itself be undone. A deleted item is only restored if nothing now exists at its original path. **Delete** and **Empty trash** remove items permanently, after a confirmation. Items are stored in `data/trash/` and stay until you remove them. Files over 20 MB are not versioned.

The number on the folder icon shows how many folders the current chat can access. Tool calling needs a compatible model; small models sometimes say they did something without actually calling the tool.

## Session settings
Every chat has two settings, in **Session settings** (the menu on the chat's name, top left) or the mode chip under the message box:

- **Permission mode**, what asks you first:

  | Mode | Without asking | Asks first |
  |---|---|---|
  | **Manual approve** (default) | reading files, searching, Git status and history | every file write or edit (review dialog), every create, copy, move, delete or restore, every command, commit, merge, push and pull request, and model changes |
  | **Auto edits** | also file writes, edits, create, copy, move, delete (to Trash) and restore, and commands on your allowed list | other commands, commits, merges, pushes, pull requests, model changes; after the chat reads web content, every command and memory note |
  | **Bypass permissions** | everything | nothing, web content or not |

- **Web search:** the chat can search the web and open any page without asking. See *Web search* below.

Every chat can use files (in folders with **Allow edits**), Git up to push, commands (in folders with **Allow edits**), subtasks, instruction files and memory; the mode decides what asks first. Deleted items always go to the app's trash in `data/trash/`, and edited files keep their earlier version there, so changes can be undone in every mode. A subtask always has read-only Git, no commands, no subtasks of its own and a fixed model.

**Settings > Session defaults** sets the mode, web search and skills every new chat starts with. Existing chats keep theirs. A project's mode and web search take precedence for chats in that project; a subtask starts with its parent chat's mode, and the parent's model can change it with `set_subtask_mode`, never above its own mode. **Use defaults** in Session settings fills in the defaults for one chat.

A chat's folders, permissions and name are **server-owned**. Normal chat saves from the window can't change them, even from an outdated copy. They change only through the validated `/api/session/permissions` and `/api/session/rename` endpoints. Chats saved before modes existed get **Auto edits** if they had auto-edit on, and **Manual approve** otherwise.

**Model changes.** The model is told to switch only when you explicitly ask, for example "switch to gpt-oss:120b". Outside Bypass, a dialog shows the current model, the new model and the reason the model gave. Before switching, the server checks that the model exists in Ollama Cloud. It refuses a model without image support if the chat contains images, and warns when the new model doesn't support tools. The new model answers from the next step of the same reply. Your default model in Settings is not changed.

## Web search
Turn on **Web search** in a chat's Session settings (or in Settings > Session defaults). The model gets two tools, which use Ollama's web API with your API key:
- **`web_search`** returns titles, addresses and snippets.
- **`web_fetch`** reads a page.

The model is told to cite the sources it uses as clickable Markdown links.

Web pages are written by strangers and can contain text aimed at steering the model. The app protects against that in four ways:
- **Results are marked as data.** Everything from the web is wrapped in clear "untrusted content" markers, which a page can't fake, and the model is told never to follow instructions inside them.
- **The chat is marked.** The first time a chat reads web content, a **⚠ Web content** badge appears in the header. From then on, in **Auto edits**, every command (even one on your allowed list) and every memory note asks first. **Manual approve** already asks for those, and **Bypass permissions** never asks.
- **You decide when to trust it again.** In Session settings, the notice lists what the chat read, and **Trust this chat again** clears the mark. The web content stays in the conversation.

With web search on, the chat opens any page without asking. A page address can carry information out of the chat (file contents written into a link, say), and the model could put private information into a search query; it's told not to, but the app can't check it. Turn on web search only for chats where that's acceptable.

## Skills
A skill is a set of instructions you write once and turn on per chat, for example how to review code or the style to write in. Open **Settings > Skills** to:
- create, edit or delete skills;
- import a Markdown file. An optional `name:` and `description:` header between `---` lines is read;
- import a **skill folder** in the open Agent Skills format, the same skills Claude, Codex, Cursor and others use. Use **Import folder** for a folder with a `SKILL.md`, or **Import file** for a `.skill` or `.zip` package.

Turn skills on for a chat in its Session settings. Enabled skills are added to the chat's system instructions on every request, up to 30,000 characters in total and 20,000 per written skill. A skill never changes what the chat is allowed to do. Written skills are Markdown files in `data/skills/`; skill folders are copied to `data/skills/<name>/`.

**Skill folders.** A skill folder has a `SKILL.md` (instructions, with `name:` and `description:` in its header) and any other files, such as reference documents and scripts.
- **Importing:** you can pick the skill's own folder or a folder above it, such as a repository with `skills/<name>/SKILL.md`, as long as it holds one skill. `.git`, `node_modules`, caches and Office lock files are left out.
- **Copy, not a link:** the copy keeps working if the original moves. After changing the original, click **Update from folder** in the Skills window. A `.skill` file is updated by importing it again.
- **What the chat sees:** its `SKILL.md` instructions and a list of its other files. The model reads a file with the `read_skill_file` tool when the instructions call for it, so long reference files cost nothing until they're needed. `read_skill_file` reads only the chat's enabled skill folders, and it works in plan mode. If `SKILL.md` is too long to send every time, the model gets the description and reads the instructions first.
- **Placeholders:** `<skill-dir>` and `{baseDir}` in `SKILL.md` are replaced with the skill's folder.
- **Scripts:** these run only through **Run commands**, under the usual rules. The model runs the script by its full path from a shared folder, and anything not on your allowed list needs your approval.

## Git and GitHub
Every chat gets Git tools for repositories in its shared folders:

| Tools | Manual approve and Auto edits | Bypass permissions |
|---|---|---|
| `git_status`, `git_diff`, `git_log`, `git_show`, `git_branches`, `gh_pr_list` | no approval | no approval |
| `git_commit`, `git_merge` | you review every commit (message, files and full diff) and merge | no approval |
| `git_push`, `gh_pr_create` | you approve every push (remote, branch and commits) and pull request (title, branches and description) | no approval |

Subtasks get only the first row.

Safety rules, enforced by the server (`gittools.py`):
- **Whole repositories only.** A repository is usable only if its **top folder** is inside a shared folder, so sharing a subfolder doesn't open up the whole repository. Committing needs **Allow edits** on that folder.
- **No hooks.** Git hooks don't run for Git commands the app runs (commits, merges, pushes, subtask worktrees), and the file tools can't change anything inside a `.git` folder. A hook is a program: otherwise the model could plant one and have it run without **Run commands**.
- **No smuggled options.** Git runs without a shell. Branch, remote and commit names are checked so they can't be read as options, and file paths are passed after `--` and must stay inside the repository.
- **No hidden programs.** External diff programs, text conversion filters and `core.fsmonitor` are switched off, and Git and GitHub CLI prompts are disabled, so nothing can hang waiting for input.
- **Nothing changes after review.** If the files change after you've reviewed a commit, or the branch moves after you've reviewed a push or pull request, it's refused and nothing happens.
- **No force-push.** It isn't offered at all, and the model is told never to rewrite history.
- **Your credentials stay with Git.** Commits use your Git identity, and pushes and pull requests use your existing Git and GitHub CLI (`gh`) sign-in. The app never sees them.
- **Subtasks** get read-only Git at most.

Pull requests need the [GitHub CLI](https://cli.github.com) installed and signed in (`gh auth login`), and the branch pushed first.

## Running commands
A chat with a shared folder with **Allow edits** gets `run_command`: one command at a time, run in such a folder, with its output returned.

**Allowed commands** (Settings) are one per line:
- `npm test` matches exactly `npm test`. The program name is compared without path or extension, so `npm.cmd test` also matches.
- `python -m pytest *` matches `python -m pytest` followed by any extra arguments.

In **Auto edits**, a command on the list runs without asking and **anything else needs your approval**, in a dialog showing the folder, the exact command and the time limit; once the chat has read web content, every command needs approval. In **Manual approve** every command asks; in **Bypass permissions** none does.

How commands run (`commands.py`):
- **No shell.** The line is split the way Windows splits it. Arguments that are shell operators (`&&`, `|`, `;`, `>`, `<`, …) are refused with a clear message, so the model can't expect pipes or chaining to work.
- **Batch-file guard.** Programs that are `.cmd`/`.bat` files, such as `npm`, are run by cmd.exe. Arguments containing characters cmd.exe treats specially (`& | < > ^ % ! "`) are refused for them.
- **Your API key stays out.** The Ollama API key is removed from the command's environment.
- **Time limit.** It defaults to 2 minutes, and the model can ask for up to 10. **Stop** or the time limit ends the command *and everything it started*.
- **Output.** The start and end are kept (about 60,000 characters), along with the exit code and duration.
- **Subtasks never get commands.**

**Important:** a command runs with your Windows account, so it can read or change anything you can, not just the shared folders. Keep the allowed list to commands you'd be happy to run without looking, such as tests, builds and linters.

## Subtasks
The model can hand self-contained pieces of work to helper chats with `start_subtask`:
- **Parallel.** Subtasks requested in the same reply run at the same time. The chat waits for them, then gets each one's final answer and continues.
- **Visible.** Each subtask is a normal chat, shown under its parent in the sidebar and marked "subtask of …". Its card in the parent chat has an **Open** button, and its approvals appear in its own chat, with an amber dot in the sidebar.
- **Never more permission than its parent.** A subtask gets the parent's folders, skills, mode and web search, and always has read-only Git, no commands and a fixed model; it can't start subtasks of its own. The parent's model can change a subtask's mode with `set_subtask_mode` (for example to Manual approve), never above its own. If the parent has read web content, the subtask starts untrusted. If a subtask reads web content, the parent becomes untrusted when the result comes back.
- **Talking to each other.** A parent and its subtasks can message each other while they work:
  - `message_parent`: a subtask sends a progress note, or asks a question with `wait_for_reply` and pauses until the parent answers (up to 30 minutes). A parent that is waiting for its subtasks stops waiting as soon as one asks, so its model can answer.
  - `message_subtask`: the parent answers a question, or sends a follow-up that the subtask reads before its next step. A subtask that has already finished continues with the message as a new reply, so the parent can ask for more without starting over.
  - `wait_subtasks`: the parent waits for results and messages.
- **Managing them.** The parent's model can also:
  - `rename_subtask`: rename a subtask, without asking;
  - `set_subtask_mode`: change its permission mode, never above the parent's own;
  - `delete_subtask`: delete a subtask and its messages, stopping it first if it's running. Outside **Bypass permissions** you approve each deletion; the files it changed stay as they are.
  - In a subtask chat, messages from the parent show as "From the parent chat"; a subtask waiting for an answer shows "waiting for the parent…".
  - Messages are treated as findings or instructions from the other chat, never as yours, and they follow the untrusted rule above in both directions. At most 30 messages per reply, 8,000 characters each.
  - If a reply ends while its subtasks are still running or unread, it's reminded twice to read them; subtasks still running when it ends are stopped.
- **Low balance.** A subtask wraps up and pauses like any reply. Its step in the parent shows "wrapping up…" or "paused: low balance", and the parent stops waiting, reads its progress summary, and pauses too. Both continue when the balance is topped up.
- **Bounded.** At most 6 subtasks per reply, and they count toward the limits in Settings.
- **Stopped and deleted with the parent.** Stopping the parent stops its subtasks, and deleting the parent deletes them.

## Balance guard
The app reads your Ollama account balance from `GET https://ollama.com/api/balance`, using your API key. The balance is your remaining plan allowance plus purchased credits, and it's shown at the bottom of the sidebar, in amber when it's within $0.50 of your pause amount and in red at or below it. The server re-checks it before every model request. The value is cached for 30 seconds and shared by all replies, because Ollama allows about 10 balance checks a minute.

When the balance is **at or below the pause amount** set in Settings (default $1):
- **Every running reply wraps up.** It finishes the step it's on. A reply that has already done some work then makes **one last short request without tools**, asking the model to summarise what it did and what's left. That summary is marked *wrap-up*. A reply that hasn't started yet doesn't spend anything.
- **The reply pauses.** The chat shows a banner with **Recharge…**, **Check now** and **Stop**, the sidebar shows a red dot, and Activity lists it as *paused: low balance*. New messages also pause until the balance is back up.
- **It continues by itself.** It re-checks about once a minute, or at once when you click **Check now**. When the balance is above the pause amount again, it adds an automatic note ("continue the work from where you stopped") and carries on.

If the balance can't be read, for example because Ollama changes its API, replies keep working and the sidebar says *Balance unavailable*. The wrap-up request itself costs a little, so the pause amount works as a reserve, not a hard cap.

## Activity
**Settings > Activity** lists every running reply across all chats, subtasks included. For each one it shows the status (running, waiting for you, wrapping up, paused, or a subtask asking its parent), model, time, steps and tokens, with **Open** and **Stop** buttons. Below that are the most recently finished replies and how they ended (done, stopped, or error).

## Diagnostics
**Settings > Diagnostics** shows how the app behaves on your PC, from anonymous measurements it keeps in `data/diagnostics/` (one file per day, kept 90 days):
- **Replies:** steps per reply, tokens per reply, the share of input served from Ollama's cache, typical reply time and wait for the first token, and outcomes (finished, stopped, step limit, errors), retries and messages sent while a reply ran.
- **Where input tokens go:** instructions, tool definitions, your messages, the model's earlier replies, tool results and summaries, estimated for every request.
- **Models:** steps, input, output and thinking tokens, tokens per second, first-token time and errors.
- **Tools:** calls, median and slowest times, how many tokens their results added, errors and denials.
- **Approvals** (asked, allowed, typical wait), **summaries** of older messages, and **window** redraw times.

**Export report** saves it all as a JSON file you can review or share. **Clear** deletes it, and **Record diagnostics** turns recording off.

What's recorded is numbers, true/false and short names from fixed sets (tool and model names, modes, outcomes, error types). Never stored: message text, file names or paths, folder names, web addresses, search queries, command text, chat names or IDs, or keys; `diagnostics.py` drops any other text, whatever is passed to it. Nothing is sent anywhere.

## Replies run on the server
Each reply runs on the app's local server (`runs.py`): the model answers, calls tools, gets their results, and continues, up to the step limit (Settings > Usage & limits, 100 by default). The window shows it live through an event stream. As a result:
- **Switching chats or closing the window doesn't stop a reply.** Reopen the chat to see where it got to. The background server keeps running while any reply is in progress.
- **Several chats can reply at the same time.** A pulsing dot in the sidebar marks chats that are replying, and an amber dot marks chats waiting for your approval.
- **Above the message box**, a working line shows what the reply is doing (thinking, writing, reading a file, running a command, waiting for you) and for how long.
- **You can write while it works.** Type and press Enter (the ■ button becomes send while there's text): the reply reads your message before its next step, and the working line shows it as waiting until then. A reply waiting for its subtasks reads it straight away. A reply that pauses at the step limit shows **Continue**.
- **Stop** (the ■ button with an empty message box, or Escape in the message box) ends the current chat's reply, including one that's waiting for approval. Escape in a menu, a dialog or a rename only closes that.
- **Temporary Ollama errors are retried.** When Ollama answers "busy" or a server error, or the connection drops before a reply finishes, the step is tried again after 3, 10 and 30 seconds, with a note in the reply. Only then does the reply stop with the error.
- **Edit and resend** shows a banner. The earlier message and everything after it are replaced only when you send.
- While a reply runs, its chat's messages and model can't be changed from elsewhere. Folders and permissions can still change, and they take effect at the next tool call.

## Context, tokens and cost
Ollama Cloud bills per token, and input it has seen before at the start of a request ("cached input") costs far less, often about a tenth. So the app keeps the start of every request the same from step to step and turn to turn: instructions and tools first, then the history, and anything that changes per request at the end. Ollama reports how much was cached; hover the token count under the message box to see it.

Before each request the server fits the conversation into the **context budget** (Settings > Usage & context):
- **Full** (default) uses the model's whole window. **Balanced** (64k tokens) and **Economy** (32k) send less per request; they suit models without cached-input pricing.
- Everything sent counts: messages, tool definitions and instructions.
- Tool results from earlier turns are shortened, and the model can call the tool again.
- Past the budget, the older turns are replaced by a summary the chat's model writes, kept until the next one. Your own earlier messages stay next to it, shortened and in order, so details you gave aren't lost. A "View summary" divider shows where it starts. If a summary can't be made, the oldest turns are left out instead.
- A file is read up to 800 lines (about 28,000 characters) at a time, with a note saying where to continue. A single very long line (minified code, a one-line JSON file) is cut at that length.
- **Compact** (under the message box) summarizes the older part of the chat now, keeping the latest messages word for word, so the next requests are smaller.
- Pictures are shrunk to at most 1600 pixels on the long side before sending. Models that bill by resolution use far fewer tokens: one test screenshot went from 5,069 to 1,579 tokens with the same answer.

The row under the message box shows the chat's folders, Git level, a context gauge (tokens in the last request out of the model's window), **Compact**, the tokens this chat sent and received, and the model. Today's totals are in Settings > Usage & limits (kept in `data/usage.json` for 90 days).

## Projects and memory
- **Projects** (the switcher under the search box, or Manage projects) group chats. A project has folders, a starting mode and web search setting, skills, written instructions and its own memory. New chats in the project start with all of them.
- **Memory**: notes chats can use later. Global memory (Settings > Memory) applies everywhere; project memory only in that project. Ask the model to remember something and it saves a note; you can edit or delete notes at any time. In Manual approve and Auto edits, a chat that has read web content asks you before saving, so a web page can't plant lasting instructions.
- **Instruction files**: `AGENTS.md`, `CLAUDE.md` or `.openay/instructions.md` at the top of a shared folder are followed by the chat. They can't change permissions.

## Plan mode, undo and documents
- **Mode** (next to the paperclip) switches between **Act** and **Plan**. Plan asks for a plan first: the model only gets tools that read, and the server refuses anything else. Then **Carry out this plan**, or reply with changes.
- **Undo changes** under a reply puts back every file it created, edited, moved or deleted. Files changed again afterwards are skipped and listed, and what undo replaces goes to the Trash, so an undo can be undone. Commands and Git actions can't be undone and are listed as such.
- **Documents**: attach PDF, Word, Excel, PowerPoint, CSV, JSON, code or text files with the paperclip, by pasting, or by dropping. Their text goes to the model with your message. Scanned PDFs have no text: attach page images instead.

## Scheduled tasks, terminal and preview
- **Scheduled tasks** (Settings > Scheduled tasks) start a new chat with a prompt every day, on weekdays, weekly, every few hours, or once, in a project if you like. They run while the app is open or in the tray (see *Keep running in the tray* and *Start with Windows*). A run missed while the app was closed happens once when it next starts (within 12 hours). When the most replies are already running, a due task waits and starts as soon as one finishes (for up to an hour).
- **Terminal and preview** (Ctrl+J, or the menu on the chat's name): run commands you type in a writable shared folder, watch their output as it comes, and stop them; see the commands the model ran. Preview shows files the chat changed: HTML pages run in a sandbox with no access to the app, Markdown is rendered, and images and text are shown.
- **Search** under New chat looks through every chat's messages, not just titles, and jumps to the matching message.
- **Export** (top right) saves the chat as a Markdown file.
- **Quick entry**: Ctrl+Alt+Space opens the app on a new chat from anywhere (Settings > App & updates).
- **Dictation** (microphone button) uses Windows speech recognition, offline. It needs a speech language installed and microphone access for desktop apps.

## Software updates
The installed app checks GitHub Releases at start and every 6 hours (Settings > App & updates > Check now). With **Install updates automatically** on, a new version downloads in the background and installs when the app closes. Otherwise Settings offers Download and Restart. Chats and settings are kept. Version 1.12.0 is the first with the updater, so moving from 1.11 or older needs one manual install.

## The window
- **Sidebar:** New chat, search, the project switcher, your sessions (subtasks under their parent), and at the bottom the Ollama balance and Settings.
- **Top:** the chat's name, with a menu (rename, session settings, terminal and preview, delete), and **Export** on the right.
- **Message box:** attach (paperclip), mode (**Act** or **Plan**), dictation (microphone) and send. Above it, the working line while a reply runs; under it, folders, the permission mode, the context gauge, **Compact**, tokens and the model.
- **Settings** has one page per tab: Connection, Session defaults, Usage & limits, Memory, Commands, Skills, Projects, Scheduled tasks, Activity, Diagnostics, Trash, and App & updates.

## Features
Streaming replies, Markdown and code highlighting, collapsible thinking/reasoning panels, image and document attachments, stop, regenerate, edit-and-resend, full-text search, projects, memory, plan mode, undo, scheduled tasks, a terminal and preview panel, export, and server-approved folder tools.

Rename a chat by double-clicking its name at the top left or in the sidebar, from the menu on its name, or by right-clicking it in the sidebar (Open, Rename and Delete). Right-clicking text or a text box gives Cut, Copy, Paste and Select all, with spelling suggestions.

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
- `diagnostics.py` — local anonymous diagnostics: the event recorder (numbers and fixed names only) and the summary for Settings > Diagnostics.
- `capabilities.py` — session permission policy, file management, trash and versions.
- `session_actions.py` — session renaming.
- `web.py` — web search and page reading, with untrusted-content markers.
- `commands.py` — running commands: parsing, the allowed list, the batch-file guard, time limits and Stop.
- `gittools.py` — Git and GitHub tools: repository checks, the safe command runner, and commit, push and pull request reviews.
- `skills.py` — skills: written skills and skill folders, importing (folder, .skill, .zip), `read_skill_file` and the per-chat prompt.
- `index.html` — application and dialog markup.
- `styles.css` — the Graphite look: colour tokens for dark and light (it follows the Windows setting), components and motion. The page's security policy blocks inline styles, so every look is a class here.
- `app.js` — the window: live event stream, rendering, approvals, sessions, trash, settings, plan mode, undo and documents.
- `features.js` — projects, scheduled tasks, the terminal and preview panel, export, dictation and quick entry.
- `electron/` — the Electron shell: `main.js` (server lifecycle, window, tray, quit), `preload.js` (folder picker, export, dictation, quick entry and updates), `icon.png` (app icon), `tray.png` (simplified tray icon).
- `package.json` — Electron and electron-builder setup (`npm start`, `npm run smoke`, `npm run dist`, `npm run release`).
- `scripts/fetch-python.js` — downloads and verifies the embeddable Python that the installer bundles.
- `scripts/release.js` — publishes the built installer as a GitHub Release for the updater (`npm run release`).
- `tests/` — isolated standard-library tests (`test_server.py`, `test_capabilities.py`, `test_runs.py`, `test_phase3.py`, `test_phase4.py`, `test_balance.py`, `test_git.py`, `test_commands.py`, `test_tokens.py`, `test_features.py`, `test_skill_folders.py`, `test_git_processes.py`, `test_stop.py`, `test_subtask_messages.py`, `test_session_refresh.py`).
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
