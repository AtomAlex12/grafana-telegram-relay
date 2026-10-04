import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(data_dir):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/api/auth/setup", json={"password": "secret123"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def sent(monkeypatch):
    """Replaces the real Telegram sender; returns the list of delivered messages."""
    delivered = []

    async def fake_send(cfg, chat_id, text, thread_id=None, silent=False):
        delivered.append({"chat_id": chat_id, "text": text, "thread_id": thread_id, "silent": silent})

    monkeypatch.setattr(main.sender, "send", fake_send)
    return delivered
