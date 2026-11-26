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
