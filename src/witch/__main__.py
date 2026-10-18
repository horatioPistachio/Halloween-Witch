"""Command-line entry point: python -m witch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from witch.app import run
from witch.audio.mic import list_input_devices
from witch.config import ConfigError, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="witch", description="Talk to the Halloween witch.")
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--model", help="Realtime model (default: WITCH_MODEL or gpt-realtime-2.1-mini)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    args = parser.parse_args(argv)

    if args.list_devices:
        for index, name, rate in list_input_devices():
            print(f"{index:>3}  {name}  ({rate} Hz default)")
        return 0

    try:
        settings = load_settings(model=args.model, device=args.device, log_level=args.log_level)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
