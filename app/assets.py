"""Cache-busting for static assets. StaticFiles sends no Cache-Control header,
so a plain `<link href="/static/style.css">` can sit in a browser's cache
indefinitely across normal reloads - editing the file doesn't invalidate it.
Appending the file's own mtime as a query string forces a fresh fetch exactly
when the file actually changes, with no manual version bumping."""
import os

_STYLE_CSS = os.path.join(os.path.dirname(__file__), "static", "style.css")


def style_css_version() -> str:
    return str(int(os.path.getmtime(_STYLE_CSS)))
