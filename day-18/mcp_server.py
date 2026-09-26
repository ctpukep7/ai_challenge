"""MCP stdout is reserved for JSON-RPC; no background scheduler on import."""
from backend.mcp_service import run

if __name__ == "__main__":
    run()
