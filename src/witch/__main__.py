"""Command-line entry point: python -m witch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from witch.app import run, say
from witch.audio.base import PlaybackError
from witch.audio.mic import list_input_devices
from witch.audio.speaker import list_output_devices
from witch.config import ConfigError, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="witch", description="Talk to the Halloween witch.")
    parser.add_argument("--list-devices", action="store_true", help="list input and output devices and exit")
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--output-device", help="output device index or name substring")
    parser.add_argument("--model", help="Realtime model (default: WITCH_MODEL or gpt-realtime-2.1-mini)")
    parser.add_argument("--no-tts", action="store_true", help="print replies only; do not call ElevenLabs")
    parser.add_argument("--say", metavar="TEXT", help="speak TEXT with ElevenLabs and exit")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    args = parser.parse_args(argv)

    if args.list_devices:
        print("inputs:")
        for index, name, rate in list_input_devices():
            print(f"{index:>3}  {name}  ({rate} Hz default)")
        print("outputs:")
        for index, name, rate in list_output_devices():
            print(f"{index:>3}  {name}  ({rate} Hz default)")
        return 0

    saying = args.say is not None
    if saying:
        tts: bool | None = True
    elif args.no_tts:
        tts = False
    else:
        tts = None
    try:
        settings = load_settings(
            model=args.model,
            device=args.device,
            output_device=args.output_device,
            log_level=args.log_level,
            tts=tts,
            require_openai=not saying,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        if saying:
            return asyncio.run(say(settings, args.say))
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 0
    except (ConfigError, PlaybackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
