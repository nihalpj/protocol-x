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
    serve.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default=None,
        help="app log level (default: PROTOX_LOG_LEVEL or server.yaml logging.level or info)",
    )
    serve.add_argument("--debug", action="store_true", help="shortcut for --log-level debug")

    doctor = sub.add_parser("doctor", help="diagnose the environment")
    doctor.add_argument("--quick", action="store_true", help="pre-flight checks only")

    sub.add_parser("setup", help="guided first-run setup wizard (P9)")

    convert = sub.add_parser("convert", help="convert a package into tools (P7)")
    convert.add_argument("package", nargs="?")
    convert.add_argument("--focus", default=None)
    convert.add_argument("--resume", action="store_true")
    convert.add_argument("--stream", action="store_true")
    convert.add_argument("--workdir", default=None)
    convert.add_argument("--config", default=None, help="path to server.yaml")
    convert.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default=None,
        help="app log level (default: PROTOX_LOG_LEVEL or server.yaml logging.level or info)",
    )
    convert.add_argument("--debug", action="store_true", help="shortcut for --log-level debug")

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
        from core.logging import get_logger, setup_logging

        level = "DEBUG" if args.debug else (args.log_level or settings.log_level)
        setup_logging(level, settings.app_log_path)
        log = get_logger("main")
        log.info(
            "starting protocol-x-tool-mcp %s (log level %s, data dir %s)",
            VERSION, level, settings.data_dir,
        )
        transport = args.transport or settings.transport
        ctx = AppContext(settings)
        log.info("registry ready: %d tools live", len(ctx.registry.list_tools()))
        if transport == "http":
            serve_http(ctx, args.host or settings.host, args.port or settings.port)
        else:
            serve_stdio(ctx)
        return 0
    if args.command == "doctor":
        from server.doctor import run_doctor

        return run_doctor(quick=args.quick)
    if args.command == "setup":
        from server.setup import run_setup

        return run_setup()
    if args.command == "convert":
        if not args.package:
            print("convert requires a package name", file=sys.stderr)
            return 2
        from server.convert_cli import run_convert

        try:
            settings = Settings(config_file=args.config)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        from core.logging import setup_logging

        setup_logging(
            "DEBUG" if args.debug else (args.log_level or settings.log_level),
            settings.app_log_path,
        )
        return run_convert(
            args.package, settings, focus=args.focus, resume=args.resume,
            stream=args.stream, workdir=args.workdir)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
