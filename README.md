# sft-marimo

**English** | [中文](README.zh-CN.md)

Marimo notebook plugin for [sft](https://github.com/StarsInDmajor/sft). Provides remote marimo notebook server launch, local ACP agent (OpenCode) orchestration, and SSH port forwarding.

## What It Does

1. **Launches marimo on a remote host** via SSH, detecting `.venv/bin/marimo` or system `marimo`
2. **Detects Nix environments** — wraps launch in `nix develop` when `.envrc` + `flake.nix` are found
3. **SSH port forwarding** — forwards the marimo port to `localhost` for browser access
4. **Local ACP agent** — starts `npx stdio-to-ws opencode acp` under an SSHFS mount for notebook AI assistance
5. **Session management** — start/stop/status/list with state persistence in `~/.local/state/sft/marimo-sessions/`

## Installation

```bash
pip install git+https://github.com/StarsInDmajor/sft-marimo.git
```

For NixOS, it's built as part of the `packages.sft` derivation alongside the core and other plugins.

## Usage

```bash
# Start remote marimo + local agent
sft marimo start my-server:~/project

# Start with specific notebook
sft marimo start my-server:~/project/notebook.py

# Without agent (no OpenCode ACP)
sft marimo start my-server:~/project --no-agent

# Don't open browser
sft marimo start my-server:~/project --no-open

# Skip nix develop detection
sft marimo start my-server:~/project --no-auto-env

# Run in background
sft marimo start my-server:~/project -b

# Session management
sft marimo status
sft marimo list
sft marimo stop [session_id]
```

## Architecture

```
Browser ←→ localhost:8686 ←(SSH -L)→ remote marimo (headless)
OpenCode ACP ←→ localhost:3023 ←(stdio-to-ws)→ local agent under SSHFS mount
```

### Session Lifecycle

1. **Preflight** — check `opencode`, `npx` available locally
2. **Mount** — SSHFS mount remote project (reuses existing if alive)
3. **Detect env** — check remote `.envrc`/`flake.nix` for nix develop
4. **Find marimo** — `.venv/bin/marimo` or system `marimo`
5. **Launch** — `nix develop --command` (or bare binary) with `nohup`
6. **Token** — capture auth token from remote log
7. **Forward** — SSH `-L` port forward for marimo port
8. **Agent** — `npx stdio-to-ws opencode acp` under mount path
9. **Cleanup** — atexit + SIGINT/SIGTERM signal flag + explicit `stop`

### Notebook File Auto-Detection

If the target path ends in `.py`, `_resolve_project_root()` walks up looking for project markers (`.git`, `flake.nix`, `.envrc`, `pyproject.toml`, `.venv`). The project root is used for SSHFS mount and env detection, while the `.py` file is passed to `marimo edit` and appended as a URL fragment (`#/<relative_path>`).

### Nix Develop Detection

Marimo is launched inside `nix develop` when the remote project has a `.envrc` with `use flake` and a `flake.nix`. This ensures devshell packages (PyTorch, CUDA, etc.) are available in the notebook. If no nix environment is detected, marimo falls back to `.venv/bin/marimo`.

## File Layout

```
sft-marimo/
├── src/sft_marimo/
│   ├── __init__.py          # Imports hooks.register()
│   ├── hooks.py             # Entry point: registers marimo subcommands
│   └── marimo.py            # Session orchestration (start/stop/status/list)
├── tests/
│   └── test_marimo.py       # Unit tests
├── pyproject.toml
└── README.md
```

## Prerequisites

On the **local** machine:
- `opencode` CLI (for ACP agent)
- `npx` (Node.js, for `stdio-to-ws`)
- `sshfs` (for mount)

On the **remote** machine:
- `marimo` (install via `pip install marimo` or `uv add marimo`)
- Python 3.10+
- Optional: `nix` with a `flake.nix` devshell containing marimo

## Development

```bash
# Test marimo module directly
PYTHONPATH=../sft/src:src \
  python3 -c "from sft_marimo.marimo import cmd_marimo_list; print('ok')"
```

See the main [sft README](https://github.com/StarsInDmajor/sft) for the NixOS development workflow.

## License

MIT
