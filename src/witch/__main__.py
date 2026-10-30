"""Command-line entry point: python -m witch."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal
import sys
from collections.abc import Coroutine
from typing import Any

from witch.app import run, run_controlled, say
from witch.audio.base import PlaybackError
from witch.audio.mic import list_input_devices
from witch.audio.speaker import list_output_devices
from witch.config import DEFAULT_VOICE, VOICES, ConfigError, load_settings
from witch.hardware.base import HardwareError
from witch.hardware.selftest import run_selftest


async def _until_stopped(main: Coroutine[Any, Any, int]) -> int:
    """Turn SIGTERM (systemd stop) into cancellation so cleanup in finally blocks runs."""
    task = asyncio.current_task()
    if sys.platform != "win32" and task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    return await main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="witch", description="Talk to the Halloween witch.")
    parser.add_argument("--list-devices", action="store_true", help="list input and output devices and exit")
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--output-device", help="output device index or name substring")
    parser.add_argument("--model", help="Realtime model (default: WITCH_MODEL or gpt-realtime-2.1-mini)")
    parser.add_argument("--persona", choices=("kids", "evil"), help="kids (default) or evil system prompt")
    parser.add_argument(
        "--voice",
        choices=tuple(VOICES),
        help="ElevenLabs voice: " + ", ".join(VOICES) + f" (default: WITCH_VOICE_ID or {DEFAULT_VOICE})",
    )
    parser.add_argument("--no-tts", action="store_true", help="print replies only; do not call ElevenLabs")
    parser.add_argument(
        "--trigger",
        choices=("none", "pir", "keyboard"),
        help="none: listen continuously; pir: PIR sensor starts conversations; "
        "keyboard: Enter simulates motion (default: WITCH_TRIGGER or none)",
    )
    parser.add_argument(
        "--hardware",
        choices=("gpio", "esp32"),
        help="gpio: Raspberry Pi pins; esp32: ESP32 Feather over USB serial (default: WITCH_HARDWARE or gpio)",
    )
    parser.add_argument("--hw-test", action="store_true", help="check PIR and witch wiring and exit")
    parser.add_argument(
        "--no-opening",
        action="store_true",
        help="do not have the witch speak first when a conversation starts (default: WITCH_OPENING or on)",
    )
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
    elif args.no_tts or args.hw_test:
        tts = False
    else:
        tts = None
    trigger = "pir" if args.hw_test else args.trigger
    try:
        settings = load_settings(
            model=args.model,
            persona=args.persona,
            voice=args.voice,
            device=args.device,
            output_device=args.output_device,
            log_level=args.log_level,
            tts=tts,
            trigger=trigger,
            hardware=args.hardware,
            opening=False if args.no_opening else None,
            require_openai=not (saying or args.hw_test),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if settings.trigger == "keyboard":
        # No PIR to settle when a person types Enter.
        settings = dataclasses.replace(settings, pir_warmup_s=0.0)

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
        if args.hw_test:
            return asyncio.run(_until_stopped(run_selftest(settings)))
        if settings.trigger == "none":
            return asyncio.run(run(settings))
        return asyncio.run(_until_stopped(run_controlled(settings)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopped.", file=sys.stderr)
        return 0
    except (ConfigError, PlaybackError, HardwareError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
