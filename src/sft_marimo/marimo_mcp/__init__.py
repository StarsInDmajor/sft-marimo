"""Marimo MCP Server — exposes notebook tools to AI agents.

This package provides an MCP server that wraps marimo's HTTP API,
giving AI agents (via opencode) the ability to execute notebook cells,
inspect outputs, and run arbitrary code in the notebook kernel.

CLI entry point::

    MARIMO_URL=http://localhost:8686 MARIMO_TOKEN=abc marimo-mcp
"""


def main() -> None:
    """CLI entry point for the marimo MCP server."""
    from .server import serve

    serve()


__all__ = ["main"]
