# Open AY Desktop

A lightweight desktop chat app for **Ollama Cloud**. It needs Python 3 (standard library only) and Edge or Chrome. There is no frontend framework, npm installation, or build step.

- **Application:** Open AY Desktop
- **Version:** 1.3.0
- **Repository:** https://github.com/arothm/OpenAYDesktop
- **Ollama connection:** direct to https://ollama.com, using an Ollama API key

The application was previously named Ollama Desk. Existing chats, folder grants, cached browser data, and API keys remain in the existing `data/` directory. The first time configuration is read, the former last-selected model is preserved as the explicit default model. Legacy local-mode, system-prompt, and temperature options are ignored and removed when settings are next saved.

## Run
- Double-click **`Open AY Desktop.cmd`**. The app opens in its own window. The background server stops about 3 minutes after the last window closes.
- The old **`Ollama Desk.cmd`** remains as a compatibility launcher.
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
3. **Check for Updates** — checks the latest release at https://github.com/arothm/OpenAYDesktop/releases using GitHub's public API. It reports the installed version, update availability, no-published-release state, or a connection error. If an update exists, **Open Latest Release** opens that release page. Nothing is downloaded or installed automatically.

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

  The model is told to switch only when you explicitly ask. Before switching, the server checks that the model exists in Ollama Cloud. It refuses a model without image support if the chat contains images, and warns when the new model doesn't support tools. The new model answers from the next step of the same reply. Your default model in Settings is not changed.

Chats saved before these settings existed get the defaults above. A folder with **Allow edits** still allows reviewed writes, but file management and deletion stay off until you enable them.

## Replies run on the server
Each reply runs on the app's local server (`runs.py`): the model answers, calls tools, gets their results, and continues, up to 25 steps. The window shows it live through an event stream. As a result:
- **Switching chats or closing the window doesn't stop a reply.** Reopen the chat to see where it got to. The background server keeps running while any reply is in progress.
- **Several chats can reply at the same time.** A pulsing dot in the sidebar marks chats that are replying, and an amber dot marks chats waiting for your approval.
- **Stop** (the ■ button, or Escape) ends the current chat's reply, including one that's waiting for approval.
- **Edit and resend** shows a banner. The earlier message and everything after it are replaced only when you send.
- While a reply runs, its chat's messages and model can't be changed from elsewhere. Folders and permissions can still change, and they take effect at the next tool call.

## Context and usage
Before each request, the server fits the conversation into the model's context window. It learns the window size from Ollama Cloud and assumes 64k tokens if Ollama Cloud doesn't report it.
- Tool results from earlier turns are shortened, and the model can call the tool again if it needs them.
- If the conversation still doesn't fit, the oldest whole turns are left out of the request and a note tells the model. The full history stays saved in the chat.

The line under the message box shows:
- how full the context was in the last request, from Ollama's real token counts;
- how many older messages were left out;
- this chat's total tokens;
- today's tokens and requests across all chats. Daily totals are kept in `data/usage.json` for 90 days.

## Features
Streaming replies, Markdown and code highlighting, collapsible thinking/reasoning panels, image attachments for vision models (paste, drop, or paperclip), stop, regenerate, edit-and-resend, searchable chat history, and server-approved folder tools.

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
1. Update `APP_VERSION` in `server.py` and the version shown here.
2. Commit and tag the release as `vX.Y.Z`, for example `v1.3.0`.
3. Publish a non-draft GitHub Release with that tag in `arothm/OpenAYDesktop`.
4. The app's update check compares numeric semantic versions. A malformed release tag produces a clear error. If no release has been published, GitHub's 404 response is reported as **No published release yet**, not as an update.

## Source layout
- `server.py` — HTTP API, storage, folder tools, write approvals, model info, usage totals, update checker, local assets, and launcher.
- `runs.py` — server-side replies: the model/tool loop, live events, approvals, stop, and per-chat usage.
- `context_budget.py` — fits the history into the model's context window.
- `capabilities.py` — session permission policy, file management, trash and versions.
- `session_actions.py` — session renaming.
- `index.html` — application and dialog markup.
- `styles.css` — interface styling and light/dark themes.
- `app.js` — the window: live event stream, rendering, approvals, sessions, trash, and settings.
- `tests/` — isolated standard-library tests (`test_server.py`, `test_capabilities.py`, `test_runs.py`).
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
Chats and settings, including the API key, are saved under `data/` next to `server.py`. The server listens only on 127.0.0.1, checks request origins, sets a restrictive content-security policy, and rejects requests from other websites.
