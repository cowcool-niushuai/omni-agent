# Vendored frontend assets

The Web console does not depend on public CDNs at runtime. The following
upstream browser assets are vendored for offline/local deployment:

| File | Project | License |
|---|---|---|
| `lucide.min.js` | Lucide | ISC |
| `marked.min.js` | Marked | MIT |
| `purify.min.js` | DOMPurify | Apache-2.0 / MPL-2.0 |
| `highlight.min.js` | Highlight.js | BSD-3-Clause |
| `github.min.css` | Highlight.js | BSD-3-Clause |

Tailwind CSS is compiled into `../app.css` from `../input.css` using the
project's local build command. See `README.md` for the build command.
