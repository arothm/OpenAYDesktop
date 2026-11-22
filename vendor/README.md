# Locally served frontend assets

`server.py` is the only code allowed to populate this directory. The HTML references local `/vendor/...` URLs exclusively; it does not load scripts or stylesheets from cdnjs.

Each asset is downloaded once from its exact pinned HTTPS URL and cached beside this file. Subsequent starts use the cached bytes, including offline starts. Do not put unrelated files in this directory or use it as a generic static-file root.

| Local file | Upstream version | Fixed source |
|---|---|---|
| `marked.min.js` | Marked 12.0.2 | https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js |
| `purify.min.js` | DOMPurify 3.1.6 | https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js |
| `highlight.min.js` | Highlight.js 11.9.0 | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js |
| `github-dark.min.css` | Highlight.js 11.9.0 | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css |
| `github.min.css` | Highlight.js 11.9.0 | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css |

Upstream projects and licenses:

- Marked: https://github.com/markedjs/marked — MIT
- DOMPurify: https://github.com/cure53/DOMPurify — Apache-2.0 OR MPL-2.0
- Highlight.js: https://github.com/highlightjs/highlight.js — BSD-3-Clause

The first successful download still requires internet access and trust in the pinned upstream asset. For a redistributable, offline-from-first-launch package, download these exact files during packaging, verify their hashes, add their license notices, and ship the populated directory.
