from climateclaw.services.mcp.client import McpCallResult
from climateclaw.services.mcp.mcp_manager import McpManager


class FakeMcpClient:
    def __init__(self, tools):
        self.tools = tools

    async def tools_list_rpc(self):
        return McpCallResult(ok=True, id="tools-list", result={"tools": self.tools})


async def discover_openai_tools(raw_tools):
    manager = McpManager(servers=["test-server"], server_urls={"test-server": ""})
    manager._clients["test-server"] = FakeMcpClient(raw_tools)

    await manager._discover_tools("test-server")

    return await manager.openai_tools()


async def test_discover_tools_accepts_camel_case_input_schema():
    tools = await discover_openai_tools(
        [
            {
                "name": "search",
                "description": "Search documents",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
    )

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search documents",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


async def test_discover_tools_keeps_existing_input_schema_fallbacks():
    tools = await discover_openai_tools(
        [
            {
                "tool_name": "web_search",
                "description": "Search the web",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
            {
                "name": "summarize",
                "description": "Summarize text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            },
        ]
    )

    assert tools[0]["function"]["name"] == "web_search"
    assert tools[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    assert tools[1]["function"]["name"] == "summarize"
    assert tools[1]["function"]["parameters"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }
