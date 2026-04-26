# sft-marimo

[English](README.md) | **中文**

[sft](https://github.com/StarsInDmajor/sft) 的 Marimo notebook 插件。提供远程 marimo notebook 服务器启动、本地 ACP agent（OpenCode）编排和 SSH 端口转发。

## 功能

1. **通过 SSH 在远程启动 marimo** — 自动检测 `.venv/bin/marimo` 或系统 `marimo`
2. **检测 Nix 环境** — 发现 `.envrc` + `flake.nix` 时自动在 `nix develop` 中启动
3. **SSH 端口转发** — 将 marimo 端口转发到本地 `localhost`，直接在浏览器中访问
4. **本地 ACP agent** — 在 SSHFS 挂载目录下启动 `npx stdio-to-ws opencode acp`，为 notebook 提供 AI 辅助
5. **会话管理** — start/stop/status/list，状态持久化在 `~/.local/state/sft/marimo-sessions/`

## 安装

```bash
pip install sft-marimo
```

NixOS 用户：此插件会与 sft 核心及其他插件一起构建为 `packages.sft`。

## 使用方法

```bash
# 启动远程 marimo + 本地 agent
sft marimo start my-server:~/project

# 启动并打开指定 notebook
sft marimo start my-server:~/project/notebook.py

# 不启动 agent（无 OpenCode ACP）
sft marimo start my-server:~/project --no-agent

# 不自动打开浏览器
sft marimo start my-server:~/project --no-open

# 跳过 nix develop 检测
sft marimo start my-server:~/project --no-auto-env

# 后台运行
sft marimo start my-server:~/project -b

# 会话管理
sft marimo status
sft marimo list
sft marimo stop [session_id]
```

## 架构

```
浏览器 ←→ localhost:8686 ←(SSH -L)→ 远程 marimo (headless)
OpenCode ACP ←→ localhost:3023 ←(stdio-to-ws)→ SSHFS 挂载目录下的本地 agent
```

### 会话生命周期

1. **预检** — 检查本地是否有 `opencode`、`npx`
2. **挂载** — SSHFS 挂载远程项目（如已存活则复用）
3. **环境检测** — 检查远程 `.envrc`/`flake.nix` 以决定是否使用 nix develop
4. **查找 marimo** — `.venv/bin/marimo` 或系统 `marimo`
5. **启动** — 通过 `nix develop --command`（或直接运行）加 `nohup` 启动
6. **令牌** — 从远程日志中获取认证令牌
7. **转发** — SSH `-L` 端口转发 marimo 端口
8. **Agent** — 在挂载路径下启动 `npx stdio-to-ws opencode acp`
9. **清理** — atexit + SIGINT/SIGTERM 信号 + 显式 `stop`

### Notebook 文件自动检测

若目标路径以 `.py` 结尾，`_resolve_project_root()` 会向上查找项目标记（`.git`、`flake.nix`、`.envrc`、`pyproject.toml`、`.venv`）。项目根目录用于 SSHFS 挂载和环境检测，`.py` 文件则传给 `marimo edit` 并作为 URL 片段（`#/<relative_path>`）附加。

### Nix Develop 检测

当远程项目包含带有 `use flake` 的 `.envrc` 和 `flake.nix` 时，marimo 会在 `nix develop` 内启动，确保 devshell 中的包（如 PyTorch、CUDA）在 notebook 中可用。若未检测到 Nix 环境，则回退到 `.venv/bin/marimo`。

## 文件结构

```
sft-marimo/
├── src/sft_marimo/
│   ├── __init__.py          # 导入 hooks.register()
│   ├── hooks.py             # 入口点：注册 marimo 子命令
│   └── marimo.py            # 会话编排（start/stop/status/list）
├── tests/
│   └── test_marimo.py       # 单元测试
├── pyproject.toml
└── README.md
```

## 前置条件

**本地**机器需要：
- `opencode` CLI（用于 ACP agent）
- `npx`（Node.js，用于 `stdio-to-ws`）
- `sshfs`（用于挂载）

**远程**机器需要：
- `marimo`（通过 `pip install marimo` 或 `uv add marimo` 安装）
- Python 3.10+
- 可选：`nix`，以及包含 marimo 的 `flake.nix` devshell

## 开发

```bash
# 直接测试 marimo 模块
PYTHONPATH=../sft/src:src \
  python3 -c "from sft_marimo.marimo import cmd_marimo_list; print('ok')"
```

NixOS 开发工作流详见 [sft 主仓库 README](https://github.com/StarsInDmajor/sft)。

## 许可证

MIT
