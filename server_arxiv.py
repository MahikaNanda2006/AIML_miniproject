import asyncio
import contextlib
import json
import logging

import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from arxiv_client import ArxivClient
from tools import TOOL_REGISTRY

# stdout is the MCP channel in stdio mode, so log to stderr (the default handler does)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("arxiv-mcp")
client = ArxivClient()


@app.list_tools()
async def handle_list_tools():
    return [
        types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
        for t in TOOL_REGISTRY.values()
    ]


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")

    logger.info("CALL_TOOL %s %s", name, arguments)

    try:
        params = tool["builder"](arguments or {})
    except ValueError as e:
        result = {"error": "INVALID_ARGUMENTS", "detail": str(e)}
    else:
        result = await client.query(params)

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def serve_http(port: int):
    session_manager = StreamableHTTPSessionManager(app=app, json_response=False, stateless=False)

    async def asgi_router(scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["path"] == "/mcp":
            await session_manager.handle_request(scope, receive, send)
            return
        await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        routes=[Mount("/", app=asgi_router)],
        lifespan=lifespan,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            )
        ],
    )

    config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
    logger.info("arXiv MCP listening on http://localhost:%s/mcp", port)
    await uvicorn.Server(config).serve()


async def serve_stdio():
    async with stdio_server() as (read_stream, write_stream):
        logger.info("arXiv MCP listening on stdio")
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    asyncio.run(serve_http(args.port) if args.port else serve_stdio())
