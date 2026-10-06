from __future__ import annotations

import argparse
import sys

from .collect import collect
from .config import load_config
from .process import process


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="stock-activity")
    subcommands = root.add_subparsers(dest="command", required=True)
    for name in ("collect", "process"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", default="config.toml")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.command == "collect":
            path = collect(settings)
            print(f"collected: {path}")
        if args.command == "process":
            outputs = process(settings)
            print(f"processed: {len(outputs)} files in {settings.output_dir}")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"stock-activity: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
