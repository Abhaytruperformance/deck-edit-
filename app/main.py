from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import COOKIE_NAME, MAX_AGE, sign_in
from app.routers import clients, editor, projects, publish

app = FastAPI(title="Client Deliverables Platform")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(clients.router)
app.include_router(projects.router)
app.include_router(editor.router)
app.include_router(publish.router)
templates = Jinja2Templates(directory="app/templates")


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


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response
