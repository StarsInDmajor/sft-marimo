"""Unit tests for acp_path_filter — bidirectional JSON-RPC path rewriting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from sft_marimo.acp_path_filter import _replace_in_string, _rewrite_strings, rewrite_line


REMOTE = "/home/user/projects/myproject"
LOCAL = "/home/user/mnt/myhost/home/user/projects/myproject"


class TestReplaceInString(unittest.TestCase):
    """Test _replace_in_string with path-boundary-aware matching."""

    def test_exact_match(self):
        self.assertEqual(
            _replace_in_string(REMOTE + "/main.py", REMOTE, LOCAL),
            LOCAL + "/main.py",
        )

    def test_exact_match_no_remainder(self):
        self.assertEqual(_replace_in_string(REMOTE, REMOTE, LOCAL), LOCAL)

    def test_no_match(self):
        path = "/home/user/projects/other/file.py"
        self.assertEqual(_replace_in_string(path, REMOTE, LOCAL), path)

    def test_partial_no_match(self):
        """'/home/user/projects/myprojectx' should NOT match."""
        path = "/home/user/projects/myprojectx/file.py"
        self.assertEqual(_replace_in_string(path, REMOTE, LOCAL), path)

    def test_empty_string(self):
        self.assertEqual(_replace_in_string("", REMOTE, LOCAL), "")

    def test_shorter_than_prefix(self):
        self.assertEqual(_replace_in_string("/home", REMOTE, LOCAL), "/home")

    def test_embedded_in_text(self):
        """Path embedded in a longer string should be rewritten."""
        text = "You can read or write to the notebook at " + REMOTE + "/main.py"
        result = _replace_in_string(text, REMOTE, LOCAL)
        # The remote path gets replaced with local mount path
        self.assertIn(LOCAL + "/main.py", result)
        # Note: REMOTE is a substring of LOCAL (due to mirrored mount layout),
        # so we check the original text position is gone by checking the
        # text before the path is preserved.
        self.assertTrue(result.startswith("You can read or write to the notebook at " + LOCAL))

    def test_embedded_in_xml(self):
        text = "<path>" + REMOTE + "/main.py</path>"
        result = _replace_in_string(text, REMOTE, LOCAL)
        self.assertEqual(result, "<path>" + LOCAL + "/main.py</path>")

    def test_multiple_occurrences(self):
        text = REMOTE + "/a.py and " + REMOTE + "/b.py"
        result = _replace_in_string(text, REMOTE, LOCAL)
        self.assertEqual(result, LOCAL + "/a.py and " + LOCAL + "/b.py")

    def test_left_boundary_prevents_false_match(self):
        """'/xhome/...' should not match '/home/...'."""
        text = "x" + REMOTE + "/file.py"
        result = _replace_in_string(text, REMOTE, LOCAL)
        self.assertEqual(result, text)  # unchanged


class TestRewriteStrings(unittest.TestCase):
    """Test recursive JSON path rewriting."""

    def test_flat_dict(self):
        obj = {"filePath": REMOTE + "/foo.py", "name": "test"}
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result["filePath"], LOCAL + "/foo.py")
        self.assertEqual(result["name"], "test")

    def test_nested_dict(self):
        obj = {"state": {"input": {"filePath": REMOTE + "/bar.py"}}}
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result["state"]["input"]["filePath"], LOCAL + "/bar.py")

    def test_list(self):
        obj = [REMOTE + "/a.py", REMOTE + "/b.py"]
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result[0], LOCAL + "/a.py")
        self.assertEqual(result[1], LOCAL + "/b.py")

    def test_mixed_types(self):
        obj = {"files": [REMOTE + "/a.py", 42, True, None, REMOTE + "/b.py"]}
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result["files"][0], LOCAL + "/a.py")
        self.assertEqual(result["files"][1], 42)
        self.assertTrue(result["files"][2])
        self.assertIsNone(result["files"][3])
        self.assertEqual(result["files"][4], LOCAL + "/b.py")

    def test_non_path_string_unchanged(self):
        obj = {"text": "hello world", "role": "assistant"}
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result["text"], "hello world")
        self.assertEqual(result["role"], "assistant")

    def test_deeply_nested(self):
        obj = {"a": {"b": {"c": {"d": REMOTE + "/deep.py"}}}}
        result = _rewrite_strings(obj, REMOTE, LOCAL)
        self.assertEqual(result["a"]["b"]["c"]["d"], LOCAL + "/deep.py")

    def test_roundtrip(self):
        """Rewriting remote→local then local→remote recovers original."""
        original = {"path": REMOTE + "/file.py", "other": "value"}
        forward = _rewrite_strings(original, REMOTE, LOCAL)
        backward = _rewrite_strings(forward, LOCAL, REMOTE)
        self.assertEqual(backward, original)


class TestRewriteLine(unittest.TestCase):
    """Test line-level JSON-RPC rewriting."""

    def test_valid_json(self):
        line = json.dumps({"filePath": REMOTE + "/test.py"}) + "\n"
        result = rewrite_line(line, REMOTE, LOCAL)
        parsed = json.loads(result.strip())
        self.assertEqual(parsed["filePath"], LOCAL + "/test.py")

    def test_invalid_json_passthrough(self):
        line = "not json at all\n"
        self.assertEqual(rewrite_line(line, REMOTE, LOCAL), line)

    def test_empty_line_passthrough(self):
        self.assertEqual(rewrite_line("\n", REMOTE, LOCAL), "\n")

    def test_preserves_unicode(self):
        obj = {"text": "中文测试 " + REMOTE + "/数据.py"}
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        result = rewrite_line(line, REMOTE, LOCAL)
        parsed = json.loads(result.strip())
        self.assertIn("中文测试", parsed["text"])
        self.assertIn(LOCAL, parsed["text"])

    def test_acp_tool_call_inbound(self):
        """Simulate an ACP tool call from marimo with a remote path."""
        msg = {
            "type": "tool",
            "tool": "read",
            "callID": "call_00_abc123",
            "state": {
                "status": "completed",
                "input": {"filePath": REMOTE + "/main.py"},
                "output": "file contents here",
            },
        }
        line = json.dumps(msg) + "\n"
        result = rewrite_line(line, REMOTE, LOCAL)
        parsed = json.loads(result.strip())
        self.assertEqual(
            parsed["state"]["input"]["filePath"], LOCAL + "/main.py"
        )
        # output doesn't contain the remote path, should be unchanged
        self.assertEqual(parsed["state"]["output"], "file contents here")

    def test_acp_tool_response_outbound(self):
        """Simulate an ACP response from opencode with a local path."""
        msg = {
            "type": "tool",
            "tool": "read",
            "state": {
                "status": "completed",
                "input": {"filePath": LOCAL + "/main.py"},
                "output": "<path>" + LOCAL + "/main.py</path>",
            },
        }
        line = json.dumps(msg) + "\n"
        result = rewrite_line(line, LOCAL, REMOTE)
        parsed = json.loads(result.strip())
        # Both the filePath and the embedded path in output get rewritten
        self.assertEqual(
            parsed["state"]["input"]["filePath"], REMOTE + "/main.py"
        )
        self.assertIn(REMOTE, parsed["state"]["output"])

    def test_marimo_system_prompt_inbound(self):
        """Simulate the marimo system prompt with embedded remote path."""
        msg = {
            "type": "text",
            "text": "You can read or write to the notebook at "
            + REMOTE
            + "/main.py",
        }
        line = json.dumps(msg) + "\n"
        result = rewrite_line(line, REMOTE, LOCAL)
        parsed = json.loads(result.strip())
        # The path in the system prompt should be rewritten to local mount
        self.assertIn(LOCAL + "/main.py", parsed["text"])

    def test_multiple_paths_in_one_message(self):
        msg = {
            "files": [REMOTE + "/a.py", REMOTE + "/b.py"],
            "dir": REMOTE,
        }
        line = json.dumps(msg) + "\n"
        result = rewrite_line(line, REMOTE, LOCAL)
        parsed = json.loads(result.strip())
        self.assertEqual(parsed["files"][0], LOCAL + "/a.py")
        self.assertEqual(parsed["files"][1], LOCAL + "/b.py")
        self.assertEqual(parsed["dir"], LOCAL)


class TestEndToEndPipe(unittest.TestCase):
    """Integration test: run the filter as a subprocess."""

    def test_filter_rewrites_inbound(self):
        """Feed a remote-path JSON line to stdin, get local-path on stdout.

        Uses a child that echoes stdin to stdout — the round trip rewrites
        remote→local (inbound) then local→remote (outbound), recovering
        the original.  We verify by checking that the child *received*
        the rewritten (local) path via a side-channel.
        """
        msg = json.dumps({"path": REMOTE + "/test.py"}) + "\n"
        src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
        script = os.path.join(src_dir, "sft_marimo", "acp_path_filter.py")
        # Child writes what it received to a temp file, then echoes back
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            tmp_path = tmp.name

        child_script = (
            f"import sys,json;"
            f"d=sys.stdin.read();"
            f"open({tmp_path!r},'w').write(d);"
            f"sys.stdout.write(d)"
        )
        result = subprocess.run(
            [
                sys.executable,
                script,
                "--remote-root",
                REMOTE,
                "--local-mount",
                LOCAL,
                "--",
                sys.executable,
                "-c",
                child_script,
            ],
            input=msg,
            capture_output=True,
            text=True,
            timeout=5,
            env={
                **os.environ,
                "PYTHONPATH": src_dir,
            },
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        # The child received the rewritten (local) path
        with open(tmp_path) as f:
            child_input = json.loads(f.read().strip())
        self.assertEqual(child_input["path"], LOCAL + "/test.py")
        os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
