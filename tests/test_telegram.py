import asyncio
import json

import httpx
import pytest

from app.telegram import Sender, TelegramError

TOKEN = "123456:TEST-token"
CFG = {"transport": "botapi", "bot_token": TOKEN, "botapi_proxy": "", "botapi_base": "https://api.telegram.org"}


def make_sender(handler):
    s = Sender()
    s._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s._http_key = None  # matches the empty proxy in CFG, so the mock client is reused
    return s


def ok(result=True):
    return httpx.Response(200, json={"ok": True, "result": result})


def tg_error(code, description, **params):
    body = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return httpx.Response(code, json=body)


def run(coro):
    return asyncio.run(coro)


def test_send_message_payload():
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return ok({"message_id": 1})

    run(make_sender(handler).send(CFG, "-1001234567890", "<b>hi</b>", thread_id=7, silent=True))
    path, body = calls[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert body == {
        "chat_id": -1001234567890,
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": True,
        "message_thread_id": 7,
    }


def test_username_chat_is_kept_as_string():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return ok()

    run(make_sender(handler).send(CFG, "@my_channel", "x"))
    assert calls[0]["chat_id"] == "@my_channel"
    assert "message_thread_id" not in calls[0]


def test_custom_api_base():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return ok()

    run(make_sender(handler).send({**CFG, "botapi_base": "https://tg.example.com/"}, "1", "x"))
    assert urls[0] == f"https://tg.example.com/bot{TOKEN}/sendMessage"


def test_invalid_html_falls_back_to_plain_text():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return tg_error(400, "Bad Request: can't parse entities: unclosed tag")
        return ok()

    run(make_sender(handler).send(CFG, "1", "<b>CPU &gt; 90%"))
    assert len(bodies) == 2
    assert "parse_mode" not in bodies[1]
    assert bodies[1]["text"] == "CPU > 90%"


def test_rate_limit_is_retryable_with_delay():
    s = make_sender(lambda r: tg_error(429, "Too Many Requests: retry after 30", retry_after=30))
    with pytest.raises(TelegramError) as e:
        run(s.send(CFG, "1", "x"))
    assert e.value.retry is True
    assert e.value.retry_after == 30


def test_server_error_is_retryable():
    with pytest.raises(TelegramError) as e:
        run(make_sender(lambda r: tg_error(502, "Bad Gateway")).send(CFG, "1", "x"))
    assert e.value.retry is True


def test_forbidden_is_permanent():
    s = make_sender(lambda r: tg_error(403, "Forbidden: bot was kicked from the group chat"))
    with pytest.raises(TelegramError) as e:
        run(s.send(CFG, "1", "x"))
    assert e.value.retry is False
    assert "kicked" in str(e.value)


def test_network_error_is_retryable_and_token_is_redacted():
    def handler(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(TelegramError) as e:
        run(make_sender(handler).send(CFG, "1", "x"))
    assert e.value.retry is True
    assert TOKEN not in str(e.value)
    assert "<token>" in str(e.value)


def test_missing_token():
    with pytest.raises(TelegramError) as e:
        run(make_sender(lambda r: ok()).send({**CFG, "bot_token": ""}, "1", "x"))
    assert e.value.retry is False


def test_check_returns_bot_identity():
    s = make_sender(lambda r: ok({"id": 42, "is_bot": True, "first_name": "Alerts", "username": "alerts_bot"}))
    assert run(s.check(CFG)) == {"username": "alerts_bot", "id": 42, "name": "Alerts"}


def test_recent_chats_from_updates():
    updates = [
        {"update_id": 1, "message": {"chat": {"id": -100111, "title": "Ops", "type": "supergroup"},
                                     "is_topic_message": True, "message_thread_id": 5,
                                     "reply_to_message": {"forum_topic_created": {"name": "Alerts"}}}},
        {"update_id": 2, "channel_post": {"chat": {"id": -100222, "title": "News", "type": "channel"}}},
        {"update_id": 3, "message": {"chat": {"id": 77, "first_name": "Ann", "type": "private"}}},
        {"update_id": 4, "message": {"chat": {"id": 77, "first_name": "Ann", "type": "private"}}},
    ]
    chats = run(make_sender(lambda r: ok(updates)).recent_chats(CFG))
    assert chats == [
        {"chat_id": -100111, "title": "Ops", "type": "supergroup", "thread_id": 5, "topic": "Alerts"},
        {"chat_id": -100222, "title": "News", "type": "channel", "thread_id": None, "topic": None},
        {"chat_id": 77, "title": "Ann", "type": "private", "thread_id": None, "topic": None},
    ]


def test_recent_chats_not_available_for_mtproto():
    with pytest.raises(TelegramError):
        run(Sender().recent_chats({**CFG, "transport": "mtproto"}))


@pytest.mark.parametrize("cfg, needle", [
    ({"mtproto_api_id": "", "mtproto_api_hash": ""}, "api_id"),
    ({"mtproto_api_id": "1", "mtproto_api_hash": "h", "mtproto_host": ""}, "MTProxy"),
    ({"mtproto_api_id": "1", "mtproto_api_hash": "h", "mtproto_host": "p.example", "mtproto_port": "443",
      "mtproto_secret": "ee" + "00" * 16}, "Fake-TLS"),
])
def test_mtproto_config_validation(cfg, needle):
    with pytest.raises(TelegramError) as e:
        run(Sender().send({**CFG, "transport": "mtproto", **cfg}, "1", "x"))
    assert e.value.retry is False
    assert needle in str(e.value)
