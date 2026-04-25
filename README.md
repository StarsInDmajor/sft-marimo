# sft-marimo

Marimo notebook plugin for [sft](https://github.com/pulcerto/sft).

Provides:
- Remote marimo notebook server launch via SSH
- Local ACP agent (OpenCode) with stdio-to-ws bridge
- SSH port forwarding for browser access
- Session lifecycle management (start/stop/status/list)
- Nix develop environment detection for marimo launch

## Installation

```bash
pip install sft-marimo
```

## Usage

```bash
# Start remote marimo + local agent
sft marimo start my-server:~/project

# Without agent
sft marimo start my-server:~/project --no-agent

# Check sessions
sft marimo status
sft marimo list

# Stop session
sft marimo stop [session_id]
```

## License

MIT
