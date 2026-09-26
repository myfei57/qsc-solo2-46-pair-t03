"""Command-line entry point for the control service."""

from __future__ import annotations

import argparse
import logging

from .server import run
from .wiring import build_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cranefleet", description="CraneFleet control service")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", type=int, default=8080, help="listen port")
    parser.add_argument(
        "--data-dir",
        default="data",
        help="persistent data directory, or 'memory' for an ephemeral run",
    )
    parser.add_argument("--web-root", default="web", help="console frontend directory")
    parser.add_argument("--log-level", default="info", help="logging level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_app(args.data_dir, args.web_root)
    try:
        run(app.console, args.host, args.port)
    except KeyboardInterrupt:
        logging.getLogger("cranefleet").info("interrupted, shutting down")
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
