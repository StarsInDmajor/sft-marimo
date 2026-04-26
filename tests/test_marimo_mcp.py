"""Tests for marimo_mcp — MarimoClient, SSE parsing, and MCP tool logic.

These tests exercise the MarimoClient (HTTP/WebSocket client for marimo)
without requiring a running marimo server or the ``mcp`` SDK package.
The MCP server tool wrappers (server.py) are tested indirectly through
MarimoClient since they are thin wrappers.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Adjust import path for test environment — import marimo_client directly
# to avoid importing the top-level __init__.py which imports server.py (needs mcp).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sft_marimo.marimo_mcp.marimo_client import (
    CellInfo,
    CellOutput,
    CellResult,
    ExecuteResult,
    MarimoClient,
    _format_error,
    _indent,
    _parse_cell_output,
    _parse_sse_stream,
)


# --- Helper tests ---


class TestIndent(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_indent("hello\nworld", 2), "  hello\n  world")

    def test_empty(self):
        self.assertEqual(_indent("", 4), "")


class TestFormatError(unittest.TestCase):
    def test_list_of_dicts(self):
        data = [
            {
                "type": "exception",
                "msg": "division by zero",
                "exception_type": "ZeroDivisionError",
                "traceback": "<pre>Traceback</pre>",
            }
        ]
        result = _format_error(data)
        self.assertIn("ZeroDivisionError", result)
        self.assertIn("division by zero", result)
        self.assertIn("Traceback", result)

    def test_string(self):
        self.assertEqual(_format_error("oops"), "oops")

    def test_empty_list(self):
        self.assertEqual(_format_error([]), "")

    def test_html_tags_stripped(self):
        data = [{"type": "exception", "msg": "x", "traceback": "<b>bold</b> <i>italic</i>"}]
        result = _format_error(data)
        self.assertNotIn("<b>", result)
        self.assertIn("bold", result)


class TestParseCellOutput(unittest.TestCase):
    def test_dict(self):
        raw = {"channel": "stdout", "mimetype": "text/plain", "data": "hello"}
        out = _parse_cell_output(raw)
        self.assertEqual(out.channel, "stdout")
        self.assertEqual(out.data, "hello")

    def test_string(self):
        out = _parse_cell_output("fallback")
        self.assertEqual(out.data, "fallback")
        self.assertEqual(out.channel, "output")


class TestParseSSEStream(unittest.TestCase):
    def test_simple_data(self):
        body = 'data: {"channel":"stdout","data":"hello"}\n\n'
        events = _parse_sse_stream(body)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"], "hello")

    def test_multiple_events(self):
        body = (
            'data: {"channel":"stdout","data":"line1"}\n\n'
            'data: {"channel":"output","data":"result"}\n\n'
        )
        events = _parse_sse_stream(body)
        self.assertEqual(len(events), 2)

    def test_event_type_ignored(self):
        body = 'event: cell-op\ndata: {"x":1}\n\n'
        events = _parse_sse_stream(body)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["x"], 1)

    def test_non_json_data(self):
        body = "data: done\n\n"
        events = _parse_sse_stream(body)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["raw"], "done")

    def test_multiline_data(self):
        body = 'data: {"key":\ndata: "value"}\n\n'
        events = _parse_sse_stream(body)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key"], "value")

    def test_empty_body(self):
        self.assertEqual(_parse_sse_stream(""), [])
        self.assertEqual(_parse_sse_stream("\n"), [])


# --- Data class tests ---


class TestCellInfo(unittest.TestCase):
    def test_to_text(self):
        info = CellInfo(cell_id="abc", name="cell_1", code="x = 1", line_count=1)
        text = info.to_text()
        self.assertIn("abc", text)
        self.assertIn("cell_1", text)
        self.assertIn("x = 1", text)

    def test_to_text_truncation(self):
        long_code = "x = 1\n" * 100
        info = CellInfo(cell_id="abc", name="c", code=long_code, line_count=100)
        text = info.to_text()
        self.assertIn("...", text)


class TestCellResult(unittest.TestCase):
    def test_to_text_empty(self):
        r = CellResult(cell_id="abc", status="idle")
        text = r.to_text()
        self.assertIn("abc", text)
        self.assertIn("idle", text)

    def test_to_text_with_outputs(self):
        r = CellResult(
            cell_id="abc",
            status="idle",
            outputs=[CellOutput(channel="output", mimetype="text/plain", data="42")],
            console=[CellOutput(channel="stdout", mimetype="text/plain", data="hello")],
        )
        text = r.to_text()
        self.assertIn("42", text)
        self.assertIn("[stdout] hello", text)


class TestExecuteResult(unittest.TestCase):
    def test_no_output(self):
        r = ExecuteResult()
        self.assertEqual(r.to_text(), "(no output)")

    def test_with_error(self):
        r = ExecuteResult(error="something failed")
        self.assertIn("something failed", r.to_text())


class TestCellOutput(unittest.TestCase):
    def test_stdout(self):
        out = CellOutput(channel="stdout", mimetype="text/plain", data="hello")
        self.assertIn("[stdout]", out.to_text())

    def test_stderr(self):
        out = CellOutput(channel="stderr", mimetype="text/plain", data="oops")
        self.assertIn("[stderr]", out.to_text())

    def test_error(self):
        out = CellOutput(channel="marimo-error", mimetype="application/json", data=[{"msg": "err"}])
        text = out.to_text()
        self.assertIn("err", text)


# --- MarimoClient tests (mocked HTTP) ---


class TestMarimoClientFetchSkewToken(unittest.TestCase):
    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_extracts_from_data_token(self, mock_urlopen):
        html = '<html><marimo-server-token data-token="skew123" hidden></marimo-server-token></html>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = html.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = MarimoClient("http://localhost:8686", "tok")
        token = client._fetch_skew_token()
        self.assertEqual(token, "skew123")

    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_extracts_from_server_token_json(self, mock_urlopen):
        html = '<html><script>Object.freeze({"serverToken":"json456"})</script></html>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = html.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = MarimoClient("http://localhost:8686", "tok")
        token = client._fetch_skew_token()
        self.assertEqual(token, "json456")

    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_returns_empty_on_http_error(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 401, "Unauthorized", {}, None
        )
        client = MarimoClient("http://localhost:8686", "tok")
        self.assertEqual(client._fetch_skew_token(), "")


class TestMarimoClientFetchSession(unittest.TestCase):
    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_parses_session_dict(self, mock_urlopen):
        sessions = {
            "sess-123": {"file_key": "notebook.py", "mode": "edit"}
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(sessions).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = MarimoClient("http://localhost:8686", "tok")
        sid, fkey = client._fetch_session()
        self.assertEqual(sid, "sess-123")
        self.assertEqual(fkey, "notebook.py")


class TestMarimoClientUpdateCellResult(unittest.TestCase):
    def test_updates_status_and_output(self):
        result = CellResult(cell_id="abc", status="queued")
        notification = {
            "status": "idle",
            "output": {"channel": "output", "mimetype": "text/plain", "data": "42"},
            "console": {"channel": "stdout", "mimetype": "text/plain", "data": "hello\n"},
        }
        MarimoClient._update_cell_result(result, notification)
        self.assertEqual(result.status, "idle")
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0].data, "42")
        self.assertEqual(len(result.console), 1)

    def test_list_console(self):
        result = CellResult(cell_id="abc", status="running")
        notification = {
            "console": [
                {"channel": "stdout", "mimetype": "text/plain", "data": "line1\n"},
                {"channel": "stderr", "mimetype": "text/plain", "data": "warn\n"},
            ],
        }
        MarimoClient._update_cell_result(result, notification)
        self.assertEqual(len(result.console), 2)


class TestMarimoClientExecuteCode(unittest.TestCase):
    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_parses_sse_response(self, mock_urlopen):
        sse_body = (
            'data: {"channel":"stdout","mimetype":"text/plain","data":"hello\\n"}\n\n'
            'data: done\n\n'
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = sse_body.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = MarimoClient("http://localhost:8686", "tok")
        result = client.execute_code("print('hello')")
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0].data, "hello\n")

    @patch("sft_marimo.marimo_mcp.marimo_client.urllib.request.urlopen")
    def test_handles_http_error(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 500, "Server Error", {}, None
        )
        client = MarimoClient("http://localhost:8686", "tok")
        result = client.execute_code("bad code")
        self.assertIn("HTTP 500", result.error)


class TestMarimoClientGetCellInfo(unittest.TestCase):
    def test_returns_matching_cell(self):
        client = MarimoClient("http://localhost:8686", "tok")
        # Pre-populate cache
        client._cell_cache = [
            CellInfo(cell_id="c1", name="cell_1", code="x=1", line_count=1),
            CellInfo(cell_id="c2", name="cell_2", code="y=2", line_count=1),
        ]
        info = client.get_cell_info("c2")
        self.assertIsNotNone(info)
        self.assertEqual(info.code, "y=2")

    def test_returns_none_for_missing(self):
        client = MarimoClient("http://localhost:8686", "tok")
        client._cell_cache = [
            CellInfo(cell_id="c1", name="cell_1", code="x=1", line_count=1),
        ]
        self.assertIsNone(client.get_cell_info("missing"))


class TestMarimoClientRunCellNotFound(unittest.TestCase):
    def test_cell_not_found(self):
        client = MarimoClient("http://localhost:8686", "tok")
        client._cell_cache = []
        result = client.run_cell("nonexistent")
        self.assertEqual(result.status, "error")
        self.assertIn("not found", result.error)


class TestMarimoClientHeaders(unittest.TestCase):
    def test_basic_headers(self):
        client = MarimoClient("http://localhost:8686", "tok123")
        h = client._headers()
        self.assertEqual(h["Authorization"], "Bearer tok123")

    def test_with_skew(self):
        client = MarimoClient("http://localhost:8686", "tok")
        client._skew_token = "skew456"
        client._session_id = "sess789"
        h = client._headers(include_skew=True)
        self.assertEqual(h["Marimo-Server-Token"], "skew456")
        self.assertEqual(h["Marimo-Session-Id"], "sess789")

    def test_no_auth_token(self):
        client = MarimoClient("http://localhost:8686", "")
        h = client._headers()
        self.assertNotIn("Authorization", h)


# --- MCP tool logic tests (via server module) ---

# The MCP tools call _get_client() which reads env vars.
# We test the underlying MarimoClient methods instead,
# and test the tool wrapper logic by calling them directly
# with a mocked client.


class TestMCPServerToolLogic(unittest.TestCase):
    """Test the logic that MCP tools implement, via MarimoClient."""

    def test_list_cells_empty(self):
        client = MarimoClient("http://localhost:8686", "tok")
        client._cell_cache = []
        cells = client.list_cells()
        self.assertEqual(len(cells), 0)

    def test_list_cells_cached(self):
        client = MarimoClient("http://localhost:8686", "tok")
        cached = [CellInfo(cell_id="c1", name="n1", code="x=1", line_count=1)]
        client._cell_cache = cached
        # Second call should return cache without hitting network
        cells = client.list_cells()
        self.assertEqual(cells, cached)

    def test_run_cell_uses_cached_code(self):
        """run_cell with no code arg should look up code from cache."""
        client = MarimoClient("http://localhost:8686", "tok")
        client._cell_cache = [
            CellInfo(cell_id="c1", name="n1", code="print(42)", line_count=1)
        ]
        # Mock the actual execution to avoid network calls
        with patch.object(client, "_collect_ws_outputs") as mock_collect:
            mock_collect.return_value = {
                "c1": CellResult(
                    cell_id="c1",
                    status="idle",
                    outputs=[
                        CellOutput(
                            channel="output",
                            mimetype="text/plain",
                            data="42",
                        )
                    ],
                )
            }
            result = client.run_cell("c1")
            self.assertEqual(result.status, "idle")
            self.assertEqual(result.outputs[0].data, "42")

    def test_execute_code_no_websockets_needed(self):
        """execute_code works via HTTP SSE, no websockets required."""
        client = MarimoClient("http://localhost:8686", "tok")
        with patch.object(client, "_post_sse") as mock_sse:
            mock_sse.return_value = (
                'data: {"channel":"output","mimetype":"text/plain","data":"42"}\n\n'
            )
            result = client.execute_code("2 + 2")
            self.assertEqual(len(result.outputs), 1)


if __name__ == "__main__":
    unittest.main()
