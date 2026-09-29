"""Single shared Jinja2Templates instance for the whole app.

Previously main.py and every router (clients.py, editor.py, projects.py,
publish.py) each called Jinja2Templates(directory="app/templates")
separately - five independent jinja2.Environment objects - and had to
remember to register templates.env.globals["style_v"] on its own copy.
Globals registered on one Environment are invisible on another, so adding a
6th router (or simply forgetting the line in one of the five) would silently
reintroduce `jinja2.exceptions.UndefinedError: 'style_v' is undefined` for
whatever pages that router serves - the environment split, not any missing
registration, was the actual defect class.

One Environment, registered once, imported everywhere: every module below
gets `templates.TemplateResponse(...)` and `templates.env.globals["style_v"]`
already wired, with nothing left to duplicate or forget.
"""
from fastapi.templating import Jinja2Templates

from app.assets import style_css_version

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["style_v"] = style_css_version
