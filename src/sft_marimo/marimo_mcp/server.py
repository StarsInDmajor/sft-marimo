"""MCP Server exposing marimo notebook tools to AI agents.

Runs as a stdio subprocess launched by opencode's MCP client.
Connection info (marimo URL, auth token) is passed via environment
variables set by ``_start_local_agent()``:

  MARIMO_URL   — e.g. ``http://localhost:8686``
  MARIMO_TOKEN — auth token (may be empty if marimo started with --no-token)

Usage::

    MARIMO_URL=http://localhost:8686 MARIMO_TOKEN=abc123 marimo-mcp

Tools provided:

  list_cells        — List all notebook cells (ID, name, code preview)
  run_cell          — Execute a cell by ID, return output
  execute_code      — Run arbitrary code in scratchpad, return output
  get_cell_info     — Detailed info about one cell
  get_cell_output   — Current cell output (best-effort)
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from .marimo_client import MarimoClient

# --- Server setup ---

mcp = MCPServer(
    name="marimo",
    title="Marimo Notebook Agent",
    description="Execute and inspect marimo notebook cells",
)


def _get_client() -> MarimoClient:
    """Create a MarimoClient from environment variables."""
    base_url = os.environ.get("MARIMO_URL", "http://localhost:8686")
    token = os.environ.get("MARIMO_TOKEN", "")
    return MarimoClient(base_url, token)


# --- Tools ---


@mcp.tool()
def list_cells() -> str:
    """List all cells in the active marimo notebook.

    Returns cell IDs, names, line counts, and code previews.
    Use this first to discover cell IDs before calling run_cell.
    """
    client = _get_client()
    cells = client.list_cells()
    if not cells:
        return "No cells found. Is a notebook open in marimo?"
    lines = [f"Notebook has {len(cells)} cells:\n"]
    for c in cells:
        lines.append(c.to_text())
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def run_cell(cell_id: str, code: str | None = None) -> str:
    """Execute a notebook cell by its ID and return the output.

    The cell must exist in the active notebook. Optionally provide new
    code to replace the cell's current code before executing.

    This updates the notebook UI — the browser will reflect the new cell
    state and output.

    Args:
        cell_id: The cell ID (from list_cells). Required.
        code: Optional new code for the cell. If omitted, uses existing code.
    """
    client = _get_client()
    result = client.run_cell(cell_id, code=code)

    parts = [f"Cell {cell_id}: {result.status}"]
    if result.console:
        parts.append("")
        for out in result.console:
            parts.append(out.to_text())
    if result.outputs:
        parts.append("")
        for out in result.outputs:
            parts.append(out.to_text())
    if result.error:
        parts.append("")
        parts.append(f"Error: {result.error}")

    return "\n".join(parts) if len(parts) > 1 else parts[0]


@mcp.tool()
def execute_code(code: str) -> str:
    """Execute arbitrary Python code in the notebook's kernel.

    The code runs in the same kernel as the notebook, so it can access
    all notebook variables and imports. Results are returned directly.

    This does NOT update the notebook UI — it uses marimo's scratchpad
    execution endpoint. Use run_cell() to update the notebook.

    Args:
        code: Python code to execute. Required.
    """
    client = _get_client()
    result = client.execute_code(code)
    return result.to_text()


@mcp.tool()
def get_cell_info(cell_id: str) -> str:
    """Get detailed information about a specific cell.

    Returns the cell's full code, name, and line count.

    Args:
        cell_id: The cell ID (from list_cells). Required.
    """
    client = _get_client()
    info = client.get_cell_info(cell_id)
    if info is None:
        return f"Cell {cell_id} not found. Use list_cells to see available cells."
    return (
        f"Cell ID: {info.cell_id}\n"
        f"Name:    {info.name}\n"
        f"Lines:   {info.line_count}\n"
        f"Code:\n{info.code}"
    )


@mcp.tool()
def get_cell_output(cell_id: str) -> str:
    """Get the current output of a cell without re-executing it.

    Note: Without the websockets library, this returns cell info only.
    Use run_cell() to execute and see fresh output.

    Args:
        cell_id: The cell ID (from list_cells). Required.
    """
    client = _get_client()
    return client.get_cell_output(cell_id)


# --- Entry point ---


def serve() -> None:
    """Run the MCP server with stdio transport."""
    mcp.run(transport="stdio")
