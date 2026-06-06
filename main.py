#!/usr/bin/env python3

import argparse
import asyncio
from pathlib import Path
import sys

from dbus_next.aio import MessageBus
from dbus_next.constants import RequestNameReply
from dbus_next.errors import DBusError, InterfaceNotFoundError

from desktop_notifications import DesktopNotifier
from discord_presence import (
    DEFAULT_DETECTABLE_PATH,
    DetectableDataError,
    DiscordPresenceManager,
    DiscordWindowActivityHandler,
    load_detectables,
)
from kde import (
    DBUS_PATH,
    DBUS_SERVICE,
    DEFAULT_KWIN_SCRIPT_PATH,
    KWIN_SCRIPT_NAME,
    KWinScriptError,
    KWinWindowEvents,
    load_kwin_script,
    unload_kwin_script,
)


async def run(
    *,
    json_output: bool,
    load_script: bool,
    keep_script: bool,
    kwin_script_path: Path,
    kwin_script_name: str,
    detectable_path: Path,
) -> None:
    detectables = load_detectables(detectable_path)
    bus = await MessageBus().connect()
    presence_manager = DiscordPresenceManager(notifier=DesktopNotifier(bus))
    activity_handler = DiscordWindowActivityHandler(
        json_output=json_output,
        detectables=detectables,
        presence_manager=presence_manager,
    )
    loaded_kwin_script = False

    try:
        name_reply = await bus.request_name(DBUS_SERVICE)
        if name_reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise KWinScriptError(f"could not own D-Bus service {DBUS_SERVICE}: {name_reply}")

        bus.export(
            DBUS_PATH,
            KWinWindowEvents(
                on_window_added=activity_handler.window_added,
                on_window_closed=activity_handler.windows_closed,
            ),
        )

        if load_script:
            await load_kwin_script(bus, kwin_script_path, kwin_script_name)
            loaded_kwin_script = True

        await asyncio.Future()
    finally:
        if loaded_kwin_script and not keep_script:
            try:
                await unload_kwin_script(bus, kwin_script_name)
            except (DBusError, InterfaceNotFoundError) as exc:
                print(f"failed to unload KWin script: {exc}", file=sys.stderr)
        await presence_manager.close()
        bus.disconnect()
        await bus.wait_for_disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen for KWin window-added events over D-Bus and print their PIDs.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print received events as compact JSON instead of plain PIDs",
    )
    parser.add_argument(
        "--kwin-script",
        type=Path,
        default=DEFAULT_KWIN_SCRIPT_PATH,
        help=f"KWin script path to load over D-Bus (default: {DEFAULT_KWIN_SCRIPT_PATH})",
    )
    parser.add_argument(
        "--kwin-script-name",
        default=KWIN_SCRIPT_NAME,
        help=f"KWin script name used for load/unload (default: {KWIN_SCRIPT_NAME})",
    )
    parser.add_argument(
        "--detectable",
        type=Path,
        default=DEFAULT_DETECTABLE_PATH,
        help=f"Discord detectable application data path (default: {DEFAULT_DETECTABLE_PATH})",
    )
    parser.add_argument(
        "--no-load-kwin-script",
        action="store_true",
        help="do not load kwin.js into KWin; only listen for incoming D-Bus calls",
    )
    parser.add_argument(
        "--keep-kwin-script",
        action="store_true",
        help="leave the loaded KWin script running when main.py exits",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(
            run(
                json_output=args.json,
                load_script=not args.no_load_kwin_script,
                keep_script=args.keep_kwin_script,
                kwin_script_path=args.kwin_script,
                kwin_script_name=args.kwin_script_name,
                detectable_path=args.detectable,
            )
        )
    except KeyboardInterrupt:
        pass
    except (DBusError, InterfaceNotFoundError, KWinScriptError, DetectableDataError) as exc:
        print(f"main.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
