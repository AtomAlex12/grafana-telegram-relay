import time

from fastapi.testclient import TestClient

from app import db, main
from app.render import SAMPLE_PAYLOAD
from app.telegram import TelegramError


def make_route(client, **kw):
    body = {"name": "Ops", "chat_id": "-100123", **kw}
    r = client.post("/api/routes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def wait_for(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------- auth

def test_first_run_setup_and_login(client):
    assert client.get("/api/auth/state").json() == {"configured": False, "authed": False}
    assert client.get("/api/routes").status_code == 401

    assert client.post("/api/auth/setup", json={"password": "123"}).status_code == 400
    assert client.post("/api/auth/setup", json={"password": "secret123"}).status_code == 200
    assert client.get("/api/auth/state").json() == {"configured": True, "authed": True}
    assert client.get("/api/routes").status_code == 200
    assert client.post("/api/auth/setup", json={"password": "other123"}).status_code == 409

    client.post("/api/auth/logout")
    assert client.get("/api/routes").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret123"}).status_code == 200
    assert client.get("/api/routes").status_code == 200


def test_forged_cookie_rejected(client):
    client.post("/api/auth/setup", json={"password": "secret123"})
    client.cookies.set(main.COOKIE, f"{int(time.time()) + 999}.deadbeef")
    assert client.get("/api/routes").status_code == 401


def test_password_change_logs_out_other_sessions(authed):
    other = TestClient(main.app)
    assert other.post("/api/auth/login", json={"password": "secret123"}).status_code == 200
    assert other.get("/api/routes").status_code == 200

    assert authed.post("/api/password", json={"old": "nope", "new": "newpass1"}).status_code == 400
    assert authed.post("/api/password", json={"old": "secret123", "new": "newpass1"}).status_code == 200
    assert authed.get("/api/routes").status_code == 200   # this session got a fresh cookie
    assert other.get("/api/routes").status_code == 401    # the other one is gone


def test_ui_and_health_are_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/app.js").status_code == 200


# ---------------------------------------------------------------- settings

def test_secrets_are_masked_and_kept(authed):
    r = authed.put("/api/settings", json={"bot_token": "123456789:ABCDEFGHIJKLMNOP", "transport": "botapi"})
    assert r.status_code == 200
    body = r.json()
    assert "bot_token" not in body
    assert body["bot_token_hint"] == "1234…MNOP"

    authed.put("/api/settings", json={"bot_token": "", "max_attempts": "5"})
    assert db.get_setting("bot_token") == "123456789:ABCDEFGHIJKLMNOP"
    assert db.get_setting("max_attempts") == "5"


def test_settings_validation(authed):
    assert authed.put("/api/settings", json={"botapi_proxy": "ftp://x"}).status_code == 400
    assert authed.put("/api/settings", json={"transport": "pigeon"}).status_code == 400
    assert authed.put("/api/settings", json={"max_attempts": "0"}).status_code == 400
    assert authed.put("/api/settings", json={"mtproto_port": "abc"}).status_code == 400
    r = authed.put("/api/settings", json={"botapi_proxy": "socks5h://proxy:1080", "public_url": "http://r.lan/"})
    assert r.status_code == 200
    assert r.json()["botapi_proxy"] == "socks5h://proxy:1080"
    assert r.json()["public_url"] == "http://r.lan"


def test_unknown_settings_are_ignored(authed):
    authed.put("/api/settings", json={"password_hash": "x", "session_secret": "y"})
    assert db.get_setting("password_hash") != "x"
    assert db.get_setting("session_secret") != "y"


# ---------------------------------------------------------------- routes

def test_route_crud(authed):
    route = make_route(authed, thread_id=5, split=True)
    assert route["template"].strip()  # empty template -> default
    assert len(route["token"]) >= 20
    assert route["thread_id"] == 5 and route["split"] is True and route["enabled"] is True

    r = authed.put(f"/api/routes/{route['id']}",
                   json={"name": "Ops 2", "chat_id": "@ops", "template": "{{ message }}", "enabled": False})
    assert r.json()["name"] == "Ops 2" and r.json()["enabled"] is False

    new = authed.post(f"/api/routes/{route['id']}/regen-token").json()
    assert new["token"] != route["token"]

    assert len(authed.get("/api/routes").json()) == 1
    authed.delete(f"/api/routes/{route['id']}")
    assert authed.get("/api/routes").json() == []
    assert authed.put(f"/api/routes/{route['id']}", json={"name": "x", "chat_id": "1"}).status_code == 404


def test_route_rejects_broken_template(authed):
    r = authed.post("/api/routes", json={"name": "x", "chat_id": "1", "template": "{% for %}"})
    assert r.status_code == 400
    assert "шаблон" in r.json()["detail"].lower()


def test_route_validation(authed):
    assert authed.post("/api/routes", json={"name": "", "chat_id": "1"}).status_code == 422


def test_preview(authed):
    r = authed.post("/api/preview", json={"template": "{{ alerts | length }} {{ status }}"}).json()
    assert r["ok"] and r["messages"] == [{"text": "2 firing", "silent": False}]

    r = authed.post("/api/preview", json={"template": "{{ x.y.z }}"}).json()
    assert r["ok"] is False and "Undefined" in r["error"]


def test_preview_last_payload(authed, sent):
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", json={"message": "real one"})
    r = authed.post("/api/preview", json={"template": "{{ message }}", "source": "last",
                                          "route_id": route["id"]}).json()
    assert r["messages"][0]["text"] == "real one"
    assert authed.get(f"/api/routes/{route['id']}/payload").json()["payload"] == {"message": "real one"}


# ---------------------------------------------------------------- webhook + worker

def test_unknown_hook(client):
    assert client.post("/hook/nope", json={}).status_code == 404


def test_hook_delivers_grafana_alerts(authed, sent):
    route = make_route(authed, split=True, silent_resolved=True, thread_id=9)
    r = authed.post(f"/hook/{route['token']}", json=SAMPLE_PAYLOAD)
    assert r.json() == {"ok": True, "queued": 2}

    wait_for(lambda: len(sent) == 2)
    assert [m["silent"] for m in sent] == [False, True]   # only the resolved one is silent
    assert all(m["chat_id"] == "-100123" and m["thread_id"] == 9 for m in sent)
    assert "HighCPU" in sent[0]["text"] and "DiskFull" in sent[1]["text"]

    msgs = wait_for(lambda: [m for m in authed.get("/api/messages").json() if m["status"] == "sent"])
    assert len(msgs) == 2
    assert authed.get("/api/stats").json()["sent_24h"] == 2


def test_hook_accepts_plain_text(authed, sent):
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", content=b"backup <failed>", headers={"Content-Type": "text/plain"})
    wait_for(lambda: sent)
    assert sent[0]["text"] == "backup &lt;failed&gt;"


def test_disabled_route_does_not_send(authed, sent):
    route = make_route(authed, enabled=False)
    assert authed.post(f"/hook/{route['token']}", json=SAMPLE_PAYLOAD).json()["queued"] == 0
    assert authed.get("/api/routes").json()[0]["last_hit"] is not None
    time.sleep(0.3)
    assert sent == []


def test_template_runtime_error_still_notifies(authed, sent):
    # works on the sample payload (so it is accepted), but fails on a payload without alerts
    route = make_route(authed, template="{{ alerts[0].labels.alertname }}")
    authed.post(f"/hook/{route['token']}", json={"foo": "<bar>"})
    wait_for(lambda: sent)
    assert "Ошибка шаблона" in sent[0]["text"]
    assert "&lt;bar&gt;" in sent[0]["text"]


def test_transient_failure_is_retried(authed, monkeypatch):
    attempts = []

    async def flaky(cfg, chat_id, text, thread_id=None, silent=False):
        attempts.append(time.time())
        if len(attempts) == 1:
            raise TelegramError("proxy down", retry=True, retry_after=1)

    monkeypatch.setattr(main.sender, "send", flaky)
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", json={"message": "hi"})

    msg = wait_for(lambda: [m for m in authed.get("/api/messages").json() if m["status"] == "sent"])[0]
    assert msg["attempts"] == 2
    assert attempts[1] - attempts[0] >= 0.9


def test_permanent_failure_and_manual_retry(authed, monkeypatch):
    calls = []

    async def forbidden(cfg, chat_id, text, thread_id=None, silent=False):
        calls.append(1)
        if len(calls) == 1:
            raise TelegramError("403: Forbidden", retry=False)

    monkeypatch.setattr(main.sender, "send", forbidden)
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", json={"message": "hi"})

    failed = wait_for(lambda: authed.get("/api/messages?status=failed").json())
    assert failed[0]["error"] == "403: Forbidden" and failed[0]["attempts"] == 1
    assert authed.get("/api/stats").json()["failed_24h"] == 1

    authed.post(f"/api/messages/{failed[0]['id']}/retry")
    wait_for(lambda: authed.get("/api/messages?status=sent").json())


def test_max_attempts_reached(authed, monkeypatch):
    async def down(cfg, chat_id, text, thread_id=None, silent=False):
        raise TelegramError("timeout", retry=True, retry_after=0.1)

    monkeypatch.setattr(main.sender, "send", down)
    authed.put("/api/settings", json={"max_attempts": "2"})
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", json={"message": "hi"})
    failed = wait_for(lambda: authed.get("/api/messages?status=failed").json())
    assert failed[0]["attempts"] == 2


def test_route_test_button(authed, sent):
    route = make_route(authed)
    assert authed.post(f"/api/routes/{route['id']}/test").json() == {"ok": True}
    assert "Тестовое сообщение" in sent[0]["text"]
    log = authed.get("/api/messages").json()
    assert log[0]["route_name"] == "Ops (тест)" and log[0]["status"] == "sent"


def test_clear_log_keeps_pending(authed, monkeypatch):
    async def never(cfg, chat_id, text, thread_id=None, silent=False):
        raise TelegramError("down", retry=True, retry_after=3600)

    monkeypatch.setattr(main.sender, "send", never)
    route = make_route(authed)
    authed.post(f"/hook/{route['token']}", json={"message": "hi"})
    wait_for(lambda: authed.get("/api/messages").json()[0]["attempts"] == 1)
    authed.delete("/api/messages")
    assert [m["status"] for m in authed.get("/api/messages").json()] == ["pending"]
