# app/main.py
import sys
import logging
from app.mcp.server import mcp_server, tool_registry

# IMPORTANT: import tool modules so @register_tool executes.
# If you add more tool files later, import them here too.
from app.mcp.tools import oauth_auth as _oauth_auth  # noqa: F401

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    if "--mcp-stdio" in sys.argv:
        logging.info("MCP starting (stdio)")
        logging.info("Tools: %s", ", ".join(tool_registry.keys()) or "(none)")
        mcp_server.run(transport="stdio")
