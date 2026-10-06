from __future__ import annotations

import argparse
import sys

from .collect import collect
from .config import load_config
from .process import process


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="stock-activity")
    root.add_argument(
        "actions",
        nargs="+",
        choices=("collect", "process"),
        metavar="{collect,process}",
        help="one or both actions; when both are used, collect must come first",
    )
    root.add_argument("--config", default="config.toml")
    return root


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if len(set(args.actions)) != len(args.actions):
        argument_parser.error("each action may be specified only once")
    if args.actions == ["process", "collect"]:
        argument_parser.error("collect must precede process")
    try:
        settings = load_config(args.config)
        for action in args.actions:
            if action == "collect":
                path = collect(settings)
                print(f"collected: {path}")
            else:
                outputs = process(settings)
                print(f"processed: {len(outputs)} files in {settings.output_dir}")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"stock-activity: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
