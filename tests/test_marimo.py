"""Unit tests for marimo session management and pure helper functions."""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sft_lib = os.path.join(os.path.dirname(__file__), "..", "src")
sft_marimo_lib = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, sft_lib)

from sft.config import HostInfo
from sft.state import (
    save_session,
    load_session,
    list_sessions,
    remove_session,
    MARIMO_SESSIONS_DIR,
    _session_file,
)

# Import pure helpers from marimo module
from sft_marimo.marimo import (
    _find_free_port,
    _fmt_duration,
    _capture_marimo_token,
    _cleanup_session,
    _detect_remote_flake,
    _start_local_agent,
)


# --- Session state tests (using temp directory) ---


class TestSessionState(unittest.TestCase):
    """Test marimo session persistence via save/load/list/remove."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_dir = MARIMO_SESSIONS_DIR

    def tearDown(self):
        # Restore original dir
        import sft.state as state_mod

        state_mod.MARIMO_SESSIONS_DIR = self._orig_dir
        # Clean up temp files
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _patch_dir(self):
        import sft.state as state_mod

        state_mod.MARIMO_SESSIONS_DIR = self._tmpdir

    def test_save_and_load_session(self):
        self._patch_dir()
        session = {
            "host": "wsl-rs",
            "remote_path": "/home/user/project",
            "marimo_port": 8686,
            "status": "running",
        }
        sid = save_session(session)
        self.assertTrue(sid)
        self.assertEqual(session["id"], sid)

        loaded = load_session(sid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["host"], "wsl-rs")
        self.assertEqual(loaded["marimo_port"], 8686)

    def test_save_auto_generates_id(self):
        self._patch_dir()
        session = {
            "host": "wsl-rs",
            "remote_path": "/home/user/myproject",
        }
        sid = save_session(session)
        # Should be like "wsl-rs-myproject-260424"
        self.assertIn("wsl-rs", sid)
        self.assertIn("myproject", sid)

    def test_save_preserves_explicit_id(self):
        self._patch_dir()
        session = {"id": "custom-id", "host": "test"}
        sid = save_session(session)
        self.assertEqual(sid, "custom-id")

    def test_load_nonexistent_returns_none(self):
        self._patch_dir()
        self.assertIsNone(load_session("nonexistent"))

    def test_remove_session(self):
        self._patch_dir()
        session = {"id": "to-remove", "host": "test"}
        save_session(session)
        self.assertIsNotNone(load_session("to-remove"))
        remove_session("to-remove")
        self.assertIsNone(load_session("to-remove"))

    def test_remove_nonexistent_is_noop(self):
        self._patch_dir()
        # Should not raise
        remove_session("nonexistent")

    def test_list_sessions_empty(self):
        self._patch_dir()
        sessions = list_sessions()
        self.assertEqual(sessions, [])

    def test_list_sessions_multiple(self):
        self._patch_dir()
        save_session({"id": "session-a", "host": "a"})
        save_session({"id": "session-b", "host": "b"})
        sessions = list_sessions()
        ids = [s["id"] for s in sessions]
        self.assertIn("session-a", ids)
        self.assertIn("session-b", ids)

    def test_list_sessions_ignores_corrupt_file(self):
        self._patch_dir()
        # Write a corrupt JSON file
        corrupt_path = os.path.join(self._tmpdir, "corrupt.json")
        Path(corrupt_path).write_text("not json{{{")
        save_session({"id": "valid", "host": "ok"})
        sessions = list_sessions()
        ids = [s["id"] for s in sessions]
        self.assertIn("valid", ids)
        self.assertNotIn("corrupt", ids)

    def test_session_file_path(self):
        self._patch_dir()
        path = _session_file("my-session")
        self.assertTrue(path.endswith("my-session.json"))


class TestDetectRemoteFlake(unittest.TestCase):
    """Test _detect_remote_flake with mocked SSH."""

    def _make_host(self):
        return HostInfo(
            name="test",
            hostname="1.2.3.4",
            port=22,
            user="u",
            aliases=[],
            extra_options={},
        )

    def test_returns_flake_dir_when_envrc_found(self):
        ctx = MagicMock()
        # find_envrc_dir_remote returns a path
        # parse_envrc_flake_full_remote returns (flake_path, flags)
        with (
            patch(
                "sft_marimo.marimo.find_envrc_dir_remote",
                return_value="/home/user/project",
            ),
            patch(
                "sft_marimo.marimo.parse_envrc_flake_full_remote",
                return_value=("/home/user/project", ["--impure"]),
            ),
        ):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertEqual(flake_dir, "/home/user/project")
        self.assertEqual(flags, ["--impure"])

    def test_returns_none_when_no_envrc(self):
        ctx = MagicMock()
        with patch("sft_marimo.marimo.find_envrc_dir_remote", return_value=None):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertIsNone(flake_dir)
        self.assertEqual(flags, [])

    def test_returns_none_when_no_flake_directive(self):
        """No 'use flake' in .envrc AND no flake.nix — returns None."""
        ctx = MagicMock()
        ctx.run_ssh = MagicMock(side_effect=RuntimeError("not found"))
        with (
            patch(
                "sft_marimo.marimo.find_envrc_dir_remote",
                return_value="/home/user/project",
            ),
            patch(
                "sft_marimo.marimo.parse_envrc_flake_full_remote", return_value=None
            ),
        ):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertIsNone(flake_dir)
        self.assertEqual(flags, [])

    def test_uses_envrc_dir_when_flake_path_empty(self):
        """'use flake' without explicit path means flake.nix is in envrc dir."""
        ctx = MagicMock()
        with (
            patch(
                "sft_marimo.marimo.find_envrc_dir_remote",
                return_value="/home/user/project",
            ),
            patch(
                "sft_marimo.marimo.parse_envrc_flake_full_remote",
                return_value=("", []),
            ),
        ):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertEqual(flake_dir, "/home/user/project")

    def test_fallback_to_flake_nix_check(self):
        """'use flake' with 2 tokens returns None from parser; check flake.nix directly."""
        ctx = MagicMock()
        # parse returns None (2-token 'use flake'), but flake.nix exists
        ctx.run_ssh = MagicMock()
        with (
            patch(
                "sft_marimo.marimo.find_envrc_dir_remote",
                return_value="/home/user/project",
            ),
            patch(
                "sft_marimo.marimo.parse_envrc_flake_full_remote", return_value=None
            ),
        ):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertEqual(flake_dir, "/home/user/project")
        self.assertEqual(flags, [])

    def test_returns_none_when_no_flake_nix(self):
        """'use flake' but no flake.nix in envrc dir — no env."""
        ctx = MagicMock()
        ctx.run_ssh = MagicMock(side_effect=RuntimeError("not found"))
        with (
            patch(
                "sft_marimo.marimo.find_envrc_dir_remote",
                return_value="/home/user/project",
            ),
            patch(
                "sft_marimo.marimo.parse_envrc_flake_full_remote", return_value=None
            ),
        ):
            flake_dir, flags = _detect_remote_flake(
                self._make_host(), "/home/user/project", ctx
            )
        self.assertIsNone(flake_dir)


# --- Pure helper function tests ---


class TestFindFreePort(unittest.TestCase):
    """Test _find_free_port finds a usable port."""

    def test_finds_a_port(self):
        port = _find_free_port(50000, 50100)
        self.assertGreaterEqual(port, 50000)
        self.assertLessEqual(port, 50100)

    def test_uses_first_available(self):
        """Should return the first bindable port in range."""
        import socket

        port = _find_free_port(50200, 50300)
        # Verify it's actually bindable
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            # If we get here, port was free (or bound successfully)

    def test_raises_on_full_range(self):
        """If all ports in range are bound, should raise RuntimeError."""
        import socket

        sockets = []
        try:
            # Bind all ports in a small range
            for p in range(50300, 50303):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", p))
                s.listen(1)
                sockets.append(s)
            with self.assertRaises(RuntimeError):
                _find_free_port(50300, 50302)
        finally:
            for s in sockets:
                s.close()


class TestFmtDuration(unittest.TestCase):
    """Test _fmt_duration human-readable formatting."""

    def test_seconds(self):
        now = datetime.datetime.now()
        started = now - datetime.timedelta(seconds=30)
        result = _fmt_duration(started.isoformat())
        self.assertEqual(result, "30s")

    def test_minutes_and_seconds(self):
        now = datetime.datetime.now()
        started = now - datetime.timedelta(seconds=125)  # 2m 5s
        result = _fmt_duration(started.isoformat())
        self.assertEqual(result, "2m 5s")

    def test_hours_and_minutes(self):
        now = datetime.datetime.now()
        started = now - datetime.timedelta(seconds=3725)  # 1h 2m 5s
        result = _fmt_duration(started.isoformat())
        self.assertEqual(result, "1h 2m")

    def test_zero_seconds(self):
        now = datetime.datetime.now()
        result = _fmt_duration(now.isoformat())
        self.assertEqual(result, "0s")

    def test_invalid_input(self):
        result = _fmt_duration("not-a-date")
        self.assertEqual(result, "unknown")

    def test_none_input(self):
        result = _fmt_duration(None)
        self.assertEqual(result, "unknown")


class TestCaptureMarimoToken(unittest.TestCase):
    """Test token extraction from remote marimo log."""

    def test_extracts_token_from_grep(self):
        host_info = HostInfo(
            name="test",
            hostname="1.2.3.4",
            port=22,
            user="u",
            aliases=[],
            extra_options={},
        )
        ctx = MagicMock()
        ctx.run_ssh.return_value = "abc123_def"

        token = _capture_marimo_token(host_info, 8686, ctx, timeout=1)
        self.assertEqual(token, "abc123_def")

    def test_returns_empty_when_no_token(self):
        host_info = HostInfo(
            name="test",
            hostname="1.2.3.4",
            port=22,
            user="u",
            aliases=[],
            extra_options={},
        )
        ctx = MagicMock()
        # First grep (token) returns nothing, second grep (Running) returns >0
        ctx.run_ssh.side_effect = [RuntimeError("no match"), "1\n"]

        token = _capture_marimo_token(host_info, 8686, ctx, timeout=1)
        self.assertEqual(token, "")

    def test_returns_empty_on_timeout(self):
        host_info = HostInfo(
            name="test",
            hostname="1.2.3.4",
            port=22,
            user="u",
            aliases=[],
            extra_options={},
        )
        ctx = MagicMock()
        ctx.run_ssh.side_effect = RuntimeError("no match")

        token = _capture_marimo_token(host_info, 8686, ctx, timeout=0)
        self.assertEqual(token, "")


# --- Cleanup tests (mocked, no real SSH/processes) ---


class TestCleanupSession(unittest.TestCase):
    """Test _cleanup_session with mocked subprocess/SSH."""

    def _make_session(self, **overrides):
        session = {
            "id": "test-session",
            "host": "wsl-rs",
            "remote_path": "/home/user/project",
            "mount_path": "/tmp/mnt/project",
            "marimo_port": 8686,
            "agent_port": 3023,
            "agent_pid": 12345,
            "agent_pg": 12345,
            "forward_pid": 12346,
            "remote_proc": {"pid": 99999, "started_at": "Fri Apr 24 00:00:00 2026"},
            "owned_mount": True,
            "owned_marimo": True,
        }
        session.update(overrides)
        return session

    @patch("sft_marimo.marimo.load_session")
    @patch("sft_marimo.marimo.remove_session")
    def test_cleanup_kills_processes(self, mock_remove, mock_load):
        session = self._make_session()
        mock_load.return_value = session

        with (
            patch("os.killpg") as mock_killpg,
            patch("sft_marimo.marimo.is_mount_alive", return_value=False),
            patch("sft.commands.mount.resolve_target"),
        ):
            _cleanup_session("test-session")

        # Should have called killpg for agent and forward
        self.assertEqual(mock_killpg.call_count, 2)
        mock_remove.assert_called_once_with("test-session")

    @patch("sft_marimo.marimo.load_session")
    @patch("sft_marimo.marimo.remove_session")
    def test_cleanup_handles_process_not_found(self, mock_remove, mock_load):
        session = self._make_session()
        mock_load.return_value = session

        import signal

        with (
            patch("os.killpg", side_effect=ProcessLookupError) as mock_killpg,
            patch("sft_marimo.marimo.is_mount_alive", return_value=False),
            patch("sft.commands.mount.resolve_target"),
        ):
            # Should not raise
            _cleanup_session("test-session")

        mock_remove.assert_called_once()

    @patch("sft_marimo.marimo.load_session")
    @patch("sft_marimo.marimo.remove_session")
    def test_cleanup_no_owned_mount_skips_unmount(self, mock_remove, mock_load):
        session = self._make_session(owned_mount=False, owned_marimo=False)
        mock_load.return_value = session

        with (
            patch("os.killpg"),
            patch(
                "sft_marimo.marimo.is_mount_alive", return_value=False
            ) as mock_alive,
        ):
            _cleanup_session("test-session")

        # is_mount_alive shouldn't be called since owned_mount is False
        mock_alive.assert_not_called()
        mock_remove.assert_called_once()

    @patch("sft_marimo.marimo.load_session")
    @patch("sft_marimo.marimo.remove_session")
    def test_cleanup_no_session_is_noop(self, mock_remove, mock_load):
        mock_load.return_value = None
        _cleanup_session("nonexistent")
        mock_remove.assert_not_called()

    @patch("sft_marimo.marimo.load_session")
    @patch("sft_marimo.marimo.remove_session")
    def test_cleanup_unmounts_owned_mount(self, mock_remove, mock_load):
        session = self._make_session(owned_mount=True, owned_marimo=False)
        mock_load.return_value = session

        with (
            patch("os.killpg"),
            patch("sft_marimo.marimo.is_mount_alive", return_value=True),
            patch("shutil.which", return_value="fusermount"),
            patch("subprocess.run") as mock_run,
        ):
            _cleanup_session("test-session")

        # fusermount -u should have been called
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "fusermount")
        self.assertEqual(args[1], "-u")


# --- CLI argument parsing tests ---


class TestMarimoCliParsing(unittest.TestCase):
    """Test marimo subcommand argument parsing."""

    @classmethod
    def setUpClass(cls):
        # Ensure plugins are discovered so marimo subcommand parsers are registered
        from sft.plugins import discover_plugins

        discover_plugins()

    def _parse(self, argv):
        from sft.cli import parse_args

        old = sys.argv
        try:
            sys.argv = argv
            return parse_args()
        finally:
            sys.argv = old

    def test_marimo_start_basic(self):
        args = self._parse(["sft", "marimo", "start", "wsl-rs:~/project"])
        self.assertEqual(args.target, "wsl-rs:~/project")
        self.assertEqual(args.marimo_command, "start")

    def test_marimo_start_no_open(self):
        args = self._parse(["sft", "marimo", "start", "wsl-rs:~/project", "--no-open"])
        self.assertTrue(args.no_open)

    def test_marimo_start_no_agent(self):
        args = self._parse(["sft", "marimo", "start", "wsl-rs:~/project", "--no-agent"])
        self.assertTrue(args.no_agent)

    def test_marimo_start_port(self):
        args = self._parse(
            ["sft", "marimo", "start", "wsl-rs:~/project", "--port", "9000"]
        )
        self.assertEqual(args.port, 9000)

    def test_marimo_start_no_auto_env(self):
        args = self._parse(
            ["sft", "marimo", "start", "wsl-rs:~/project", "--no-auto-env"]
        )
        self.assertTrue(args.no_auto_env)

    def test_marimo_start_background(self):
        args = self._parse(["sft", "marimo", "start", "wsl-rs:~/project", "-b"])
        self.assertTrue(args.background)

    def test_marimo_start_background_long(self):
        args = self._parse(
            ["sft", "marimo", "start", "wsl-rs:~/project", "--background"]
        )
        self.assertTrue(args.background)

    def test_marimo_status(self):
        args = self._parse(["sft", "marimo", "status"])
        self.assertEqual(args.marimo_command, "status")

    def test_marimo_status_with_session(self):
        args = self._parse(["sft", "marimo", "status", "my-session"])
        self.assertEqual(args.session_id, "my-session")

    def test_marimo_stop(self):
        args = self._parse(["sft", "marimo", "stop"])
        self.assertEqual(args.marimo_command, "stop")

    def test_marimo_stop_with_session(self):
        args = self._parse(["sft", "marimo", "stop", "my-session"])
        self.assertEqual(args.session_id, "my-session")

    def test_marimo_list(self):
        args = self._parse(["sft", "marimo", "list"])
        self.assertEqual(args.marimo_command, "list")

    def test_marimo_no_subcommand(self):
        """marimo without subcommand should still parse (shows help)."""
        args = self._parse(["sft", "marimo"])
        self.assertIsNone(getattr(args, "marimo_command", None))


# --- Path mapping in agent config tests ---


class TestStartLocalAgentPathMapping(unittest.TestCase):
    """Test that _start_local_agent generates precise path mapping config."""

    @patch("subprocess.Popen")
    def test_config_contains_exact_prefix_mapping(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        _start_local_agent(
            mount_path="/home/user/mnt/wsl-rs/home/user/project",
            agent_port=3023,
            host_info=HostInfo("wsl-rs", "1.2.3.4", 22, "u", [], {}),
            remote_root="/home/user/project",
            ctx=MagicMock(),
        )
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs["env"]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        instructions = config["instructions"]
        all_text = " ".join(instructions)
        # Must contain the exact remote root and local mount
        self.assertIn("/home/user/project", all_text)
        self.assertIn("/home/user/mnt/wsl-rs/home/user/project", all_text)
        # Must contain mandatory mapping instruction
        self.assertIn("MANDATORY", all_text)

    @patch("subprocess.Popen")
    def test_config_uses_absolute_mount_path(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        _start_local_agent(
            mount_path="~/mnt/wsl-rs/home/user/project",
            agent_port=3023,
            host_info=HostInfo("wsl-rs", "1.2.3.4", 22, "u", [], {}),
            remote_root="/home/user/project",
            ctx=MagicMock(),
        )
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs["env"]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        instructions = config["instructions"]
        all_text = " ".join(instructions)
        # Should use absolute path, not tilde
        self.assertNotIn("~", all_text)
        self.assertIn(os.path.abspath(os.path.expanduser("~/mnt/wsl-rs/home/user/project")), all_text)

    @patch("subprocess.Popen")
    def test_cwd_set_to_mount_path(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        _start_local_agent(
            mount_path="/mnt/project",
            agent_port=3023,
            host_info=HostInfo("h", "1.2.3.4", 22, "u", [], {}),
            remote_root="/home/user/project",
            ctx=MagicMock(),
        )
        call_kwargs = mock_popen.call_args[1]
        self.assertEqual(call_kwargs["cwd"], "/mnt/project")


if __name__ == "__main__":
    unittest.main()
