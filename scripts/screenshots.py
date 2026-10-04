"""Regenerates docs/screenshots/*.png from a demo instance.

Everything is fake: a stub Telegram Bot API server answers the relay, so no real
token, chat or network access is involved.

    pip install -r requirements.txt playwright
    python scripts/screenshots.py            # uses the installed Edge or Chrome
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tg-relay-demo-")
os.environ.setdefault("TZ", "UTC")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import main as relay  # noqa: E402

RELAY_PORT, TG_PORT = 18095, 18099
RELAY = f"http://127.0.0.1:{RELAY_PORT}"
PASSWORD = "demo-password"
KICKED_CHAT = -1005550000000

# ---------------------------------------------------------------- stub Telegram Bot API

tg = FastAPI()


@tg.post("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request):
    body = await request.json()
    if method == "getMe":
        return {"ok": True, "result": {"id": 7000000001, "is_bot": True, "first_name": "Grafana Alerts",
                                       "username": "grafana_alerts_demo_bot"}}
    if method == "getUpdates":
        chat = {"id": -1001987654321, "title": "Infra alerts", "type": "supergroup"}
        return {"ok": True, "result": [{"update_id": 1, "message": {"chat": chat}}]}
    if method == "sendMessage":
        if body["chat_id"] == KICKED_CHAT:
            return JSONResponse({"ok": False, "error_code": 403,
                                 "description": "Forbidden: bot was kicked from the supergroup chat"}, 403)
        if "CertificateExpiry" in body["text"]:
            return JSONResponse({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1800",
                                 "parameters": {"retry_after": 1800}}, 429)
        return {"ok": True, "result": {"message_id": 1}}
    return {"ok": True, "result": True}


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz" if app is relay.app else f"http://127.0.0.1:{port}/docs")
            return server
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"server on {port} did not start")


# ---------------------------------------------------------------- demo data

NOW = datetime.now(timezone.utc)


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def alert(name, status, labels, summary, started, ended=None, description=None, values=None):
    return {
        "status": status,
        "labels": {"alertname": name, **labels},
        "annotations": {k: v for k, v in (("summary", summary), ("description", description)) if v},
        "startsAt": ts(started),
        "endsAt": ts(ended) if ended is not None else "0001-01-01T00:00:00Z",
        "generatorURL": "https://grafana.example.com/alerting/grafana/x/view",
        "panelURL": "https://grafana.example.com/d/node?viewPanel=3",
        "values": values or {},
    }


def payload(*alerts):
    return {"receiver": "telegram", "status": "firing" if any(a["status"] == "firing" for a in alerts) else "resolved",
            "alerts": list(alerts), "externalURL": "https://grafana.example.com/"}


def seed():
    c = httpx.Client(base_url=RELAY)
    c.post("/api/auth/setup", json={"password": PASSWORD}).raise_for_status()
    c.put("/api/settings", json={
        "transport": "botapi", "bot_token": "7000000001:AAH-demo-token-not-real",
        "botapi_base": f"http://127.0.0.1:{TG_PORT}", "botapi_proxy": "", "public_url": "http://relay.lan:8095",
    }).raise_for_status()

    def route(**kw):
        r = c.post("/api/routes", json=kw)
        r.raise_for_status()
        return r.json()

    infra = route(name="Инфраструктура · критичные", chat_id="-1001987654321", split=True)
    home = route(name="Домашний сервер", chat_id="-1001122334455", thread_id=12, silent_resolved=True)
    backups = route(name="Бэкапы", chat_id=str(KICKED_CHAT))
    route(name="Staging", chat_id="-1003334445556", enabled=False)

    def hook(r, body):
        c.post(f"/hook/{r['token']}", json=body).raise_for_status()
        time.sleep(0.4)

    hook(home, payload(alert("DiskFull", "resolved", {"severity": "warning", "instance": "nas-01:9100",
                                                     "mountpoint": "/srv/media"},
                             "Диск заполнен более чем на 85%", 190, 95, values={"B": 71.4})))
    hook(backups, payload(alert("BackupFailed", "firing", {"severity": "critical", "job": "restic-nightly"},
                                "Ночной бэкап завершился с ошибкой", 60)))
    hook(home, payload(alert("CertificateExpiry", "firing", {"severity": "warning", "domain": "cloud.example.com"},
                             "Сертификат истекает через 6 дней", 30)))
    hook(infra, payload(
        alert("HighCPU", "firing", {"severity": "critical", "instance": "web-01:9100"},
              "Загрузка CPU выше 90%", 14, description="web-01 загружен на 96% последние 10 минут",
              values={"A": 96.3}),
        alert("MemoryPressure", "firing", {"severity": "warning", "instance": "db-01:9100"},
              "Свободной памяти меньше 10%", 9, values={"A": 7.8}),
    ))
    hook(home, payload(alert("HighTemperature", "resolved", {"severity": "warning", "sensor": "cpu_thermal"},
                             "Температура CPU выше 75 °C", 48, 3, values={"T": 61.2})))
    time.sleep(2)


# ---------------------------------------------------------------- screenshots

def shoot():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch(channel="chrome")

        def context(scheme):
            return browser.new_context(viewport={"width": 1360, "height": 860}, device_scale_factor=2,
                                       color_scheme=scheme, locale="ru-RU", timezone_id="UTC")

        # login screen
        page = context("light").new_page()
        page.goto(RELAY)
        page.wait_for_selector("#loginForm")
        page.screenshot(path=OUT / "login.png")

        for scheme in ("dark", "light"):
            page = context(scheme).new_page()
            page.goto(RELAY)
            page.fill("#loginPassword", PASSWORD)
            page.press("#loginPassword", "Enter")
            page.wait_for_selector(".route")
            page.wait_for_selector(".status-pill.ok")
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"routes-{scheme}.png")

            # route editor with live preview
            page.locator(".route").first.get_by_role("button", name="Изменить").click()
            page.wait_for_selector("#preview .bubble")
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"editor-{scheme}.png")
            page.keyboard.press("Escape")

            # delivery log with a delivered and a failed message expanded
            page.click("#tabs button[data-tab=log]")
            page.wait_for_selector(".msg")
            page.locator(".msg-head").nth(0).click()
            page.locator(".msg .status.failed").first.click()
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"log-{scheme}.png")

            # connection settings
            page.click("#tabs button[data-tab=settings]")
            page.fill("input[name=botapi_proxy]", "socks5://host.docker.internal:1080")
            page.fill("input[name=botapi_base]", "https://api.telegram.org")
            page.wait_for_timeout(200)
            page.screenshot(path=OUT / f"settings-{scheme}.png")

        # mobile
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                                  color_scheme="dark", locale="ru-RU", is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(RELAY)
        page.fill("#loginPassword", PASSWORD)
        page.press("#loginPassword", "Enter")
        page.wait_for_selector(".status-pill.ok")
        page.wait_for_timeout(300)
        page.screenshot(path=OUT / "mobile-dark.png")
        browser.close()


if __name__ == "__main__":
    serve(tg, TG_PORT)
    serve(relay.app, RELAY_PORT)
    seed()
    shoot()
    for f in sorted(OUT.glob("*.png")):
        print(f.relative_to(ROOT), f"{f.stat().st_size // 1024} KB")
