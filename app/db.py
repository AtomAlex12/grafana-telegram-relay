"""SQLite storage: settings, routes and the outgoing message queue/log."""
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
LOG_KEEP = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  token           TEXT NOT NULL UNIQUE,
  chat_id         TEXT NOT NULL,
  thread_id       INTEGER,
  template        TEXT NOT NULL,
  split           INTEGER NOT NULL DEFAULT 0,
  silent_resolved INTEGER NOT NULL DEFAULT 0,
  enabled         INTEGER NOT NULL DEFAULT 1,
  last_payload    TEXT,
  last_hit        REAL,
  created         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created    REAL NOT NULL,
  route_id   INTEGER,
  route_name TEXT,
  chat_id    TEXT NOT NULL,
  thread_id  INTEGER,
  text       TEXT NOT NULL,
  silent     INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL,           -- pending | sent | failed
  attempts   INTEGER NOT NULL DEFAULT 0,
  next_try   REAL NOT NULL,
  error      TEXT,
  sent_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_due ON messages(status, next_try);
"""

DEFAULTS = {
    "transport": "botapi",
    "bot_token": "",
    "botapi_proxy": os.environ.get("INITIAL_PROXY_URL", ""),
    "botapi_base": "https://api.telegram.org",
    "mtproto_api_id": "",
    "mtproto_api_hash": "",
    "mtproto_host": "",
    "mtproto_port": "443",
    "mtproto_secret": "",
    "max_attempts": "30",
    "public_url": "",
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DATA_DIR / "relay.db", check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    with _lock:
        for k, v in DEFAULTS.items():
            _conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
        _conn.execute("INSERT OR IGNORE INTO settings VALUES ('session_secret', ?)", (secrets.token_hex(32),))


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _all(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def _one(sql: str, args=()) -> dict | None:
    rows = _all(sql, args)
    return rows[0] if rows else None


def _exec(sql: str, args=()) -> int:
    with _lock:
        return _conn.execute(sql, args).lastrowid


# ---------------------------------------------------------------- settings

def get_settings() -> dict:
    return {r["key"]: r["value"] for r in _all("SELECT key, value FROM settings")}


def get_setting(key: str) -> str | None:
    row = _one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


def set_settings(values: dict) -> None:
    with _lock:
        for k, v in values.items():
            _conn.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, "" if v is None else str(v)),
            )


# ---------------------------------------------------------------- routes

def _route_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    for k in ("split", "silent_resolved", "enabled"):
        r[k] = bool(r[k])
    return r


def list_routes() -> list[dict]:
    return [_route_row(r) for r in _all(
        "SELECT id, name, token, chat_id, thread_id, template, split, silent_resolved, enabled, last_hit, created,"
        " last_payload IS NOT NULL AS has_payload FROM routes ORDER BY id"
    )]


def get_route(route_id: int) -> dict | None:
    return _route_row(_one("SELECT * FROM routes WHERE id = ?", (route_id,)))


def get_route_by_token(token: str) -> dict | None:
    return _route_row(_one("SELECT * FROM routes WHERE token = ?", (token,)))


def create_route(data: dict) -> int:
    return _exec(
        "INSERT INTO routes (name, token, chat_id, thread_id, template, split, silent_resolved, enabled, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["name"], secrets.token_urlsafe(18), data["chat_id"], data.get("thread_id"), data["template"],
         int(data["split"]), int(data["silent_resolved"]), int(data["enabled"]), time.time()),
    )


def update_route(route_id: int, data: dict) -> None:
    _exec(
        "UPDATE routes SET name = ?, chat_id = ?, thread_id = ?, template = ?, split = ?, silent_resolved = ?,"
        " enabled = ? WHERE id = ?",
        (data["name"], data["chat_id"], data.get("thread_id"), data["template"], int(data["split"]),
         int(data["silent_resolved"]), int(data["enabled"]), route_id),
    )


def regen_token(route_id: int) -> None:
    _exec("UPDATE routes SET token = ? WHERE id = ?", (secrets.token_urlsafe(18), route_id))


def delete_route(route_id: int) -> None:
    _exec("DELETE FROM routes WHERE id = ?", (route_id,))


def touch_route(route_id: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > 200_000:
        raw = None
    _exec("UPDATE routes SET last_hit = ?, last_payload = COALESCE(?, last_payload) WHERE id = ?",
          (time.time(), raw, route_id))


# ---------------------------------------------------------------- messages

def enqueue(route: dict, text: str, silent: bool) -> int:
    now = time.time()
    return _exec(
        "INSERT INTO messages (created, route_id, route_name, chat_id, thread_id, text, silent, status, next_try)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (now, route["id"], route["name"], route["chat_id"], route.get("thread_id"), text, int(silent), now),
    )


def log_direct(route: dict, text: str, error: str | None) -> None:
    now = time.time()
    _exec(
        "INSERT INTO messages (created, route_id, route_name, chat_id, thread_id, text, status, attempts, next_try,"
        " error, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (now, route["id"], route["name"] + " (тест)", route["chat_id"], route.get("thread_id"), text,
         "failed" if error else "sent", now, error, None if error else now),
    )


def next_due(now: float) -> dict | None:
    return _one("SELECT * FROM messages WHERE status = 'pending' AND next_try <= ? ORDER BY id LIMIT 1", (now,))


def seconds_until_next(now: float) -> float | None:
    row = _one("SELECT MIN(next_try) AS t FROM messages WHERE status = 'pending'")
    return None if row is None or row["t"] is None else max(row["t"] - now, 0)


def mark_sent(msg_id: int, attempts: int) -> None:
    _exec("UPDATE messages SET status = 'sent', attempts = ?, sent_at = ?, error = NULL WHERE id = ?",
          (attempts, time.time(), msg_id))


def mark_retry(msg_id: int, attempts: int, next_try: float, error: str) -> None:
    _exec("UPDATE messages SET attempts = ?, next_try = ?, error = ? WHERE id = ?",
          (attempts, next_try, error, msg_id))


def mark_failed(msg_id: int, attempts: int, error: str) -> None:
    _exec("UPDATE messages SET status = 'failed', attempts = ?, error = ? WHERE id = ?", (attempts, error, msg_id))


def list_messages(status: str | None, limit: int) -> list[dict]:
    if status:
        return _all("SELECT * FROM messages WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
    return _all("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))


def retry_message(msg_id: int) -> None:
    _exec("UPDATE messages SET status = 'pending', attempts = 0, next_try = ?, error = NULL WHERE id = ?",
          (time.time(), msg_id))


def retry_all_now() -> None:
    _exec("UPDATE messages SET next_try = ? WHERE status = 'pending'", (time.time(),))


def clear_messages() -> None:
    _exec("DELETE FROM messages WHERE status != 'pending'")


def prune() -> None:
    _exec(
        "DELETE FROM messages WHERE status != 'pending' AND id NOT IN"
        " (SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
        (LOG_KEEP,),
    )


def stats() -> dict:
    day_ago = time.time() - 86400
    row = _one(
        "SELECT"
        " SUM(status = 'pending') AS pending,"
        " SUM(status = 'failed' AND created >= ?) AS failed_24h,"
        " SUM(status = 'sent' AND created >= ?) AS sent_24h,"
        " MAX(CASE WHEN status = 'sent' THEN sent_at END) AS last_sent"
        " FROM messages",
        (day_ago, day_ago),
    )
    last_err = _one("SELECT error, created FROM messages WHERE status = 'failed' ORDER BY id DESC LIMIT 1")
    return {
        "pending": row["pending"] or 0,
        "failed_24h": row["failed_24h"] or 0,
        "sent_24h": row["sent_24h"] or 0,
        "last_sent": row["last_sent"],
        "last_error": last_err,
    }
