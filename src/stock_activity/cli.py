from __future__ import annotations

import argparse
import sys
from typing import TextIO

from .collect import COLLECT_STEPS, collect
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
    root.add_argument(
        "--from", "--start-from", dest="start_from", choices=COLLECT_STEPS, default="alpha",
        help="collection step to start from; assets and logs use the last checkpoint",
    )
    return root


class CollectVisualizer:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.last_phase: str | None = None
        self.active = False

    def update(self, phase: str, detail: str) -> None:
        text = f"collect: {phase} — {detail}"
        if self.interactive:
            print(f"\r\033[2K{text}", end="", file=self.stream, flush=True)
            self.active = True
        elif phase != self.last_phase:
            print(text, file=self.stream)
        self.last_phase = phase

    def finish(self) -> None:
        if self.interactive and self.active:
            print(file=self.stream)
            self.active = False


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if len(set(args.actions)) != len(args.actions):
        argument_parser.error("each action may be specified only once")
    if args.actions == ["process", "collect"]:
        argument_parser.error("collect must precede process")
    if "collect" not in args.actions and args.start_from != "alpha":
        argument_parser.error("--from can only be used with collect")
    try:
        settings = load_config(args.config)
        for action in args.actions:
            if action == "collect":
                visualizer = CollectVisualizer()
                try:
                    path = collect(settings, start_from=args.start_from, progress=visualizer.update)
                finally:
                    visualizer.finish()
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
