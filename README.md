# Open AY Desktop

A lightweight desktop chat app for **Ollama Cloud**. It needs Python 3 (standard library only) and Edge or Chrome. There is no frontend framework, npm installation, or build step.

- **Application:** Open AY Desktop
- **Version:** 1.1.0
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

Tick **Allow edits** to let the model create or overwrite text files. Every write requires an explicit review:
- The dialog shows the **complete replacement content**, not a truncated preview.
- For an existing file, **Show full diff** displays a complete unified diff against the current file.
- The server issues a random, single-use approval token tied to that chat, canonical path, and exact content.
- Approval expires after five minutes and is revalidated against saved permissions immediately before writing.
- Denying, cancelling, or changing an approval does not write the file.

The number on the folder icon shows how many folders the current chat can access. Tool calling needs a compatible model; small models sometimes say they did something without actually calling the tool.

## Session settings
The **Session** button in the header opens settings for the current chat only. The server stores them with the chat and checks them on every tool call.

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

## Features
Streaming replies, Markdown and code highlighting, collapsible thinking/reasoning panels, image attachments for vision models (paste, drop, or paperclip), stop, regenerate, edit-and-resend, searchable chat history, and server-approved folder tools.

## Persistence and reliability
- Chats and configuration are written atomically with unique temporary files.
- Per-chat server locks protect reads, writes, and deletion.
- Browser saves are ordered and coalesced per chat so older queued snapshots do not overwrite newer ones.
- Opening or deleting the currently streaming chat stops it and waits for its final save first.

## Local interface libraries
The browser loads Marked, DOMPurify, Highlight.js, and both syntax themes from this app's own `/vendor/` routes, not from CDN script tags. The server permits only five fixed, versioned assets: Marked 12.0.2, DOMPurify 3.1.6, Highlight.js 11.9.0, and its two 11.9.0 themes.

On the first request for each asset, the server downloads its fixed HTTPS URL into `vendor/`. After all five have been cached successfully, the complete interface works offline without contacting cdnjs. If a first download fails, plain-text Markdown fallback remains available. See `vendor/README.md` for upstream source URLs and licensing information.

## Publishing a release for the updater
1. Update `APP_VERSION` in `server.py` and the version shown here.
2. Commit and tag the release as `vX.Y.Z`, for example `v1.1.0`.
3. Publish a non-draft GitHub Release with that tag in `arothm/OpenAYDesktop`.
4. The app's update check compares numeric semantic versions. A malformed release tag produces a clear error. If no release has been published, GitHub's 404 response is reported as **No published release yet**, not as an update.

## Source layout
- `server.py` — cloud proxy, storage, update checker, folder tools, write authorization, local assets, and launcher.
- `index.html` — application and dialog markup.
- `styles.css` — interface styling and light/dark themes.
- `app.js` — chat state, streaming, tools, ordered saves, settings, and update UI.
- `tests/test_server.py` — isolated standard-library regression tests.
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
