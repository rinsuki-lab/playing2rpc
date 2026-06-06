#!/usr/bin/env python3

import argparse
import asyncio
import json
from pathlib import Path
import sys

try:
    from dbus_next import Message, MessageType
    from dbus_next.aio import MessageBus
    from dbus_next.constants import RequestNameReply
    from dbus_next.errors import DBusError, InterfaceNotFoundError
    from dbus_next.service import ServiceInterface, method
except ModuleNotFoundError as exc:
    if exc.name != "dbus_next":
        raise
    print(
        "main.py requires dbus-next. Install it with: python3 -m pip install dbus-next",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


DBUS_SERVICE = "net.rinsuki.lab.Playing2RPC"
DBUS_PATH = "/net/rinsuki/lab/Playing2RPC/KWinWindowEvents"
DBUS_INTERFACE = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents"

KWIN_SERVICE = "org.kde.KWin"
KWIN_SCRIPTING_PATH = "/Scripting"
KWIN_SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
KWIN_SCRIPT_INTERFACE = "org.kde.kwin.Script"
KWIN_SCRIPT_NAME = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents"
DEFAULT_KWIN_SCRIPT_PATH = Path(__file__).resolve().with_name("kwin.js")


class KWinScriptError(RuntimeError):
    pass


class KWinWindowEvents(ServiceInterface):
    def __init__(self, *, json_output: bool) -> None:
        super().__init__(DBUS_INTERFACE)
        self._json_output = json_output

    @method()
    def WindowAdded(self, payload: "s") -> "b":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON payload: {payload!r}: {exc}", file=sys.stderr, flush=True)
            return False

        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            print(f"invalid pid payload: {payload!r}", file=sys.stderr, flush=True)
            return False

        if self._json_output:
            print(json.dumps({"pid": pid}, separators=(",", ":")), flush=True)
        else:
            print(pid, flush=True)

        return True


async def get_dbus_interface(bus: MessageBus, service: str, path: str, interface: str):
    introspection = await bus.introspect(service, path)
    proxy = bus.get_proxy_object(service, path, introspection)
    return proxy.get_interface(interface)


async def try_unload_kwin_script(scripting, script_name: str) -> bool:
    try:
        return bool(await scripting.call_unload_script(script_name))
    except DBusError:
        return False


async def call_kwin_load_script(bus: MessageBus, script_path: Path, script_name: str) -> int:
    reply = await bus.call(
        Message(
            destination=KWIN_SERVICE,
            path=KWIN_SCRIPTING_PATH,
            interface=KWIN_SCRIPTING_INTERFACE,
            member="loadScript",
            signature="ss",
            body=[str(script_path), script_name],
        )
    )

    if reply.message_type == MessageType.ERROR:
        raise KWinScriptError(f"KWin loadScript failed: {reply.error_name}: {reply.body!r}")
    if not reply.body or not isinstance(reply.body[0], int):
        raise KWinScriptError(f"unexpected KWin loadScript reply: {reply.body!r}")

    return reply.body[0]


async def run_loaded_kwin_script(bus: MessageBus, script_id: int) -> None:
    errors: list[Exception] = []
    for path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
        try:
            script = await get_dbus_interface(bus, KWIN_SERVICE, path, KWIN_SCRIPT_INTERFACE)
            await script.call_run()
            return
        except (DBusError, InterfaceNotFoundError) as exc:
            errors.append(exc)

    detail = "; ".join(str(error) for error in errors)
    raise KWinScriptError(f"loaded KWin script {script_id}, but could not run it: {detail}")


async def load_kwin_script(bus: MessageBus, script_path: Path, script_name: str) -> int:
    script_path = script_path.expanduser().resolve()
    if not script_path.is_file():
        raise KWinScriptError(f"KWin script does not exist: {script_path}")

    scripting = await get_dbus_interface(
        bus,
        KWIN_SERVICE,
        KWIN_SCRIPTING_PATH,
        KWIN_SCRIPTING_INTERFACE,
    )

    await try_unload_kwin_script(scripting, script_name)

    script_id = await call_kwin_load_script(bus, script_path, script_name)
    if script_id == -1:
        unloaded = await try_unload_kwin_script(scripting, script_name)
        if not unloaded:
            raise KWinScriptError(f"KWin refused to load script and could not unload: {script_name}")
        script_id = await call_kwin_load_script(bus, script_path, script_name)

    if script_id < 0:
        raise KWinScriptError(f"KWin refused to load script: {script_path}")

    try:
        await run_loaded_kwin_script(bus, script_id)
    except Exception:
        await try_unload_kwin_script(scripting, script_name)
        raise

    return int(script_id)


async def unload_kwin_script(bus: MessageBus, script_name: str) -> bool:
    scripting = await get_dbus_interface(
        bus,
        KWIN_SERVICE,
        KWIN_SCRIPTING_PATH,
        KWIN_SCRIPTING_INTERFACE,
    )
    return await try_unload_kwin_script(scripting, script_name)


async def run(
    *,
    json_output: bool,
    load_script: bool,
    keep_script: bool,
    kwin_script_path: Path,
    kwin_script_name: str,
) -> None:
    bus = await MessageBus().connect()
    loaded_kwin_script = False

    try:
        name_reply = await bus.request_name(DBUS_SERVICE)
        if name_reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise KWinScriptError(f"could not own D-Bus service {DBUS_SERVICE}: {name_reply}")

        bus.export(DBUS_PATH, KWinWindowEvents(json_output=json_output))

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
            )
        )
    except KeyboardInterrupt:
        pass
    except (DBusError, InterfaceNotFoundError, KWinScriptError) as exc:
        print(f"main.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
