"""cc-star MCP server entry point — run with: python -m cc_star.mcp"""
from cc_star.mcp.server import main

if __name__ == "__main__":
    import anyio
    anyio.run(main)
