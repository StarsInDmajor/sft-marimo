"""HTTP + WebSocket client for a running marimo server.

Provides a synchronous, stateless API to:
- List notebook cells and their state
- Execute notebook cells (via POST /api/kernel/run + WebSocket output collection)
- Execute arbitrary code in scratchpad (via POST /api/kernel/execute SSE)
- Query cell outputs and metadata

Authentication:
  - Auth token: extracted from marimo startup log, passed via constructor
  - Skew token: lazily fetched from marimo HTML on first POST /api/kernel/run
  - Session ID: lazily fetched from GET /api/sessions

WebSocket protocol:
  - marimo's WS is server→client only; commands go via HTTP POST
  - We connect WS briefly to collect cell-op + completed-run notifications
  - Each run_cell() call: connect WS → HTTP run → collect WS output → disconnect
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --- Data types ---


@dataclass
class CellInfo:
    """Summary of a single notebook cell."""

    cell_id: str
    name: str
    code: str
    line_count: int

    def to_text(self) -> str:
        preview = self.code
        if len(preview) > 200:
            preview = preview[:197] + "..."
        return (
            f"  Cell ID: {self.cell_id}\n"
            f"  Name:    {self.name}\n"
            f"  Lines:   {self.line_count}\n"
            f"  Code:\n{_indent(preview, 4)}"
        )


@dataclass
class CellOutput:
    """A single output item from cell execution."""

    channel: str  # "stdout" | "stderr" | "output" | "marimo-error" | "media"
    mimetype: str  # e.g. "text/plain", "text/html", "application/vnd.marimo+error"
    data: Any  # str for text/*, dict/list for JSON mimetypes

    def to_text(self) -> str:
        if self.channel == "stdout":
            return f"[stdout] {self.data}"
        if self.channel == "stderr":
            return f"[stderr] {self.data}"
        if self.channel == "marimo-error":
            return _format_error(self.data)
        if isinstance(self.data, str):
            return self.data
        return json.dumps(self.data, indent=2, ensure_ascii=False)[:500]


@dataclass
class CellResult:
    """Result of executing a cell."""

    cell_id: str
    status: str  # "idle" | "error" | "interrupted"
    outputs: List[CellOutput] = field(default_factory=list)
    console: List[CellOutput] = field(default_factory=list)
    error: Optional[str] = None

    def to_text(self) -> str:
        lines = [f"Cell {self.cell_id}: {self.status}"]
        for out in self.console:
            lines.append(out.to_text())
        for out in self.outputs:
            lines.append(out.to_text())
        if self.error:
            lines.append(f"[error] {self.error}")
        return "\n".join(lines)


@dataclass
class ExecuteResult:
    """Result of scratchpad code execution (SSE)."""

    outputs: List[CellOutput] = field(default_factory=list)
    error: Optional[str] = None

    def to_text(self) -> str:
        lines = []
        for out in self.outputs:
            lines.append(out.to_text())
        if self.error:
            lines.append(f"[error] {self.error}")
        return "\n".join(lines) if lines else "(no output)"


# --- Helpers ---


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


def _format_error(data: Any) -> str:
    """Format marimo error data into readable text."""
    if isinstance(data, list):
        parts = []
        for item in data:
            if isinstance(item, dict):
                msg = item.get("msg", str(item))
                etype = item.get("exception_type", "")
                tb = item.get("traceback", "")
                parts.append(f"[{etype}] {msg}" if etype else msg)
                if tb:
                    # Strip HTML tags from traceback
                    clean = re.sub(r"<[^>]+>", "", tb)
                    parts.append(clean[:500])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(data)


def _parse_cell_output(raw: Any) -> CellOutput:
    """Parse a CellOutput JSON dict into CellOutput dataclass."""
    if isinstance(raw, dict):
        return CellOutput(
            channel=raw.get("channel", "output"),
            mimetype=raw.get("mimetype", "text/plain"),
            data=raw.get("data", ""),
        )
    return CellOutput(channel="output", mimetype="text/plain", data=str(raw))


def _parse_sse_stream(body: str) -> List[dict]:
    """Parse SSE body text into list of event dicts.

    SSE format:
        event: <type>
        data: <json>

        Or just:
        data: <json>
    """
    events = []
    current_data_lines: List[str] = []

    for line in body.splitlines():
        if line.startswith("data: "):
            current_data_lines.append(line[6:])
        elif line.startswith("data:"):
            current_data_lines.append(line[5:].strip())
        elif line.strip() == "":
            # End of event
            if current_data_lines:
                raw = "\n".join(current_data_lines)
                current_data_lines = []
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    # Could be a non-JSON data field (e.g. "done")
                    events.append({"raw": raw})
        elif line.startswith("event:"):
            pass  # We ignore event type, parse data only
        # else: ignore comment lines (starting with :)

    # Handle trailing event without blank line
    if current_data_lines:
        raw = "\n".join(current_data_lines)
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            events.append({"raw": raw})

    return events


# --- Main client ---


class MarimoClient:
    """Synchronous client for a running marimo server.

    Usage::

        client = MarimoClient("http://localhost:8686", auth_token="abc123")
        cells = client.list_cells()
        result = client.run_cell(cells[0].cell_id)
        print(result.to_text())
    """

    def __init__(self, base_url: str, auth_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self._skew_token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._file_key: Optional[str] = None
        # Cache kernel-ready data after first WS connection
        self._cell_cache: Optional[List[CellInfo]] = None
        self._cache_lock = threading.Lock()

    # --- Token management ---

    def _headers(self, include_skew: bool = False) -> Dict[str, str]:
        """Build HTTP headers with auth token."""
        h: Dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        if include_skew and self._skew_token:
            h["Marimo-Server-Token"] = self._skew_token
        if self._session_id:
            h["Marimo-Session-Id"] = self._session_id
        return h

    def _ensure_tokens(self) -> None:
        """Lazily fetch skew token and session ID if not yet cached."""
        if self._skew_token is None:
            self._skew_token = self._fetch_skew_token()
        if self._session_id is None:
            self._session_id, self._file_key = self._fetch_session()

    def _fetch_skew_token(self) -> str:
        """Extract skew protection token from marimo HTML page."""
        url = f"{self.base_url}/"
        req = urllib.request.Request(url)
        if self.auth_token:
            req.add_header("Authorization", f"Bearer {self.auth_token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            # No auth or no token — return empty (some endpoints don't need it)
            return ""

        # Try <marimo-server-token data-token="...">
        m = re.search(r'data-token="([^"]+)"', html)
        if m:
            return m.group(1)

        # Try serverToken in JSON config
        m = re.search(r'"serverToken"\s*:\s*"([^"]+)"', html)
        if m:
            return m.group(1)

        return ""

    def _fetch_session(self) -> tuple[str, Optional[str]]:
        """Fetch session ID and file key from /api/sessions."""
        url = f"{self.base_url}/api/sessions"
        req = urllib.request.Request(url)
        if self.auth_token:
            req.add_header("Authorization", f"Bearer {self.auth_token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, json.JSONDecodeError):
            return "", None

        # /api/sessions returns a dict: {session_id: session_info, ...}
        if isinstance(data, dict):
            for sid, info in data.items():
                file_key = info.get("file_key", None) if isinstance(info, dict) else None
                return sid, file_key
        elif isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return first.get("session_id", ""), first.get("file_key", None)

        return "", None

    # --- HTTP helpers ---

    def _get_json(self, path: str) -> Any:
        """GET JSON from marimo server."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        if self.auth_token:
            req.add_header("Authorization", f"Bearer {self.auth_token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, path: str, body: dict, include_skew: bool = False) -> Any:
        """POST JSON to marimo server."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in self._headers(include_skew=include_skew).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            if raw:
                return json.loads(raw)
            return {"success": True}

    def _post_sse(self, path: str, body: dict) -> str:
        """POST to marimo server and return raw SSE response body."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in self._headers(include_skew=False).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8")

    # --- WebSocket cell collector ---

    def _collect_ws_outputs(
        self,
        cell_ids: List[str],
        codes: Optional[List[str]] = None,
        timeout: float = 120,
    ) -> Dict[str, CellResult]:
        """Connect WebSocket, trigger run, collect outputs.

        Uses a WebSocket connection to receive cell-op notifications
        for the given cell_ids, then returns aggregated results.

        If the 'websockets' package is available, a real WS connection
        is used for accurate output collection.
        """
        self._ensure_tokens()

        # Try WebSocket-based collection if websockets is available
        try:
            return self._collect_ws_outputs_native(cell_ids, codes, timeout)
        except ImportError:
            pass

        # Fallback: fire-and-forget run + description of what happened
        return self._collect_outputs_fallback(cell_ids, codes, timeout)

    def _collect_ws_outputs_native(
        self,
        cell_ids: List[str],
        codes: Optional[List[str]],
        timeout: float,
    ) -> Dict[str, CellResult]:
        """Collect outputs using the websockets library."""
        import asyncio
        import websockets

        results: Dict[str, CellResult] = {
            cid: CellResult(cell_id=cid, status="queued") for cid in cell_ids
        }
        completed = threading.Event()

        async def _ws_listen() -> None:
            ws_url = self.base_url.replace("http", "ws") + "/ws"
            params = f"session_id={self._session_id}"
            if self._file_key:
                params += f"&file={self._file_key}"
            if self.auth_token:
                params += f"&access_token={self.auth_token}"
            full_url = f"{ws_url}?{params}"

            async with websockets.connect(full_url) as ws:
                # Receive kernel-ready (first message)
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                _data = json.loads(msg)

                # Now trigger execution via HTTP
                self._post_run_cells(cell_ids, codes)

                # Collect notifications
                deadline = time.time() + timeout
                while time.time() < deadline:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=min(remaining, 5)
                        )
                    except asyncio.TimeoutError:
                        # Check if all cells completed
                        if all(
                            r.status in ("idle", "error", "interrupted")
                            for r in results.values()
                        ):
                            break
                        continue

                    data = json.loads(msg)
                    op = data.get("op", "")
                    inner = data.get("data", data)

                    if op == "cell-op":
                        cid = inner.get("cell_id", "")
                        if cid in results:
                            self._update_cell_result(results[cid], inner)
                    elif op == "completed-run":
                        # Mark any still-queued cells as idle
                        for r in results.values():
                            if r.status == "queued" or r.status == "running":
                                r.status = "idle"
                        completed.set()
                        break

        # Run in a new event loop (this method is called synchronously)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_ws_listen())
        finally:
            loop.close()

        return results

    def _collect_outputs_fallback(
        self,
        cell_ids: List[str],
        codes: Optional[List[str]],
        timeout: float,
    ) -> Dict[str, CellResult]:
        """Fallback: run cells via fire-and-forget POST.

        Without websockets library, we can only fire-and-forget via
        POST /api/kernel/run.
        """
        self._post_run_cells(cell_ids, codes)
        return {
            cid: CellResult(
                cell_id=cid,
                status="idle",
                error=None,
            )
            for cid in cell_ids
        }

    def _post_run_cells(self, cell_ids: List[str], codes: Optional[List[str]] = None) -> Any:
        """POST /api/kernel/run to execute cells."""
        self._ensure_tokens()
        if codes is None:
            codes = [""] * len(cell_ids)
        return self._post_json(
            "/api/kernel/run",
            {"cellIds": cell_ids, "codes": codes},
            include_skew=True,
        )

    @staticmethod
    def _update_cell_result(result: CellResult, notification: dict) -> None:
        """Update a CellResult from a cell-op notification."""
        status = notification.get("status")
        if status:
            result.status = status

        # Parse output
        raw_output = notification.get("output")
        if raw_output:
            if isinstance(raw_output, list):
                result.outputs.extend(_parse_cell_output(o) for o in raw_output)
            else:
                result.outputs.append(_parse_cell_output(raw_output))

        # Parse console
        raw_console = notification.get("console")
        if raw_console:
            if isinstance(raw_console, list):
                result.console.extend(_parse_cell_output(o) for o in raw_console)
            else:
                result.console.append(_parse_cell_output(raw_console))

    # --- Public API ---

    def list_cells(self) -> List[CellInfo]:
        """List all cells in the active notebook.

        Connects WebSocket to receive kernel-ready, then disconnects.
        Caches the result for subsequent calls.
        """
        with self._cache_lock:
            if self._cell_cache is not None:
                return self._cell_cache

        cells = self._fetch_cells_via_ws()
        with self._cache_lock:
            self._cell_cache = cells
        return cells

    def _fetch_cells_via_ws(self) -> List[CellInfo]:
        """Fetch cell info by connecting WebSocket for kernel-ready."""
        try:
            return self._fetch_cells_via_ws_native()
        except ImportError:
            return self._fetch_cells_via_fallback()

    def _fetch_cells_via_ws_native(self) -> List[CellInfo]:
        """Fetch cells using websockets library."""
        import asyncio
        import websockets

        cells: List[CellInfo] = []

        async def _connect() -> None:
            self._ensure_tokens()
            ws_url = self.base_url.replace("http", "ws") + "/ws"
            params = f"session_id={self._session_id}"
            if self._file_key:
                params += f"&file={self._file_key}"
            if self.auth_token:
                params += f"&access_token={self.auth_token}"
            full_url = f"{ws_url}?{params}"

            async with websockets.connect(full_url) as ws:
                # First message is kernel-ready
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                data = json.loads(msg)
                inner = data.get("data", data)

                cell_ids = inner.get("cell_ids", [])
                codes = inner.get("codes", [])
                names = inner.get("names", [])

                for i, cid in enumerate(cell_ids):
                    code = codes[i] if i < len(codes) else ""
                    name = names[i] if i < len(names) else ""
                    cells.append(
                        CellInfo(
                            cell_id=cid,
                            name=name,
                            code=code,
                            line_count=code.count("\n") + 1 if code else 0,
                        )
                    )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_connect())
        finally:
            loop.close()

        return cells

    def _fetch_cells_via_fallback(self) -> List[CellInfo]:
        """Fallback when websockets is not available — parse notebook file.

        Uses GET /api/sessions to find the file path, then reads the .py
        file to extract cell definitions. Limited but functional.
        """
        # Try to get session info for file path
        try:
            data = self._get_json("/api/sessions")
            file_path = None
            if isinstance(data, dict):
                for _sid, info in data.items():
                    if isinstance(info, dict):
                        file_path = info.get("path") or info.get("file_key")
                        break
            elif isinstance(data, list) and data:
                file_path = data[0].get("path") if isinstance(data[0], dict) else None

            if file_path:
                # We can't read the remote file from here (it's on a different host)
                # Return a placeholder indicating cells need to be listed differently
                return [
                    CellInfo(
                        cell_id="(unknown)",
                        name="(unknown)",
                        code=f"# Could not read notebook: {file_path}\n# Install 'websockets' package for full cell listing.",
                        line_count=2,
                    )
                ]
        except Exception:
            pass

        return [
            CellInfo(
                cell_id="(unknown)",
                name="(unknown)",
                code="# Could not connect to marimo WebSocket.\n# Install 'websockets' package for full functionality.",
                line_count=2,
            )
        ]

    def run_cell(
        self,
        cell_id: str,
        code: Optional[str] = None,
        timeout: float = 120,
    ) -> CellResult:
        """Execute a notebook cell by ID.

        Optionally provide new code for the cell. Returns the execution
        result including stdout, stderr, output, and any errors.

        Updates the notebook UI (browser reflects the new cell state).
        """
        # Get current code if not provided
        if code is None:
            cells = self.list_cells()
            for c in cells:
                if c.cell_id == cell_id:
                    code = c.code
                    break
            if code is None:
                return CellResult(
                    cell_id=cell_id, status="error", error=f"Cell {cell_id} not found"
                )

        codes = [code]
        results = self._collect_ws_outputs([cell_id], codes=codes, timeout=timeout)

        # If we got empty results (fallback), also do a fire-and-forget run
        result = results.get(cell_id)
        if result is None:
            # Direct fire-and-forget
            self._post_run_cells([cell_id], codes)
            result = CellResult(
                cell_id=cell_id,
                status="idle",
                error="Cell execution triggered (output collection requires 'websockets' package)",
            )

        # Invalidate cell cache since execution may change state
        with self._cache_lock:
            self._cell_cache = None

        return result

    def run_cells(
        self,
        cell_ids: List[str],
        codes: Optional[List[str]] = None,
        timeout: float = 120,
    ) -> Dict[str, CellResult]:
        """Execute multiple notebook cells by ID."""
        if codes is None:
            all_cells = self.list_cells()
            code_map = {c.cell_id: c.code for c in all_cells}
            codes = [code_map.get(cid, "") for cid in cell_ids]

        results = self._collect_ws_outputs(cell_ids, codes=codes, timeout=timeout)
        if not results:
            # Fallback
            self._post_run_cells(cell_ids, codes)
            results = {
                cid: CellResult(
                    cell_id=cid,
                    status="idle",
                    error="Cell execution triggered (output collection requires 'websockets' package)",
                )
                for cid in cell_ids
            }

        with self._cache_lock:
            self._cell_cache = None

        return results

    def execute_code(self, code: str) -> ExecuteResult:
        """Execute arbitrary Python code in the notebook's kernel.

        Uses the scratchpad endpoint (POST /api/kernel/execute) which
        returns output via SSE. Does NOT update the notebook UI.

        Shares the kernel's globals — can access notebook variables.
        Does NOT require skew token.
        """
        result = ExecuteResult()
        try:
            body = self._post_sse("/api/kernel/execute", {"code": code})
        except urllib.error.HTTPError as e:
            result.error = f"HTTP {e.code}: {e.reason}"
            return result
        except Exception as e:
            result.error = str(e)
            return result

        if not body.strip():
            return result

        events = _parse_sse_stream(body)
        for event in events:
            if isinstance(event, dict):
                if "raw" in event:
                    # Non-JSON event (e.g. "done")
                    if event["raw"] == "done":
                        break
                else:
                    # Parse as cell output
                    channel = event.get("channel", "output")
                    mimetype = event.get("mimetype", "text/plain")
                    data = event.get("data", "")
                    result.outputs.append(
                        CellOutput(channel=channel, mimetype=mimetype, data=data)
                    )
                    # Check for errors
                    if channel == "marimo-error":
                        result.error = _format_error(data)

        return result

    def get_cell_info(self, cell_id: str) -> Optional[CellInfo]:
        """Get detailed info about a specific cell."""
        cells = self.list_cells()
        for c in cells:
            if c.cell_id == cell_id:
                return c
        return None

    def get_cell_output(self, cell_id: str) -> str:
        """Get the current output of a cell (best-effort, may be stale).

        Note: Without the websockets library or marimo's MCP server,
        we cannot query cell output directly. This method returns
        cell info instead.
        """
        info = self.get_cell_info(cell_id)
        if info is None:
            return f"Cell {cell_id} not found"
        return (
            f"Cell {cell_id} ({info.name}):\n"
            f"  Status: Use run_cell() to execute and see output\n"
            f"  Code ({info.line_count} lines):\n{_indent(info.code, 4)}"
        )
