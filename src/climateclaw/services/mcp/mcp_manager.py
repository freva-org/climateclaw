from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.settings import get_settings
from climateclaw.services.authentication.auth import Authenticator
from climateclaw.services.mcp.client import McpClient
from climateclaw.services.storage.helpers import get_mongodb_uri
from climateclaw.services.streaming.stream_variants import mcp_tool_to_openai_function

settings = get_settings()
DEFAULT_LOGGER = configure_logging(__name__)


Target = Literal[*settings.AVAILABLE_MCP_SERVERS]  # type: ignore[valid-type]
# Despite the specification of `Literal` forbidding this, this shows the valid values when debugging, so we keep it as is.


class McpManager:
    """
    Keeps one McpClient per target (rag-server/ code-server / web-search-server),
    initializes lazily, caches an MCP session id per logical conversation (handled
    by McpClient), and caches discovered tool schemas for export to LLM.

    Thread-safe for simple web workloads (single-process).
    """

    def __init__(
        self,
        *,
        servers: list,
        server_urls: dict[Target, str],
        default_headers: dict[str, str] | None = None,
        logger=None,
    ) -> None:
        self._lock = asyncio.Lock()
        self.log = logger or DEFAULT_LOGGER

        self._servers = servers
        self._server_urls = server_urls
        self._default_headers = {
            t: dict(default_headers or {}) or {} for t in self._servers
        }

        self._clients: dict[Target, McpClient | None] = dict.fromkeys(self._servers)

        # Cache of MCP tool descriptors and OpenAI tool schemas
        self._tools_by_target: dict[Target, list[dict[str, Any]]] = {
            t: [] for t in self._servers
        }
        self._openai_tools_cache: list[dict[str, Any]] | None = None

    # ────────── lifecycle ──────────

    async def close(self):
        async with self._lock:
            clients = [
                self._clients.get(s) for s in self._servers if self._clients.get(s)
            ]
            for s in self._servers:
                self._clients[s] = None

        for client in clients:
            try:
                await client.close()
            except Exception:
                self.log.exception("Failed to close MCP client")

    # ────────── internal clients ──────────

    async def _build_client(self, target: Target) -> McpClient:
        client = self._clients.get(target)
        if client is None:
            client = McpClient(
                self._server_urls.get(target, ""),
                default_headers=self._default_headers.get(target),
                logger=self.log,
            )
            self._clients[target] = client
        return client

    # ────────── initialization / discovery ──────────

    async def initialize(self, headers: dict | None = None) -> None:
        """
        Eagerly connect to MCP servers and discover tools so the LLM can be given
        the function schemas before first token is generated.
        Idempotent; safe to call multiple times.
        """
        try:
            async with self._lock:
                if headers:
                    for s in self._servers:
                        self._default_headers[s].update(headers.get(s, {}))

                for s in self._servers:
                    await self._build_client(s)

                    try:
                        await self._discover_tools(s)  # populates _tools_by_target[tgt]
                    except Exception as e:
                        self.log.warning(
                            "MCP tool discovery failed for %s: %s", s, e, exc_info=True
                        )

                self._openai_tools_cache = []
                for s in self._servers:
                    for t in self._tools_by_target[s]:
                        self._openai_tools_cache.append(mcp_tool_to_openai_function(t))

            self.log.info(
                f"MCP initialized. Tools discovered: total:{len(self._openai_tools_cache)} "
                + " ".join(
                    [
                        s + ":" + str(len(self._tools_by_target[s]))
                        for s in self._servers
                    ]
                )
            )
        except Exception as e:
            self.log.warning(
                "MCP manager initialization failed (tools may be unavailable): %s",
                e,
                exc_info=True,
            )

    async def _discover_tools(self, target: Target) -> None:
        """
        Ask the MCP server for available tools.
        Result shape is normalized to: [{"name":..., "description":..., "input_schema":{...}}, ...]
        """
        cli = self._clients.get(target)

        if cli is None:
            raise RuntimeError(f"MCP client not initialized for target={target}")

        tools: list[dict[str, Any]] = []

        res = await cli.tools_list_rpc()
        if res.ok and isinstance(res.result, dict):
            items = res.result.get("tools") or res.result.get("items") or res.result
            if isinstance(items, list):
                tools = items

        if not tools:
            raise RuntimeError(f"No tools discovered from MCP target={target}")

        normalized: list[dict[str, Any]] = []
        for tool in tools:
            name = tool.get("name") or tool.get("tool_name") or ""
            desc = tool.get("description") or ""
            schema = tool.get("input_schema") or tool.get("parameters") or {}
            normalized.append(
                {"name": name, "description": desc, "input_schema": schema}
            )

        self._tools_by_target[target] = normalized
        # invalidate merged cache
        self._openai_tools_cache = None

    async def get_server_from_tool(self, tool_name: str) -> Target | None:
        async with self._lock:
            for tgt in self._servers:
                for t in self._tools_by_target[tgt]:
                    if t.get("name") == tool_name:
                        return tgt
        return None

    # ────────── tool export to LLM ──────────

    async def openai_tools(self) -> list[dict[str, Any]]:
        """
        Return cached OpenAI-style tool schemas. Empty list if discovery failed.
        """
        async with self._lock:
            if self._openai_tools_cache is None:
                merged: list[dict[str, Any]] = []
                for tgt in self._servers:
                    for t in self._tools_by_target[tgt]:
                        merged.append(mcp_tool_to_openai_function(t))
                self._openai_tools_cache = merged
            return list(self._openai_tools_cache)

    # ────────── calling tools ──────────

    async def call_tool(
        self,
        target: Target | str,
        *,
        name: str,
        arguments: dict[str, Any],
        extra_headers: dict | None = None,
    ) -> dict[str, Any]:
        """
        Call a tool on the chosen target. If 'target' isn't in AVAILABLE_MCP_SERVERS,
        all the available servers are called as best-effort.
        """
        async with self._lock:
            if target in self._servers:
                client = self._clients.get(target)
                if client is None:
                    raise RuntimeError(
                        f"MCP client not initialized for target={target}"
                    )
                return await client.call_tool(
                    name=name, args=arguments, extra_headers=extra_headers
                )

            clients = [(tgt, self._clients.get(tgt)) for tgt in self._servers]

        for tgt, client in clients:
            if client is None:
                continue
            try:
                return await client.call_tool(
                    name=name, args=arguments, extra_headers=extra_headers
                )
            except Exception as e:
                self.log.debug("tool %s failed on %s: %s", name, tgt, e)

        raise RuntimeError(f"Tool invocation failed on all targets: {name}")

    async def cancel_tool_call(self, tool_name: str, reason: str | None = None) -> None:
        client_name = self.get_server_from_tool(tool_name=tool_name)
        client = self._clients.get(client_name)
        if client is None:
            return

        await client.cancel_request(reason)


# ──────────────────── Helper functions ──────────────────────────────


def get_mcp_headers(
    auth: Authenticator, cache: os.PathLike
) -> dict[str, dict[str, str | None]]:
    mongodb_uri = get_mongodb_uri()

    headers: dict[str, dict[str, str | None]] = {
        "rag-server": {
            "mongodb-uri": mongodb_uri,
        },
        "code-server": {
            "working-dir": str(cache),
        },
    }
    return headers
