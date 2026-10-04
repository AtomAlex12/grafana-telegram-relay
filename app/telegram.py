"""Telegram transports: Bot API over HTTP/SOCKS proxy, or MTProto (bot login) over MTProxy."""
import asyncio
import hashlib
import html
import logging
import re

import httpx

from . import db

log = logging.getLogger("relay.telegram")


class TelegramError(Exception):
    def __init__(self, message: str, retry: bool = True, retry_after: int | None = None):
        super().__init__(message)
        self.retry = retry
        self.retry_after = retry_after


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _chat(chat_id: str):
    chat_id = str(chat_id).strip()
    return int(chat_id) if re.fullmatch(r"-?\d+", chat_id) else chat_id


class Sender:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self._http_key = None
        self._mt = None
        self._mt_key = None

    async def close(self):
        if self._http:
            await self._http.aclose()
        await self._mt_reset()

    # ------------------------------------------------------------ public API

    async def send(self, cfg: dict, chat_id: str, text: str, thread_id: int | None = None, silent: bool = False):
        async with self._lock:
            if cfg.get("transport") == "mtproto":
                await self._mt_send(cfg, chat_id, text, thread_id, silent)
            else:
                await self._bot_send(cfg, chat_id, text, thread_id, silent)

    async def check(self, cfg: dict) -> dict:
        async with self._lock:
            if cfg.get("transport") == "mtproto":
                client = await self._mt_client(cfg)
                me = await client.get_me()
                return {"username": me.username, "id": me.id, "name": me.first_name}
            me = await self._bot_call(cfg, "getMe", {})
            return {"username": me.get("username"), "id": me.get("id"), "name": me.get("first_name")}

    async def recent_chats(self, cfg: dict) -> list[dict]:
        if cfg.get("transport") == "mtproto":
            raise TelegramError("Поиск чатов доступен только в режиме Bot API", retry=False)
        async with self._lock:
            updates = await self._bot_call(cfg, "getUpdates", {"limit": 100, "timeout": 0})
        chats: dict[tuple, dict] = {}
        for u in updates:
            for key in ("message", "channel_post", "edited_message", "my_chat_member", "edited_channel_post"):
                obj = u.get(key)
                if not obj or "chat" not in obj:
                    continue
                c = obj["chat"]
                thread = obj.get("message_thread_id") if obj.get("is_topic_message") else None
                topic = (obj.get("reply_to_message") or {}).get("forum_topic_created", {}).get("name") \
                    or (obj.get("forum_topic_created") or {}).get("name")
                title = c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")])) \
                    or c.get("username") or str(c["id"])
                chats[(c["id"], thread)] = {"chat_id": c["id"], "title": title, "type": c.get("type"),
                                            "thread_id": thread, "topic": topic}
        return list(chats.values())

    # ------------------------------------------------------------ Bot API

    def _http_client(self, cfg: dict) -> httpx.AsyncClient:
        proxy = (cfg.get("botapi_proxy") or "").strip() or None
        if self._http is None or self._http_key != proxy:
            if self._http:
                asyncio.get_running_loop().create_task(self._http.aclose())
            self._http = httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=httpx.Timeout(25, connect=12))
            self._http_key = proxy
        return self._http

    async def _bot_call(self, cfg: dict, method: str, payload: dict):
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            raise TelegramError("Не задан токен бота", retry=False)
        base = (cfg.get("botapi_base") or "https://api.telegram.org").rstrip("/")
        try:
            r = await self._http_client(cfg).post(f"{base}/bot{token}/{method}", json=payload)
        except httpx.HTTPError as e:
            msg = f"Сеть/прокси: {type(e).__name__}: {e}".replace(token, "<token>")
            raise TelegramError(msg, retry=True) from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"Неожиданный ответ HTTP {r.status_code}", retry=r.status_code >= 500) from None
        if not data.get("ok"):
            code = data.get("error_code") or r.status_code
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(f"{code}: {data.get('description')}", retry=code == 429 or code >= 500,
                                retry_after=retry_after)
        return data["result"]

    async def _bot_send(self, cfg, chat_id, text, thread_id, silent):
        payload = {
            "chat_id": _chat(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            await self._bot_call(cfg, "sendMessage", payload)
        except TelegramError as e:
            if "can't parse entities" not in str(e):
                raise
            log.warning("HTML rejected by Telegram, resending as plain text: %s", e)
            payload.pop("parse_mode")
            payload["text"] = _strip_html(text)
            await self._bot_call(cfg, "sendMessage", payload)

    # ------------------------------------------------------------ MTProto

    async def _mt_reset(self):
        if self._mt is not None:
            try:
                await self._mt.disconnect()
            except Exception:
                pass
        self._mt = None
        self._mt_key = None

    async def _mt_client(self, cfg: dict):
        from telethon import TelegramClient, connection

        token = (cfg.get("bot_token") or "").strip()
        api_id = (cfg.get("mtproto_api_id") or "").strip()
        api_hash = (cfg.get("mtproto_api_hash") or "").strip()
        host = (cfg.get("mtproto_host") or "").strip()
        port = (cfg.get("mtproto_port") or "").strip()
        secret = (cfg.get("mtproto_secret") or "").strip()
        if not token:
            raise TelegramError("Не задан токен бота", retry=False)
        if not (api_id.isdigit() and api_hash):
            raise TelegramError("Для MTProto нужны api_id и api_hash (my.telegram.org → API development tools)",
                                retry=False)
        if not (host and port.isdigit() and secret):
            raise TelegramError("Не заданы адрес, порт или секрет MTProxy", retry=False)
        if secret.lower().startswith("ee"):
            raise TelegramError("Fake-TLS секрет (ee…) не поддерживается — нужен секрет вида dd… или обычный",
                                retry=False)

        key = (token, api_id, api_hash, host, port, secret)
        if self._mt is not None and self._mt_key == key and self._mt.is_connected():
            return self._mt
        await self._mt_reset()

        conn = (connection.ConnectionTcpMTProxyRandomizedIntermediate if secret.lower().startswith("dd")
                else connection.ConnectionTcpMTProxyIntermediate)
        session = db.DATA_DIR / f"mtproto-{hashlib.sha256(token.encode()).hexdigest()[:12]}"
        client = TelegramClient(str(session), int(api_id), api_hash, connection=conn,
                                proxy=(host, int(port), secret), timeout=15, connection_retries=1,
                                retry_delay=2, receive_updates=False)
        try:
            await asyncio.wait_for(client.start(bot_token=token), timeout=45)
        except asyncio.TimeoutError:
            await client.disconnect()
            raise TelegramError("MTProxy: таймаут подключения", retry=True) from None
        except Exception as e:
            await client.disconnect()
            raise TelegramError(f"MTProxy: {type(e).__name__}: {e}".replace(token, "<token>"),
                                retry=isinstance(e, (ConnectionError, OSError))) from None
        self._mt, self._mt_key = client, key
        return client

    async def _mt_send(self, cfg, chat_id, text, thread_id, silent):
        from telethon import errors

        client = await self._mt_client(cfg)
        try:
            entity = await client.get_input_entity(_chat(chat_id))
            await client.send_message(entity, text, parse_mode="html", link_preview=False,
                                      reply_to=thread_id or None, silent=silent)
        except errors.FloodWaitError as e:
            raise TelegramError(f"FloodWait {e.seconds} с", retry=True, retry_after=e.seconds) from None
        except errors.RPCError as e:
            raise TelegramError(f"{type(e).__name__}: {e}", retry=False) from None
        except ValueError as e:
            raise TelegramError(f"Чат не найден: {e}. Бот должен быть добавлен в чат", retry=False) from None
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            await self._mt_reset()
            raise TelegramError(f"MTProxy: {type(e).__name__}: {e}", retry=True) from None
