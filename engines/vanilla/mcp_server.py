"""MCP: the operations registry as tools, so an agent (Claude, Cursor, …) works the pipeline directly.

    creative-kit mcp                                 # stdio, for Claude Code / Claude Desktop (.mcp.json)
    creative-kit mcp --http --port 3210              # streamable HTTP, bearer token, for a hosted copy

One tool per operation in ops.py, generated, never hand-written: read operations are marked read-only
(clients can auto-allow them), write operations destructive. Who the agent acts as is fixed when the
server starts (PIPELINE_USER, or --as), never a tool argument, and access.py checks every write.
"""
from __future__ import annotations

import functools
import inspect
import secrets
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .ops import OPS, Context, Op

INSTRUCTIONS = """Creative automation pipeline: one campaign brief -> localized, on-brand ad creatives per market.
Call `help` first for the operations, or help(operation=...) for one operation's arguments and examples.
Start with `campaigns`, then `explain` a market to see which config file set each value, and `report` /
`deliverables` for the last run. `who` and `can` answer permission questions. `config_set` edits one value
(checked against the team permissions and audited); `run` re-renders a campaign (provider=mock costs nothing)."""


def _tool(o: Op, ctx: Context):
    @functools.wraps(o.fn)
    def call(**args: Any):
        try:
            return o(ctx, **args)
        except (PermissionError, FileNotFoundError, KeyError, ValueError) as e:
            raise ToolError(f"{'refused' if isinstance(e, PermissionError) else 'error'}: {e}") from e   # the agent sees why
    sig = inspect.signature(o.fn)
    call.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])   # ctx is ours, not the agent's
    call.__annotations__ = {k: v for k, v in o.fn.__annotations__.items() if k != "ctx"}
    return call


def build(ctx: Context) -> MCPServer:
    who = f" Acting as {ctx.user}." if ctx.user else " Read-only: no PIPELINE_USER, so write tools are refused."
    server = MCPServer("creative-kit", instructions=INSTRUCTIONS + who)
    for o in OPS.values():
        server.add_tool(_tool(o, ctx), name=o.name, description=inspect.getdoc(o.fn),
                        annotations=ToolAnnotations(read_only_hint=not o.write, destructive_hint=o.write, idempotent_hint=not o.write, open_world_hint=False))
    return server


def serve(ctx: Context, http: bool = False, port: int = 3210, token: str | None = None, host: str = "127.0.0.1") -> None:
    server = build(ctx)
    if not http:
        return server.run("stdio")
    import uvicorn
    from starlette.responses import JSONResponse

    token = token or secrets.token_urlsafe(24)
    app = server.streamable_http_app(stateless_http=True)

    async def guarded(scope, receive, send):                  # a bearer token in front of every request
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth != f"Bearer {token}":
                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        await app(scope, receive, send)
    print(f"MCP on http://{host}:{port}/mcp  ·  Authorization: Bearer {token}  ·  acting as {ctx.user or 'nobody (read-only)'}",
          file=sys.stderr)
    uvicorn.run(guarded, host=host, port=port, log_level="warning")
