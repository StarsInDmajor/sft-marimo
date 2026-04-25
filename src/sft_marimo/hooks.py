"""Plugin hooks for sft-marimo.

Registers the ``marimo`` subcommand with sft's CLI.
"""

from __future__ import annotations

from sft.plugins import register_subcommand, register_subcommand_parser

from sft_marimo.marimo import cmd_marimo_start, cmd_marimo_status, cmd_marimo_stop, cmd_marimo_list


MARIMO_COMMANDS = {
    "start": cmd_marimo_start,
    "status": cmd_marimo_status,
    "stop": cmd_marimo_stop,
    "list": cmd_marimo_list,
}


def _dispatch_marimo(args, ctx) -> None:
    import sys

    marimo_cmd = getattr(args, "marimo_command", None)
    if not marimo_cmd:
        remaining = [a for a in sys.argv[2:] if not a.startswith("-")]
        if remaining and remaining[0] not in MARIMO_COMMANDS:
            sys.argv.insert(2, "start")
            marimo_cmd = "start"
    if marimo_cmd and marimo_cmd in MARIMO_COMMANDS:
        MARIMO_COMMANDS[marimo_cmd](args, ctx)
    else:
        print("Usage: sft marimo {start,status,stop,list}", file=sys.stderr)
        sys.exit(1)


def _add_marimo_parser(subparsers, global_parent) -> None:
    import argparse

    marimo_parser = subparsers.add_parser(
        "marimo",
        help="Remote marimo notebook + local ACP agent",
        parents=[global_parent],
    )
    marimo_sub = marimo_parser.add_subparsers(dest="marimo_command")

    marimo_start_parser = marimo_sub.add_parser(
        "start",
        help="Start remote marimo + local agent (default if no subcommand)",
        parents=[global_parent],
    )
    marimo_start_parser.add_argument("target", help="Remote target (host:/path)")
    marimo_start_parser.add_argument("--port", type=int, default=None, help="Marimo port")
    marimo_start_parser.add_argument("--agent-port", type=int, default=None, help="ACP agent port")
    marimo_start_parser.add_argument("--no-agent", action="store_true", help="Skip ACP agent")
    marimo_start_parser.add_argument("--no-open", action="store_true", help="Don't open browser")
    marimo_start_parser.add_argument("--no-auto-env", action="store_true", help="Skip env detection")
    marimo_start_parser.add_argument("-b", "--background", action="store_true", help="Run in background")

    marimo_sub.add_parser(
        "status", help="Show marimo session status", parents=[global_parent],
    ).add_argument("session_id", nargs="?", default=None, help="Session ID")

    marimo_stop_parser = marimo_sub.add_parser(
        "stop", help="Stop marimo session", parents=[global_parent],
    )
    marimo_stop_parser.add_argument("session_id", nargs="?", default=None, help="Session ID")

    marimo_sub.add_parser("list", help="List all marimo sessions", parents=[global_parent])


def register() -> None:
    """Register marimo subcommands with sft."""
    register_subcommand("marimo", _dispatch_marimo)
    register_subcommand_parser("marimo", _add_marimo_parser)


# Auto-register on import so that discover_plugins() via ep.load() activates us.
register()
