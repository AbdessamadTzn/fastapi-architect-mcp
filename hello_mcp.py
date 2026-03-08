from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
import asyncio

app = Server("hello-mcp")


@app.list_tools()
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=[
        Tool(
            name = "say_hello",
            description='Says hello to someone',
            inputSchema={
                "type": 'object',
                "propreties":{
                    "name": {"type": "string", "description": "Who to greet"}
                },
                "required": ["name"]
            }
        )
    ])

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    if name == "say_hello":
        greeting = f"Hello, {arguments['name']}!"
        return CallToolResult(content=[TextContent(type="text", text=greeting)])

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

asyncio.run(main())