# tests/conftest.py
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

import climateclaw.services.streaming.active_conversations as act_conv
from climateclaw.services.streaming.stream_variants import from_json_to_sv

# ──────────────────────────────────────────────────────────────────────────────
# GLOBAL / COMMON
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app(patch_thread_storage):
    # Reload settings after environment patching
    import importlib

    import climateclaw.core.settings as settings

    importlib.reload(settings)

    import climateclaw.services.service_factory as sf

    importlib.reload(sf)

    import climateclaw.app as app_module

    importlib.reload(app_module)

    _app = app_module.app
    _app.state.thread_storage = patch_thread_storage

    return _app


@pytest.fixture
def client(app):
    try:
        transport = httpx.ASGITransport(app=app, lifespan="on")  # httpx >= 0.28
    except TypeError:
        transport = httpx.ASGITransport(app=app)  # older httpx
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLIMATECLAW_HOST", "localhost")
    monkeypatch.setenv("CLIMATECLAW_BACKEND_PORT", "8502")
    monkeypatch.setenv("CLIMATECLAW_DEV", "0")  # for PROD-like auth
    yield


@pytest.fixture
def GOOD_HEADERS():
    return {
        "Authorization": "Bearer test-token",
        "x-freva-rest-url": "http://rest.example",
    }


# ──────────────────────────────────────────────────────────────────────────────
# NETWORK STUBS
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_resp(respx_mock):
    """
    Provide a default stub for the auth system call used in routes.
    Individual tests can override or add more routes to respx_mock.
    """
    respx_mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
        200, json={"username": "alice"}
    )
    return respx_mock


# ──────────────────────────────────────────────────────────────────────────────
# MONGODB FAKES and PATCHES
# ──────────────────────────────────────────────────────────────────────────────


class DummyCollection:
    def __init__(self):
        self.storage = {}

    class _Cursor:
        def __init__(self, docs):
            self._docs = docs
            self._limit = None

        def sort(self, *args, **kwargs):
            return self

        def limit(self, n):
            self._limit = n
            return self

        async def to_list(self, length):
            docs = list(self._docs.values())
            if length is not None:
                docs = docs[:length]
            return docs[: self._limit] if self._limit is not None else docs

    async def find_one(self, q, unique=None):
        return self.storage.get(q.get("thread_id"))

    def find(self, q):
        return self._Cursor(self.storage)

    async def insert_one(self, doc):
        self.storage[doc["thread_id"]] = doc
        return None

    async def update_one(self, query, update, upsert=False):
        tid = query.get("thread_id")
        doc = update.get("$set", update)
        self.storage[tid] = doc
        return None

    async def delete_one(self, query):
        tid = query.get("thread_id")
        self.storage.pop(tid, None)
        return None

    async def count_documents(self, q):
        user_id = q.get("user_id")
        if user_id is None:
            return len(self.storage)
        return sum(1 for doc in self.storage.values() if doc.get("user_id") == user_id)

    async def create_index(self, *args, **kwargs):
        pass


class DummyDB:
    def __init__(self):
        self._coll = DummyCollection()

    def __getitem__(self, name):
        return self._coll


@pytest.fixture
def dummy_db():
    return DummyDB()


@pytest.fixture
def patch_mongodb(monkeypatch, dummy_db):
    import climateclaw.services.storage.mongodb_storage as mongodb_storage

    class DummyMongoClient:
        def __init__(self, *args, **kwargs):
            self._db = dummy_db

        def __getitem__(self, name):
            return self._db

    monkeypatch.setattr(
        mongodb_storage,
        "AsyncMongoClient",
        DummyMongoClient,
    )

    monkeypatch.setattr(
        mongodb_storage,
        "get_mongodb_uri",
        lambda: "mongodb://dummy",
    )

    return dummy_db


@pytest.fixture
async def patch_thread_storage(patch_mongodb):
    from climateclaw.services.storage.mongodb_storage import ThreadStorage

    return await ThreadStorage.create()


@pytest.fixture
def patch_read_thread(monkeypatch):
    async def _fake(self, thread_id: str):
        return [
            {"variant": "ServerHint", "content": {"thread_id": thread_id}},
            {"variant": "Prompt", "content": "user prompt should be filtered out"},
            {"variant": "User", "content": "kept"},
            {"variant": "Assistant", "content": "also kept"},
        ]

    import climateclaw.services.storage.mongodb_storage as mongo_store

    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "read_thread",
        _fake,
        raising=False,
    )

    return _fake


@pytest.fixture
def patch_save_thread(monkeypatch):
    calls = []

    calls = []

    async def _fake_append(
        self,
        thread_id: str,
        user_id: str,
        content,
        root_thread_id=None,
        parent_thread_id=None,
        fork_from_index=None,
        append_to_existing=False,
        **kwargs,
    ):
        calls.append(
            {
                "thread_id": thread_id,
                "user_id": user_id,
                "content": content,
                "root_thread_id": root_thread_id,
                "parent_thread_id": parent_thread_id,
                "fork_from_index": fork_from_index,
                "append_to_existing": append_to_existing,
            }
        )
        return

    import climateclaw.services.storage.mongodb_storage as mongo_store

    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "save_thread",
        _fake_append,
        raising=False,
    )

    return calls


@pytest.fixture
def patch_user_threads(monkeypatch):
    async def fake_get_user_threads(self, user_id: str, limit: int = 20, page: int = 0):
        threads = [
            SimpleNamespace(
                user_id=user_id,
                thread_id="t-1",
                date="2025-01-01T00:00:00Z",
                topic="First thread",
                content="first content",
            ),
            SimpleNamespace(
                user_id=user_id,
                thread_id="t-2",
                date="2025-01-02T00:00:00Z",
                topic="Second thread",
                content="second content",
            ),
        ]
        return threads, len(threads)

    import climateclaw.services.storage.mongodb_storage as mongo_store

    monkeypatch.setattr(
        mongo_store.ThreadStorage,
        "list_recent_threads",
        fake_get_user_threads,
        raising=True,
    )

    return fake_get_user_threads


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRY PATCH
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_registry(monkeypatch):
    monkeypatch.setattr(act_conv, "RegistryLock", asyncio.Lock())
    act_conv.Registry.clear()

    def _populate(entries: dict[str, list[dict]]):
        """
        entries = {
            "t-2": [ {...}, {...}, {...} ],
        }
        """
        act_conv.Registry.clear()

        for thread_id, content in entries.items():
            act_conv.Registry[thread_id] = act_conv.ActiveConversation(
                thread_id=thread_id,
                user_id="u-test",
                state=act_conv.ConversationState.STREAMING,
                mcp_manager=None,
                messages=[from_json_to_sv(c) for c in content],
                last_activity=datetime.now(timezone.utc),
            )

    yield _populate

    act_conv.Registry.clear()


def register_fake_mcp(patch_registry, thread_id, fake_mcp):
    patch_registry({thread_id: []})
    act_conv.Registry[thread_id].mcp_manager = fake_mcp


# ──────────────────────────────────────────────────────────────────────────────
# STREAM PATCH
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_stream(monkeypatch):
    async def fake_run_stream(**kwargs):
        from climateclaw.services.streaming.stream_variants import (
            SVAssistant,
            SVServerHint,
        )

        yield SVServerHint(content={"thread_id": "t-abc"})
        yield SVAssistant(content="hello")
        return

    monkeypatch.setattr(
        "climateclaw.api.chatbot.streamresponse.run_stream",
        fake_run_stream,
        raising=True,
    )
    return fake_run_stream


# ──────────────────────────────────────────────────────────────────────────────
# MCP FAKES and PATCHES
# ──────────────────────────────────────────────────────────────────────────────


class DummyMcpManager:
    def __init__(self, tools=None):
        self._tools = tools or []

    async def available_tools(self):
        return self._tools

    async def close(self) -> None:
        pass


@pytest.fixture
def patch_mcp_manager(monkeypatch):
    """
    Avoid hitting the real MCP manager / MCP Mongo from tests.
    initialize_conversation() will still run, but with a dummy manager.
    """

    async def fake_get_mcp_manager(authenticator, thread_id):
        return DummyMcpManager()

    monkeypatch.setattr(act_conv, "get_mcp_manager", fake_get_mcp_manager, raising=True)
    return fake_get_mcp_manager
