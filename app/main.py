import asyncio
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import TemplateError
from pydantic import BaseModel, Field

from . import __version__, db
from .render import DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, check_template, render_messages
from .telegram import Sender, TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("relay")

STATIC = Path(__file__).parent / "static"
COOKIE = "relay_session"
SESSION_TTL = 30 * 86400
SECRET_KEYS = {"bot_token", "mtproto_api_hash", "mtproto_secret"}
PUBLIC_KEYS = {"transport", "botapi_proxy", "botapi_base", "mtproto_api_id", "mtproto_host", "mtproto_port",
               "max_attempts", "public_url"}

sender = Sender()
wake: asyncio.Event | None = None  # created in lifespan, bound to the running loop


def kick_worker():
    if wake is not None:
        wake.set()


async def worker():
    while True:
        try:
            wake.clear()
            msg = db.next_due(time.time())
            if msg is None:
                delay = db.seconds_until_next(time.time())
                try:
                    await asyncio.wait_for(wake.wait(), timeout=max(min(delay if delay is not None else 30, 30), 0.5))
                except asyncio.TimeoutError:
                    pass
                continue

            cfg = db.get_settings()
            attempts = msg["attempts"] + 1
            try:
                await sender.send(cfg, msg["chat_id"], msg["text"], msg["thread_id"], bool(msg["silent"]))
                db.mark_sent(msg["id"], attempts)
                log.info("sent #%s to %s (%s)", msg["id"], msg["chat_id"], msg["route_name"])
            except TelegramError as e:
                max_attempts = int(cfg.get("max_attempts") or 30)
                if e.retry and attempts < max_attempts:
                    delay = e.retry_after or min(15 * 2 ** (attempts - 1), 1800)
                    db.mark_retry(msg["id"], attempts, time.time() + delay, str(e))
                    log.warning("#%s attempt %s failed, retry in %ss: %s", msg["id"], attempts, delay, e)
                else:
                    db.mark_failed(msg["id"], attempts, str(e))
                    log.error("#%s failed permanently: %s", msg["id"], e)
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker error")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app):
    global wake
    db.init()
    wake = asyncio.Event()
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    await sender.close()
    db.close()


app = FastAPI(title="Telegram Relay", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- auth

def _hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def _check_pw(password: str) -> bool:
    stored = db.get_setting("password_hash")
    if not stored:
        return False
    return hmac.compare_digest(_hash_pw(password, stored.split("$", 1)[0]), stored)


def _sign(exp: int) -> str:
    return hmac.new(db.get_setting("session_secret").encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def _valid_session(value: str) -> bool:
    try:
        exp_s, sig = value.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(exp))


def _login(response: Response):
    exp = int(time.time()) + SESSION_TTL
    response.set_cookie(COOKIE, f"{exp}.{_sign(exp)}", max_age=SESSION_TTL, httponly=True, samesite="lax")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if not _valid_session(request.cookies.get(COOKIE, "")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class PasswordIn(BaseModel):
    password: str = Field(min_length=1)


class PasswordChangeIn(BaseModel):
    old: str
    new: str = Field(min_length=6)


@app.get("/api/auth/state")
def auth_state(request: Request):
    return {"configured": bool(db.get_setting("password_hash")),
            "authed": _valid_session(request.cookies.get(COOKIE, ""))}


@app.post("/api/auth/setup")
def auth_setup(body: PasswordIn, response: Response):
    if db.get_setting("password_hash"):
        raise HTTPException(409, "Пароль уже задан")
    if len(body.password) < 6:
        raise HTTPException(400, "Пароль должен быть не короче 6 символов")
    db.set_settings({"password_hash": _hash_pw(body.password)})
    _login(response)
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(body: PasswordIn, response: Response):
    if not _check_pw(body.password):
        await asyncio.sleep(1)
        raise HTTPException(401, "Неверный пароль")
    _login(response)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/password")
def change_password(body: PasswordChangeIn, response: Response):
    if not _check_pw(body.old):
        raise HTTPException(400, "Текущий пароль неверен")
    # new session secret invalidates every other session
    db.set_settings({"password_hash": _hash_pw(body.new), "session_secret": secrets.token_hex(32)})
    _login(response)
    return {"ok": True}


# ---------------------------------------------------------------- settings

def _hint(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 12 else "••••"


@app.get("/api/settings")
def get_settings():
    cfg = db.get_settings()
    out = {k: cfg.get(k, "") for k in PUBLIC_KEYS}
    for k in SECRET_KEYS:
        out[k + "_hint"] = _hint(cfg.get(k, ""))
    return out


@app.put("/api/settings")
def put_settings(body: dict):
    values = {}
    for k, v in body.items():
        if k in SECRET_KEYS:
            if v:  # empty = keep the stored value
                values[k] = str(v).strip()
        elif k in PUBLIC_KEYS:
            values[k] = "" if v is None else str(v).strip()

    if values.get("transport", "botapi") not in ("botapi", "mtproto"):
        raise HTTPException(400, "Неизвестный режим отправки")
    proxy = values.get("botapi_proxy")
    if proxy and urlparse(proxy).scheme not in ("http", "https", "socks5", "socks5h"):
        raise HTTPException(400, "Прокси: поддерживаются схемы http://, https://, socks5://, socks5h://")
    if "max_attempts" in values and not (values["max_attempts"].isdigit() and 1 <= int(values["max_attempts"]) <= 1000):
        raise HTTPException(400, "Число попыток должно быть от 1 до 1000")
    if values.get("mtproto_port") and not values["mtproto_port"].isdigit():
        raise HTTPException(400, "Порт MTProxy должен быть числом")
    if values.get("public_url"):
        values["public_url"] = values["public_url"].rstrip("/")

    db.set_settings(values)
    db.retry_all_now()
    kick_worker()
    return get_settings()


@app.post("/api/check")
async def check_connection():
    cfg = db.get_settings()
    started = time.monotonic()
    try:
        me = await sender.check(cfg)
    except TelegramError as e:
        return {"ok": False, "error": str(e), "transport": cfg.get("transport")}
    return {"ok": True, "bot": me, "ms": round((time.monotonic() - started) * 1000),
            "transport": cfg.get("transport")}


@app.get("/api/chats")
async def recent_chats():
    try:
        return {"chats": await sender.recent_chats(db.get_settings())}
    except TelegramError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- routes

class RouteIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    chat_id: str = Field(min_length=1, max_length=100)
    thread_id: int | None = None
    template: str | None = None
    split: bool = False
    silent_resolved: bool = False
    enabled: bool = True


def _route_data(body: RouteIn) -> dict:
    data = body.model_dump()
    data["name"] = data["name"].strip()
    data["chat_id"] = data["chat_id"].strip()
    data["template"] = data["template"] if (data["template"] or "").strip() else DEFAULT_TEMPLATE
    try:
        check_template(data["template"])
    except TemplateError as e:
        raise HTTPException(400, f"Ошибка в шаблоне: {e}")
    except Exception as e:
        raise HTTPException(400, f"Шаблон не рендерится на тестовых данных: {type(e).__name__}: {e}")
    return data


def _route_or_404(route_id: int) -> dict:
    route = db.get_route(route_id)
    if not route:
        raise HTTPException(404, "Маршрут не найден")
    return route


@app.get("/api/routes")
def list_routes():
    return db.list_routes()


@app.post("/api/routes")
def create_route(body: RouteIn):
    return _route_or_404(db.create_route(_route_data(body)))


@app.put("/api/routes/{route_id}")
def update_route(route_id: int, body: RouteIn):
    _route_or_404(route_id)
    db.update_route(route_id, _route_data(body))
    return _route_or_404(route_id)


@app.delete("/api/routes/{route_id}")
def delete_route(route_id: int):
    db.delete_route(route_id)
    return {"ok": True}


@app.post("/api/routes/{route_id}/regen-token")
def regen_token(route_id: int):
    _route_or_404(route_id)
    db.regen_token(route_id)
    return _route_or_404(route_id)


@app.get("/api/routes/{route_id}/payload")
def last_payload(route_id: int):
    route = _route_or_404(route_id)
    return {"payload": json.loads(route["last_payload"]) if route["last_payload"] else None,
            "last_hit": route["last_hit"]}


@app.post("/api/routes/{route_id}/test")
async def test_route(route_id: int):
    route = _route_or_404(route_id)
    messages = render_messages(route["template"], SAMPLE_PAYLOAD, route)
    text = "🧪 <i>Тестовое сообщение релея</i>\n\n" + messages[0][0]
    try:
        await sender.send(db.get_settings(), route["chat_id"], text, route["thread_id"])
    except TelegramError as e:
        db.log_direct(route, text, str(e))
        return {"ok": False, "error": str(e)}
    db.log_direct(route, text, None)
    return {"ok": True}


class PreviewIn(BaseModel):
    template: str
    split: bool = False
    source: str = "sample"
    route_id: int | None = None


@app.post("/api/preview")
def preview(body: PreviewIn):
    payload = SAMPLE_PAYLOAD
    if body.source == "last" and body.route_id:
        route = db.get_route(body.route_id)
        if route and route["last_payload"]:
            payload = json.loads(route["last_payload"])
    try:
        messages = render_messages(body.template or DEFAULT_TEMPLATE, payload, {"split": body.split, "name": "preview"})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "payload": payload}
    return {"ok": True, "messages": [{"text": t, "silent": r} for t, r in messages], "payload": payload}


@app.get("/api/default-template")
def default_template():
    return {"template": DEFAULT_TEMPLATE}


# ---------------------------------------------------------------- log

@app.get("/api/messages")
def list_messages(status: str | None = None, limit: int = 200):
    return db.list_messages(status if status in ("pending", "sent", "failed") else None, min(max(limit, 1), 1000))


@app.post("/api/messages/{msg_id}/retry")
def retry_message(msg_id: int):
    db.retry_message(msg_id)
    kick_worker()
    return {"ok": True}


@app.delete("/api/messages")
def clear_messages():
    db.clear_messages()
    return {"ok": True}


@app.get("/api/stats")
def stats():
    return db.stats()


# ---------------------------------------------------------------- webhook

@app.post("/hook/{token}")
async def hook(token: str, request: Request):
    route = db.get_route_by_token(token)
    if not route:
        raise HTTPException(404, "unknown hook")

    body = await request.body()
    try:
        payload = json.loads(body) if body.strip() else {}
    except ValueError:
        payload = {"message": body.decode("utf-8", "replace")}
    if not isinstance(payload, dict):
        payload = {"message": json.dumps(payload, ensure_ascii=False)}

    db.touch_route(route["id"], payload)
    if not route["enabled"]:
        return {"ok": True, "queued": 0, "disabled": True}

    try:
        messages = render_messages(route["template"], payload, route)
    except Exception as e:
        log.exception("template error in route %s", route["name"])
        # never lose an alert because of a broken template
        raw = json.dumps(payload, ensure_ascii=False, indent=1)[:3500]
        messages = [(f"⚠️ <b>Ошибка шаблона</b> маршрута «{html.escape(route['name'])}»: "
                     f"<code>{html.escape(str(e))}</code>\n\n<pre>{html.escape(raw)}</pre>", False)]

    for text, all_resolved in messages:
        db.enqueue(route, text, silent=route["silent_resolved"] and all_resolved)
    db.prune()
    kick_worker()
    return {"ok": True, "queued": len(messages)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
