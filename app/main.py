from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.assets import style_css_version
from app.auth import COOKIE_NAME, MAX_AGE, current_user, current_user_email, sign_in, sign_up
from app.db import current_workspace_info, get_supabase, sidebar_clients
from app.routers import clients, editor, projects, publish

app = FastAPI(title="Client Deliverables Platform")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(clients.router)
app.include_router(projects.router)
app.include_router(editor.router)
app.include_router(publish.router)
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["style_v"] = style_css_version
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())
templates.env.globals["current_user_email"] = current_user_email


@app.get("/")
def root():
    return RedirectResponse("/clients")


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        cookie_value = sign_in(email, password)
    except Exception:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )
    response = RedirectResponse("/clients", status_code=303)
    response.set_cookie(COOKIE_NAME, cookie_value, max_age=MAX_AGE, httponly=True, samesite="lax")
    return response


@app.get("/register")
def register_form(request: Request, invite: str = ""):
    return templates.TemplateResponse(request, "register.html", {"invite_code": invite})


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    workspace_name: str = Form(""),
    invite_code: str = Form(""),
):
    try:
        cookie_value = sign_up(email, password, workspace_name, invite_code)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": str(e), "invite_code": invite_code, "email": email, "workspace_name": workspace_name},
            status_code=400,
        )
    response = RedirectResponse("/clients", status_code=303)
    response.set_cookie(COOKIE_NAME, cookie_value, max_age=MAX_AGE, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/settings", dependencies=[Depends(current_user)])
def settings_page(request: Request):
    workspace = current_workspace_info(get_supabase())
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"workspace": workspace, "invite_url": str(request.base_url).rstrip("/") + f"/register?invite={workspace['invite_code']}"},
    )
