import json
from pathlib import Path
import sys
from typing import Callable, Optional

from dbus_next import Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method


DBUS_SERVICE = "net.rinsuki.lab.Playing2RPC"
DBUS_PATH = "/net/rinsuki/lab/Playing2RPC/KWinWindowEvents"
DBUS_INTERFACE = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents"

KWIN_SERVICE = "org.kde.KWin"
KWIN_SCRIPTING_PATH = "/Scripting"
KWIN_SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
KWIN_SCRIPT_INTERFACE = "org.kde.kwin.Script"
KWIN_SCRIPT_NAME = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents"
DEFAULT_KWIN_SCRIPT_PATH = Path(__file__).resolve().with_name("kwin.js")

WindowAddedCallback = Callable[[int], bool]
WindowClosedCallback = Callable[[int], bool]


class KWinScriptError(RuntimeError):
    pass


class KWinWindowEvents(ServiceInterface):
    def __init__(
        self,
        *,
        on_window_added: WindowAddedCallback,
        on_window_closed: WindowClosedCallback,
    ) -> None:
        super().__init__(DBUS_INTERFACE)
        self._on_window_added = on_window_added
        self._on_window_closed = on_window_closed

    def _parse_pid_payload(self, payload: str) -> Optional[int]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON payload: {payload!r}: {exc}", file=sys.stderr, flush=True)
            return None

        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            print(f"invalid pid payload: {payload!r}", file=sys.stderr, flush=True)
            return None

        return pid

    @method()
    def WindowAdded(self, payload: "s") -> "b":
        pid = self._parse_pid_payload(payload)
        if pid is None:
            return False

        return self._on_window_added(pid)

    @method()
    def WindowClosed(self, payload: "s") -> "b":
        pid = self._parse_pid_payload(payload)
        if pid is None:
            return False

        return self._on_window_closed(pid)


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
