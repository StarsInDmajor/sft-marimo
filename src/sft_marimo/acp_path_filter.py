"""Bidirectional JSON-RPC path filter for SSHFS-mounted marimo sessions.

Interposes between stdio-to-ws and opencode acp, rewriting filesystem
paths so that opencode sees local SSHFS paths while marimo continues
to use remote absolute paths.

Usage (as a stdio proxy):
    python3 acp_path_filter.py \
        --remote-root /home/user/project \
        --local-mount /home/user/mnt/host/user/project \
        -- opencode acp

Protocol:
    stdin  ← stdio-to-ws (marimo → agent, inbound: remote paths)
    stdout → stdio-to-ws (agent → marimo, outbound: local paths need reversal)
    child.stdin  → opencode acp (rewritten to local paths)
    child.stdout ← opencode acp (rewritten back to remote paths)

Message framing: newline-delimited JSON (JSON-RPC over line framing).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading


def _replace_in_string(value: str, old: str, new: str) -> str:
    """Replace all occurrences of ``old`` path prefix with ``new`` in ``value``.

    Unlike a simple prefix check, this also handles paths embedded in
    longer strings (e.g. marimo system prompts that say "read the
    notebook at /home/user/project/file.py").

    Replacement only happens at path boundaries: the character before
    ``old`` must be a non-alphanumeric char (or start-of-string), and
    the character after ``old`` must be ``/``, end-of-string, or
    non-alphanumeric.
    """
    if old not in value:
        return value

    result = []
    i = 0
    old_len = len(old)
    while i <= len(value) - old_len:
        # Check for match at position i
        if value[i : i + old_len] == old:
            # Verify left boundary: start of string or non-alphanumeric
            left_ok = i == 0 or not value[i - 1].isalnum()
            # Verify right boundary: end of string, '/', or non-alphanumeric
            if i + old_len < len(value):
                right_ok = value[i + old_len] in ("/",) or not value[i + old_len].isalnum()
            else:
                right_ok = True
            if left_ok and right_ok:
                result.append(new)
                i += old_len
                continue
        result.append(value[i])
        i += 1
    # Append remaining characters
    result.append(value[i:])
    return "".join(result)


def _rewrite_strings(obj, old: str, new: str):
    """Recursively rewrite all string values in a JSON-compatible object.

    Performs exact prefix replacement on every string in dicts, lists,
    and nested structures.
    """
    if isinstance(obj, str):
        return _replace_in_string(obj, old, new)
    if isinstance(obj, dict):
        return {k: _rewrite_strings(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_strings(v, old, new) for v in obj]
    return obj


def rewrite_line(line: str, old: str, new: str) -> str:
    """Parse a JSON-RPC line, rewrite paths, return serialized line.

    If the line is not valid JSON, pass through unchanged (defensive).
    """
    stripped = line.strip()
    if not stripped:
        return line
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return line
    rewritten = _rewrite_strings(obj, old, new)
    return json.dumps(rewritten, ensure_ascii=False) + "\n"


def _pipe_inbound(remote_root: str, local_mount: str, child_stdin):
    """Thread: stdin (from marimo) → rewrite remote→local → child stdin."""
    try:
        for line in sys.stdin:
            try:
                rewritten = rewrite_line(line, remote_root, local_mount)
                child_stdin.write(rewritten)
                child_stdin.flush()
            except (BrokenPipeError, OSError):
                break
    finally:
        try:
            child_stdin.close()
        except OSError:
            pass


def _pipe_outbound(remote_root: str, local_mount: str, child_stdout):
    """Thread: child stdout → rewrite local→remote → stdout (to marimo)."""
    try:
        for line in child_stdout:
            try:
                rewritten = rewrite_line(line, local_mount, remote_root)
                sys.stdout.write(rewritten)
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                break
    finally:
        try:
            sys.stdout.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bidirectional path-rewriting proxy for opencode ACP",
    )
    parser.add_argument(
        "--remote-root",
        required=True,
        help="Remote project root path (e.g. /home/user/project)",
    )
    parser.add_argument(
        "--local-mount",
        required=True,
        help="Local SSHFS mount path (e.g. /home/user/mnt/host/user/project)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run (preceded by --)",
    )
    args = parser.parse_args()

    # Strip leading "--" separator
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("acp_path_filter: no command specified after --", file=sys.stderr)
        sys.exit(1)

    remote_root = args.remote_root.rstrip("/")
    local_mount = args.local_mount.rstrip("/")

    # Start child process (opencode acp)
    child = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )

    # Wrap binary pipes in text mode for line-by-line iteration
    child_stdin = io.TextIOWrapper(child.stdin, write_through=True)
    child_stdout = io.TextIOWrapper(child.stdout)

    # Bidirectional piping threads
    t_in = threading.Thread(
        target=_pipe_inbound,
        args=(remote_root, local_mount, child_stdin),
        daemon=True,
    )
    t_out = threading.Thread(
        target=_pipe_outbound,
        args=(remote_root, local_mount, child_stdout),
        daemon=True,
    )
    t_in.start()
    t_out.start()

    # Wait for child to exit
    child.wait()
    # Give threads a moment to flush
    t_in.join(timeout=2)
    t_out.join(timeout=2)
    sys.exit(child.returncode)


if __name__ == "__main__":
    main()
