"""protocol-x-tool-mcp CLI: serve / setup / doctor / convert / version."""

from __future__ import annotations

import argparse
import sys

from server.app import VERSION, AppContext, serve_http, serve_stdio
from server.config import ConfigError, Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="protocol-x-tool-mcp")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default=None)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", default=None, help="path to server.yaml")

    doctor = sub.add_parser("doctor", help="diagnose the environment")
    doctor.add_argument("--quick", action="store_true", help="pre-flight checks only")

    sub.add_parser("setup", help="guided first-run setup wizard (P9)")

    convert = sub.add_parser("convert", help="convert a package into tools (P7)")
    convert.add_argument("package", nargs="?")
    convert.add_argument("--focus", default=None)
    convert.add_argument("--resume", action="store_true")

    sub.add_parser("version", help="print version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version or args.command == "version":
        print(VERSION)
        return 0
    if args.command == "serve":
        try:
            settings = Settings(config_file=args.config)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        transport = args.transport or settings.transport
        ctx = AppContext(settings)
        if transport == "http":
            serve_http(ctx, args.host or settings.host, args.port or settings.port)
        else:
            serve_stdio(ctx)
        return 0
    if args.command == "doctor":
        from server.doctor import run_doctor

        return run_doctor(quick=args.quick)
    if args.command == "setup":
        print("setup wizard lands in P9; see docs/quickstart.md for manual setup", file=sys.stderr)
        return 0
    if args.command == "convert":
        print("converter agent lands in P7; see agents/converter_agent.py skeleton", file=sys.stderr)
        return 0
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
