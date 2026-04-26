"""Marimo remote notebook + local OpenCode ACP agent orchestration."""

from __future__ import annotations

import atexit
import datetime
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from sft.commands.mount import _resolve_remote_path, ensure_mount, resolve_target
from sft.config import HostInfo, ParsedTarget
from sft.context import ExecutionContext
from sft.env import (
    find_envrc_dir_remote,
    parse_envrc_flake_full_remote,
)
from sft.state import (
    list_sessions,
    load_session,
    remove_session,
    save_session,
    is_mount_alive,
)
from sft.ui import Spinner, Theme

_shutdown_event = threading.Event()
_current_session_id: Optional[str] = None


# --- Helpers ---


def _find_free_port(start: int, end: int) -> int:
    """Find a free local port in [start, end]."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {start}-{end}")


def _find_marimo_bin(
    host_info: HostInfo, remote_path: str, ctx: ExecutionContext
) -> str:
    """Locate marimo binary on remote host. Returns the path or raises."""
    # Priority 1: .venv (uv project)
    venv = f"{remote_path}/.venv/bin/marimo"
    try:
        ctx.run_ssh(host_info, f"test -x {shlex.quote(venv)}", silent=True)
        return venv
    except RuntimeError:
        pass

    # Priority 2: system PATH
    try:
        out = ctx.run_ssh(host_info, "which marimo", capture=True, silent=True)
        if out:
            return out.strip()
    except RuntimeError:
        pass

    raise RuntimeError(
        f"marimo not found on {host_info.name} at {remote_path}. "
        f"Install with: uv add 'marimo[recommended]'"
    )


def _detect_remote_flake(
    host_info: HostInfo, remote_path: str, ctx: ExecutionContext
) -> Tuple[Optional[str], List[str]]:
    """Detect nix develop environment on remote host.

    Walks up from remote_path looking for .envrc with 'use flake' directive.
    Returns (flake_dir, flake_flags) or (None, []).
    """
    target = ParsedTarget(
        is_remote=True,
        path=remote_path,
        host=host_info,
        user_override=None,
    )
    remote_envrc = find_envrc_dir_remote(target, ctx, allow_dry_run_execute=True)
    if not remote_envrc:
        return None, []

    result = parse_envrc_flake_full_remote(target, remote_envrc, ctx)
    if result:
        flake_path, flake_flags = result
        if flake_path:
            return flake_path, flake_flags
        # 'use flake' without path — the flake.nix is in the envrc directory
        return remote_envrc, flake_flags

    # .envrc exists but parse_envrc_flake_full_remote returned None.
    # This can happen with bare 'use flake' (2 tokens, < 3 required).
    # Check if flake.nix exists in the envrc directory directly.
    try:
        ctx.run_ssh(
            host_info,
            f"test -f {shlex.quote(remote_envrc + '/flake.nix')}",
            silent=True,
        )
        return remote_envrc, []
    except RuntimeError:
        return None, []


def _resolve_project_root(
    host_info: HostInfo, remote_path: str, ctx: ExecutionContext
) -> Tuple[str, Optional[str]]:
    """Resolve project root and optional notebook file from remote path.

    If remote_path is a .py file, walks up to find the project root
    (nearest directory with .git, flake.nix, .envrc, or pyproject.toml).
    Returns (project_root, notebook_file_or_None).
    """
    # Check if remote_path is a file
    is_file = False
    try:
        ctx.run_ssh(
            host_info,
            f"test -f {shlex.quote(remote_path)}",
            silent=True,
        )
        is_file = True
    except RuntimeError:
        pass

    if not is_file:
        return remote_path, None

    notebook_file = remote_path
    # Walk up to find project root
    script = (
        f'python3 -c "'
        f"import os,sys\n"
        f"path=os.path.dirname(os.path.abspath(os.path.expanduser(sys.argv[1])))\n"
        f"markers=['.git','flake.nix','.envrc','pyproject.toml','.venv']\n"
        f"while True:\n"
        f"  for m in markers:\n"
        f"    if os.path.exists(os.path.join(path,m)):\n"
        f"      print(path)\n"
        f"      sys.exit(0)\n"
        f"  parent=os.path.dirname(path)\n"
        f"  if parent==path: break\n"
        f"  path=parent\n"
        f"print(path)\n"
        f'" {shlex.quote(notebook_file)}'
    )
    try:
        project_root = ctx.run_ssh(host_info, script, capture=True, silent=True)
        if project_root:
            return project_root.strip(), notebook_file
    except RuntimeError:
        pass
    return remote_path, None


def _launch_remote_marimo(
    host_info: HostInfo,
    project_root: str,
    marimo_bin: str,
    port: int,
    ctx: ExecutionContext,
    *,
    remote_flake_dir: Optional[str] = None,
    flake_flags: Optional[List[str]] = None,
    notebook_file: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Launch marimo on remote host. Returns {pid, started_at, log_path}.

    If remote_flake_dir is set, launches inside ``nix develop`` so the
    devshell environment (packages, PYTHONPATH, etc.) is available.
    Otherwise falls back to running the marimo binary directly.

    If notebook_file is set, opens that specific notebook.
    Otherwise opens the project root in marimo's file browser.
    """
    log_path = f"/tmp/sft-marimo-{port}.log"
    marimo_target = notebook_file or project_root
    marimo_cmd = (
        f"{shlex.quote(marimo_bin)} edit "
        f"--headless --watch --port {port} {shlex.quote(marimo_target)}"
    )

    if remote_flake_dir:
        # Wrap marimo launch inside nix develop, similar to how
        # sft run wraps user commands via build_remote_execution_command().
        # We write a launcher script so nohup + nix develop coexist.
        flags_str = " ".join(shlex.quote(f) for f in (flake_flags or []))
        if flags_str:
            flags_str = " " + flags_str
        script_path = f"/tmp/sft-marimo-launcher-{port}.sh"
        # Use $HOME-relative quoting for paths that may contain ~
        flake_quoted = shlex.quote(remote_flake_dir)
        launch_cmd = (
            f"cat > {script_path} << 'MARIMO_LAUNCHER_EOF'\n"
            f"#!/usr/bin/env bash\n"
            f"cd {shlex.quote(project_root)}\n"
            f"exec {marimo_cmd}\n"
            f"MARIMO_LAUNCHER_EOF\n"
            f"chmod +x {script_path} && "
            f"nohup nix develop{flags_str} {flake_quoted} "
            f"--command bash {script_path} "
            f"> {log_path} 2>&1 & "
            f"echo PID=$!; "
            f"ps -o lstart= -p $! 2>/dev/null || true"
        )
    else:
        # No nix develop — run marimo binary directly
        launch_cmd = (
            f"cd {shlex.quote(project_root)} && "
            f"nohup {marimo_cmd} "
            f"> {log_path} 2>&1 & "
            f"echo PID=$!; "
            f"ps -o lstart= -p $! 2>/dev/null || true"
        )

    output = ctx.run_ssh(
        host_info,
        launch_cmd,
        capture=True,
        description=f"Starting marimo on {host_info.name}:{port}",
    )
    if not output:
        raise RuntimeError("Failed to start remote marimo — no output")

    pid = None
    started_at = None
    for line in output.strip().splitlines():
        if line.startswith("PID="):
            pid = line.split("=", 1)[1].strip()
        elif pid and not line.startswith("PID="):
            started_at = line.strip()

    if not pid:
        raise RuntimeError(f"Failed to parse marimo PID from: {output}")

    return {"pid": pid, "started_at": started_at, "log_path": log_path}


def _capture_marimo_token(
    host_info: HostInfo, port: int, ctx: ExecutionContext, timeout: int = 30
) -> str:
    """Extract marimo auth token from remote log. Returns empty string if no token."""
    log_path = f"/tmp/sft-marimo-{port}.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = ctx.run_ssh(
                host_info,
                f"grep -oP 'access_token=\\K[a-zA-Z0-9_-]+' {log_path} 2>/dev/null | head -1",
                capture=True,
                silent=True,
            )
            if out and out.strip():
                return out.strip()
        except RuntimeError:
            pass
        # Also check if marimo started without token
        try:
            out = ctx.run_ssh(
                host_info,
                f"grep -c 'Running' {log_path} 2>/dev/null || echo 0",
                capture=True,
                silent=True,
            )
            if out and int(out.strip()) > 0:
                return ""
        except (RuntimeError, ValueError):
            pass
        time.sleep(1)
    return ""


def _wait_for_marimo(port: int, timeout: int = 30) -> None:
    """Wait for marimo HTTP server to respond on localhost."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}", timeout=2)
            return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"marimo not responding on port {port} after {timeout}s")


def _start_port_forward(
    host_info: HostInfo, ports: List[int], ctx: ExecutionContext
) -> int:
    """Start SSH port forwarding. Returns the local PID."""
    control_path = os.path.expanduser(
        ctx.config.ssh_control_path.replace("%h", host_info.hostname)
        .replace("%p", str(host_info.port))
        .replace("%r", host_info.user)
    )
    cmd = ["ssh", "-n", "-N", "-o", f"ControlPath={control_path}"]
    for p in ports:
        cmd += ["-L", f"127.0.0.1:{p}:127.0.0.1:{p}"]
    cmd += ["-p", str(host_info.port), host_info.ssh_target()]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _start_local_agent(
    mount_path: str,
    agent_port: int,
    host_info: HostInfo,
    remote_root: str,
    ctx: ExecutionContext,
    *,
    marimo_port: int = 0,
    marimo_token: str = "",
) -> Tuple[int, int]:
    """Start local OpenCode ACP agent under mount path.

    Spawns::

        stdio-to-ws → acp_path_filter.py → opencode acp

    The path filter intercepts JSON-RPC messages between marimo and
    opencode, rewriting filesystem paths so that opencode sees local
    SSHFS paths while marimo continues using remote absolute paths.

    If ``marimo_port`` is set, the agent also gets MCP tools for
    executing notebook cells via a local ``marimo-mcp`` subprocess.

    Args:
        mount_path: Local SSHFS mount directory (e.g. ~/mnt/wsl-rs/home/user/project).
        agent_port: Local port for the ACP WebSocket server.
        host_info: Remote host information.
        remote_root: Remote project root directory (must match what was mounted).
        ctx: Execution context.
        marimo_port: Marimo HTTP port on localhost (via port forward).
        marimo_token: Marimo auth token (may be empty if --no-token).

    Returns (pid, process_group).
    """
    abs_mount = os.path.abspath(os.path.expanduser(mount_path))

    # Locate the path filter script (shipped alongside this module)
    filter_script = os.path.join(os.path.dirname(__file__), "acp_path_filter.py")

    # Locate the marimo-mcp binary (installed alongside sft-marimo)
    marimo_mcp_bin = shutil.which("marimo-mcp") or ""

    # Build MCP config if marimo port is available
    mcp_config = {}
    mcp_permissions = {}
    if marimo_port and marimo_mcp_bin:
        marimo_url = f"http://localhost:{marimo_port}"
        mcp_config["marimo"] = {
            "type": "local",
            "command": [marimo_mcp_bin],
            "environment": {
                "MARIMO_URL": marimo_url,
                "MARIMO_TOKEN": marimo_token,
            },
        }
        # Pre-approve all marimo MCP tools
        for tool_name in [
            "list_cells",
            "run_cell",
            "execute_code",
            "get_cell_info",
            "get_cell_output",
        ]:
            mcp_permissions[f"marimo_{tool_name}"] = "allow"

    extra_config = json.dumps(
        {
            **({"mcp": mcp_config} if mcp_config else {}),
            **({"permission": mcp_permissions} if mcp_permissions else {}),
            "instructions": [
                f"MARIMO_REMOTE_HOST={host_info.name}",
                f"MARIMO_REMOTE_ROOT={remote_root}",
                f"MARIMO_LOCAL_MOUNT={abs_mount}",
                "PATH MAPPING RULE (MANDATORY):",
                "  All file tool operations must use LOCAL paths.",
                "  Translate remote paths to local by prefix replacement:",
                f"    Remote prefix: {remote_root}",
                f"    Local  prefix: {abs_mount}",
                f"  Example: {remote_root}/foo.py -> {abs_mount}/foo.py",
                f"  Any absolute path starting with '{remote_root}' must be rewritten"
                f" to start with '{abs_mount}'.",
                f"  Relative paths are relative to {abs_mount} and work as-is.",
                "MARIMO FILE WATCH: marimo is running with --watch enabled.",
                "  Edits made via the `edit` tool will automatically propagate"
                " to the marimo web UI.",
                "  No manual refresh is needed.",
            ]
            + (
                [
                    "",
                    "MARIMO CELL EXECUTION:",
                    "  You have MCP tools to execute and inspect notebook cells.",
                    "  - list_cells: See all cells, their IDs, code, and status.",
                    "  - run_cell(cell_id, code?): Execute a cell and get output."
                    " Updates the notebook UI.",
                    "  - execute_code(code): Run arbitrary Python in the kernel."
                    " Quick computations, does NOT update notebook UI.",
                    "  - get_cell_info(cell_id): View a cell's full code.",
                    "  - get_cell_output(cell_id): View cell output.",
                    "",
                    "WORKFLOW for modifying and running a cell:",
                    "  1. Use edit tool to modify cell code in the .py file.",
                    "  2. --watch auto-syncs changes to marimo UI.",
                    "  3. Use run_cell(cell_id) to execute and see results.",
                    "  4. Or use execute_code(code) for quick one-off computations.",
                ]
                if mcp_config
                else []
            ),
        }
    )
    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = extra_config

    # Build the command: stdio-to-ws → path-filter → opencode acp
    inner_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(filter_script)}"
        f" --remote-root {shlex.quote(remote_root)}"
        f" --local-mount {shlex.quote(abs_mount)}"
        f" -- opencode acp"
    )
    proc = subprocess.Popen(
        ["npx", "stdio-to-ws", inner_cmd, "--port", str(agent_port)],
        cwd=mount_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid, proc.pid


def _open_browser(url: str) -> None:
    """Open URL in default browser via xdg-open."""
    subprocess.Popen(
        ["xdg-open", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _fmt_duration(iso_start: str) -> str:
    """Human-readable duration from ISO timestamp."""
    try:
        started = datetime.datetime.fromisoformat(iso_start)
        delta = datetime.datetime.now() - started
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m {secs % 60}s"
        hrs = mins // 60
        return f"{hrs}h {mins % 60}m"
    except (ValueError, TypeError):
        return "unknown"


def _preflight_checks(ctx: ExecutionContext) -> None:
    """Verify local dependencies are available."""
    if not shutil.which("opencode"):
        raise RuntimeError("opencode not found. Install the opencode module first.")
    # stdio-to-ws will be resolved by npx; check npx exists
    if not shutil.which("npx"):
        raise RuntimeError("npx not found. Install nodejs (npm) first.")


# --- Cleanup ---


def _cleanup_session(session_id: str, ctx: Optional[ExecutionContext] = None) -> None:
    """Safe, idempotent cleanup of a marimo session and all its resources."""
    session = load_session(session_id)
    if not session:
        return

    errors: List[str] = []

    # 1. Kill local agent (process group)
    agent_pg = session.get("agent_pg")
    if agent_pg:
        try:
            os.killpg(agent_pg, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            errors.append(f"kill agent: {e}")

    # 2. Kill port forward
    forward_pid = session.get("forward_pid")
    if forward_pid:
        try:
            os.killpg(forward_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            errors.append(f"kill forward: {e}")

    # 3. Kill remote marimo (only if we started it)
    if session.get("owned_marimo"):
        remote_proc = session.get("remote_proc", {})
        remote_pid = remote_proc.get("pid")
        if remote_pid:
            try:
                _ctx = ctx or ExecutionContext(dry_run=False, verbose=False)
                from sft.commands.mount import resolve_target as _resolve

                host_info, _ = _resolve(
                    session["host"] + ":", _ctx, allow_empty_path=True
                )
                _ctx.run_ssh(host_info, f"kill {remote_pid} 2>/dev/null", silent=True)
            except Exception as e:
                # kill returns non-zero if process already dead — benign
                msg = str(e)
                if "Command failed" not in msg or "kill" not in msg:
                    errors.append(f"kill remote marimo: {e}")

    # 4. Unmount (only if we mounted it)
    if session.get("owned_mount"):
        mount_path = session.get("mount_path")
        if mount_path and is_mount_alive(mount_path):
            try:
                umount = (
                    ["fusermount", "-u", mount_path]
                    if shutil.which("fusermount")
                    else ["umount", mount_path]
                )
                subprocess.run(umount, capture_output=True, timeout=10)
            except Exception as e:
                errors.append(f"unmount: {e}")

    # 5. Remove session file
    remove_session(session_id)

    if errors:
        Theme.warning(f"Cleanup issues: {'; '.join(errors)}")


def _request_shutdown(signum: int, frame: object) -> None:
    """Signal handler — just set flag, no complex work."""
    _shutdown_event.set()


# --- Commands ---


def cmd_marimo_start(args, ctx: ExecutionContext) -> None:
    """Orchestrate: mount → launch marimo → forward ports → start agent → open browser."""
    global _current_session_id

    # 0. Preflight
    try:
        _preflight_checks(ctx)
    except RuntimeError as e:
        Theme.error(str(e))
        sys.exit(1)

    # 1. Parse target
    target = getattr(args, "target", "")
    if not target:
        Theme.error("Usage: sft marimo start <host>[:<path>]")
        sys.exit(1)

    try:
        host_info, remote_path = resolve_target(target, ctx, allow_empty_path=False)
    except SystemExit:
        raise
    except Exception as e:
        Theme.error(f"Failed to resolve target: {e}")
        sys.exit(1)

    if not remote_path:
        Theme.error("A remote path is required (host:/path)")
        sys.exit(1)

    # 1b. Expand ~ in remote path so all subsequent operations use the
    #     absolute path (ensure_mount does this internally, but we need
    #     the expanded value for _find_marimo_bin and _launch_remote_marimo).
    remote_path = _resolve_remote_path(remote_path, host_info, ctx)

    # 1c. If remote_path points to a .py file, detect project root.
    #     The project root is used for mount, env detection, and cd;
    #     the notebook file is passed to marimo edit.
    notebook_file = None
    project_root = remote_path
    if remote_path.endswith(".py"):
        project_root, notebook_file = _resolve_project_root(host_info, remote_path, ctx)
        if notebook_file:
            ctx.log(f"Notebook: {notebook_file}")
            ctx.log(f"Project root: {project_root}")
        else:
            project_root = remote_path

    # 2. SSHFS mount (mount the project root, not the notebook file)
    try:
        with Spinner(f"Mounting {host_info.name}:{project_root}"):
            mount_path = ensure_mount(host_info, project_root, ctx, mkdir=True)
        owned_mount = True
    except RuntimeError:
        # Might already be mounted from a previous sft mount
        from sft.state import derive_mountpoint

        mount_path = os.path.expanduser(derive_mountpoint(host_info.name, project_root))
        if is_mount_alive(mount_path):
            owned_mount = False
            Theme.info("Mount", f"Reusing existing mount at {mount_path}")
        else:
            Theme.error(f"Mount failed and no existing mount at {mount_path}")
            sys.exit(1)

    # 3. Detect nix develop environment on remote host
    remote_flake_dir = None
    flake_flags: List[str] = []
    auto_env = not getattr(args, "no_auto_env", False)
    if auto_env:
        with Spinner(f"Detecting nix environment on {host_info.name}"):
            remote_flake_dir, flake_flags = _detect_remote_flake(
                host_info, project_root, ctx
            )
        if remote_flake_dir:
            ctx.log(f"Found nix develop at {remote_flake_dir}")
        else:
            ctx.log("No nix develop environment detected, using bare binary")

    # 4. Find marimo binary
    try:
        marimo_bin = _find_marimo_bin(host_info, project_root, ctx)
    except RuntimeError as e:
        Theme.error(str(e))
        sys.exit(1)

    # 5. Allocate ports
    marimo_port = getattr(args, "port", None) or _find_free_port(8686, 8696)
    agent_port = getattr(args, "agent_port", None) or _find_free_port(3023, 3033)

    # 6. Launch remote marimo (inside nix develop if detected)
    no_agent = getattr(args, "no_agent", False)
    try:
        remote_proc = _launch_remote_marimo(
            host_info,
            project_root,
            marimo_bin,
            marimo_port,
            ctx,
            remote_flake_dir=remote_flake_dir,
            flake_flags=flake_flags,
            notebook_file=notebook_file,
        )
    except RuntimeError as e:
        Theme.error(f"Failed to start marimo: {e}")
        sys.exit(1)

    # 7. Capture auth token
    with Spinner("Waiting for marimo to start"):
        token = _capture_marimo_token(host_info, marimo_port, ctx, timeout=30)

    # 8. Port forward
    try:
        forward_pid = _start_port_forward(host_info, [marimo_port], ctx)
    except Exception as e:
        Theme.error(f"Port forward failed: {e}")
        sys.exit(1)

    # 9. Wait for marimo via health check
    try:
        _wait_for_marimo(marimo_port, timeout=15)
    except RuntimeError:
        Theme.error(f"marimo not responding on localhost:{marimo_port}")
        # Attempt cleanup
        os.killpg(forward_pid, signal.SIGTERM)
        sys.exit(1)

    # 10. Start local agent
    agent_pid = None
    agent_pg = None
    if not no_agent:
        try:
            agent_pid, agent_pg = _start_local_agent(
                mount_path, agent_port, host_info, project_root, ctx,
                marimo_port=marimo_port,
                marimo_token=token,
            )
            # Brief wait for agent to bind
            time.sleep(2)
        except Exception as e:
            Theme.warning(f"Agent start failed: {e}")
            agent_pid = None
            agent_pg = None

    # 11. Build URL
    token_suffix = f"?access_token={token}" if token else ""
    marimo_url = f"http://localhost:{marimo_port}{token_suffix}"
    # When a specific notebook is opened, append the file path to the URL
    # so the browser navigates directly to it
    if notebook_file:
        # marimo URL scheme: http://host:port?access_token=xxx#/<relative_path>
        rel = os.path.relpath(notebook_file, project_root)
        marimo_url = f"http://localhost:{marimo_port}{token_suffix}#/{rel}"

    # 12. Register session
    session = {
        "host": host_info.name,
        "remote_path": project_root,
        "notebook_file": notebook_file,
        "mount_path": os.path.abspath(os.path.expanduser(mount_path)),
        "marimo_port": marimo_port,
        "agent_port": agent_port,
        "marimo_url": marimo_url,
        "marimo_token": token,
        "remote_proc": remote_proc,
        "forward_pid": forward_pid,
        "agent_pid": agent_pid,
        "agent_pg": agent_pg,
        "owned_mount": owned_mount,
        "owned_marimo": True,
        "remote_flake_dir": remote_flake_dir,
        "started_at": datetime.datetime.now().isoformat(),
        "status": "running",
        "path_mapping": {
            "remote_root": project_root,
            "local_mount": os.path.abspath(os.path.expanduser(mount_path)),
        },
    }
    session_id = save_session(session)
    _current_session_id = session_id

    # 13. Print summary
    print()
    Theme.success("Marimo session started")
    print(f"  {'Session:':<12} {session_id}")
    print(f"  {'Marimo:':<12} {marimo_url}")
    if notebook_file:
        print(f"  {'Notebook:':<12} {os.path.basename(notebook_file)}")
    if remote_flake_dir:
        print(f"  {'Env:':<12} nix develop {remote_flake_dir}")
    if agent_pid:
        print(f"  {'Agent:':<12} ws://localhost:{agent_port} (local OpenCode)")
    print(f"  {'Mount:':<12} {mount_path}")
    print(f"  {'Stop:':<12} Ctrl+C  or  sft marimo stop {session_id}")
    print()

    # 14. Open browser
    if not getattr(args, "no_open", False):
        _open_browser(marimo_url)

    # 15. Register cleanup and wait
    background = getattr(args, "background", False)
    if background:
        # Don't block — remote marimo, port forward, and agent are all
        # separate processes that survive after we exit.  atexit won't
        # fire on normal exit, so the user runs `sft marimo stop` later.
        print(f"  {'Tip:':<12} sft marimo stop {session_id}")
        return

    atexit.register(_cleanup_session, session_id, ctx)
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    try:
        _shutdown_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        Theme.info("Stopping", "Cleaning up session...")
        _cleanup_session(session_id, ctx)


def cmd_marimo_status(args, ctx: ExecutionContext) -> None:
    """Show marimo session status."""
    sessions = list_sessions()
    if not sessions:
        Theme.info("No marimo sessions", "")
        return

    session_id = getattr(args, "session_id", None)
    if session_id:
        sessions = [s for s in sessions if s["id"] == session_id]
        if not sessions:
            Theme.error(f"Session not found: {session_id}")
            sys.exit(1)

    for s in sessions:
        status = s.get("status", "unknown")
        status_str = (
            f"{Theme.GREEN}running{Theme.CLR}"
            if status == "running"
            else f"{Theme.YELLOW}{status}{Theme.CLR}"
        )
        duration = _fmt_duration(s.get("started_at", ""))
        url = s.get("marimo_url", f"http://localhost:{s.get('marimo_port', '?')}")

        print(f"  {Theme.BOLD}{s['id']}{Theme.CLR}  {status_str}  {duration}")
        print(f"    URL:      {url}")
        print(f"    Host:     {s.get('host', '?')}")
        print(f"    Path:     {s.get('remote_path', '?')}")
        print(f"    Mount:    {s.get('mount_path', '?')}")
        if s.get("agent_pid"):
            print(f"    Agent:    ws://localhost:{s.get('agent_port', '?')}")
        print()


def cmd_marimo_stop(args, ctx: ExecutionContext) -> None:
    """Stop a marimo session and clean up."""
    session_id = getattr(args, "session_id", None)

    if session_id:
        session = load_session(session_id)
        if not session:
            Theme.error(f"Session not found: {session_id}")
            sys.exit(1)
        Theme.info("Stopping", session_id)
        _cleanup_session(session_id, ctx)
        Theme.success(f"Session {session_id} stopped")
    else:
        sessions = [s for s in list_sessions() if s.get("status") == "running"]
        if not sessions:
            Theme.info("No running sessions", "")
            return
        for s in sessions:
            Theme.info("Stopping", s["id"])
            _cleanup_session(s["id"], ctx)
        Theme.success(f"Stopped {len(sessions)} session(s)")


def cmd_marimo_list(args, ctx: ExecutionContext) -> None:
    """List all marimo sessions."""
    sessions = list_sessions()
    if not sessions:
        Theme.info("No marimo sessions", "")
        return

    header = (
        f"  {Theme.BOLD}"
        f"{'SESSION':<30} {'HOST':<12} {'PORT':<7} {'STATUS':<10} {'DURATION'}"
        f"{Theme.CLR}"
    )
    print(header)

    for s in sessions:
        status = s.get("status", "unknown")
        status_str = (
            f"{Theme.GREEN}running{Theme.CLR}"
            if status == "running"
            else f"{Theme.YELLOW}{status}{Theme.CLR}"
        )
        duration = _fmt_duration(s.get("started_at", ""))
        port = s.get("marimo_port", "?")
        print(
            f"  {s['id']:<30} {s.get('host', '?'):<12} {port:<7} {status_str:<18} {duration}"
        )
