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
