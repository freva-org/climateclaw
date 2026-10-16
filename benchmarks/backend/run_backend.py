"""Run the real backend with MongoDB replaced by an in-memory store.

Everything else is the production code path. HTTP dependencies (Freva auth,
LiteLLM) are pointed at ``fake_upstream.py`` via the environment; no MCP
servers are configured, so no tools are offered to the model.
"""

from __future__ import annotations

import argparse
import os

UPSTREAM = os.getenv("BENCH_UPSTREAM", "http://127.0.0.1:9100")
os.environ.setdefault("CLIMATECLAW_LITE_LLM_ADDRESS", UPSTREAM)
os.environ.setdefault("CLIMATECLAW_AVAILABLE_MCP_SERVERS", "")

import uvicorn  # noqa: E402

from climateclaw.services.storage import mongodb_storage  # noqa: E402


class _Cursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, *args, **kwargs) -> "_Cursor":
        return self

    def skip(self, n: int) -> "_Cursor":
        self._docs = self._docs[n:]
        return self

    def limit(self, n: int) -> "_Cursor":
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        return self._docs if length is None else self._docs[:length]


def _matches(doc: dict, query: dict) -> bool:
    return all(doc.get(k) == v for k, v in query.items() if not k.startswith("$"))


class InMemoryCollection:
    """The subset of the pymongo collection API ThreadStorage uses."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def create_index(self, *args, **kwargs) -> None:
        pass

    async def find_one(self, query: dict, projection: dict | None = None):
        if "thread_id" in query:
            doc = self.docs.get(query["thread_id"])
            return doc if doc is not None and _matches(doc, query) else None
        return next((d for d in self.docs.values() if _matches(d, query)), None)

    def find(self, query: dict, projection: dict | None = None) -> _Cursor:
        return _Cursor([d for d in self.docs.values() if _matches(d, query)])

    async def count_documents(self, query: dict) -> int:
        return sum(1 for d in self.docs.values() if _matches(d, query))

    async def insert_one(self, doc: dict) -> None:
        self.docs[doc.get("thread_id", str(len(self.docs)))] = dict(doc)

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        doc = await self.find_one(query)
        if doc is None:
            if not upsert:
                return
            doc = dict(query)
            self.docs[doc["thread_id"]] = doc
        doc.update(update.get("$set", {}))

    async def delete_one(self, query: dict) -> None:
        doc = await self.find_one(query)
        if doc is not None:
            self.docs.pop(doc.get("thread_id"), None)


class InMemoryDB:
    def __init__(self) -> None:
        self._colls: dict[str, InMemoryCollection] = {}

    def __getitem__(self, name: str) -> InMemoryCollection:
        return self._colls.setdefault(name, InMemoryCollection())


class _NoClient:
    async def close(self) -> None:
        pass


async def _create_in_memory(cls):
    return cls(client=_NoClient(), db=InMemoryDB())


mongodb_storage.ThreadStorage.create = classmethod(_create_in_memory)  # type: ignore[method-assign,assignment]

if os.getenv("BENCH_SHARED_SSL_CONTEXT") == "1":
    # Experiment: the backend builds a new httpx.AsyncClient per outgoing call,
    # and each one loads the CA bundle into a fresh SSL context. Reuse one.
    import ssl

    import httpx

    _ctx = ssl.create_default_context()
    _orig_init = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs.setdefault("verify", _ctx)
        _orig_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _init  # type: ignore[method-assign]

from climateclaw.app import app  # noqa: E402,F401

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8502)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run(
        "run_backend:app",
        app_dir=os.path.dirname(__file__),
        host="127.0.0.1",
        port=args.port,
        workers=args.workers,
        proxy_headers=True,
    )
