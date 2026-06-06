#!/usr/bin/env python3

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sys
from typing import Optional

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

try:
    from pypresence import AioPresence
except ModuleNotFoundError as exc:
    if exc.name != "pypresence":
        raise
    print(
        "main.py requires pypresence. Install it with: python3 -m pip install pypresence",
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
DEFAULT_DETECTABLE_PATH = Path(__file__).resolve().with_name("detectable.json")
DISCORD_CONNECTION_TIMEOUT = 5
DISCORD_RESPONSE_TIMEOUT = 5


class KWinScriptError(RuntimeError):
    pass


class DetectableDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectableProcess:
    application_id: str
    application_name: Optional[str]
    os: str
    executable_name: str


DetectableIndex = dict[str, dict[str, list[DetectableProcess]]]


class DiscordPresenceManager:
    def __init__(self) -> None:
        self._rpc: Optional[AioPresence] = None
        self._application_id: Optional[str] = None
        self._pid: Optional[int] = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def set_detected_process(self, process: DetectableProcess, *, pid: int) -> None:
        task = asyncio.create_task(self._set_application(process, pid=pid))
        self._tasks.add(task)
        task.add_done_callback(self._handle_task_done)

    def _handle_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"failed to update Discord presence: {exc}", file=sys.stderr, flush=True)

    async def _set_application(self, process: DetectableProcess, *, pid: int) -> None:
        async with self._lock:
            if self._application_id == process.application_id and self._rpc is not None:
                if self._pid != pid:
                    try:
                        await self._rpc.update(pid=pid, name=process.application_name)
                    except Exception as exc:
                        await self._close_rpc()
                        raise RuntimeError(
                            f"{process.application_id} ({process.application_name or process.executable_name}): {exc}"
                        ) from exc
                    self._pid = pid
                return

            await self._close_rpc()

            rpc = AioPresence(
                process.application_id,
                connection_timeout=DISCORD_CONNECTION_TIMEOUT,
                response_timeout=DISCORD_RESPONSE_TIMEOUT,
            )

            try:
                await rpc.connect()
                await rpc.update(pid=pid, name=process.application_name)
            except Exception as exc:
                await self._close_rpc_client(rpc, clear_pid=None)
                raise RuntimeError(
                    f"{process.application_id} ({process.application_name or process.executable_name}): {exc}"
                ) from exc

            self._rpc = rpc
            self._application_id = process.application_id
            self._pid = pid

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        async with self._lock:
            await self._close_rpc()

    async def _close_rpc(self) -> None:
        rpc = self._rpc
        pid = self._pid
        self._rpc = None
        self._application_id = None
        self._pid = None

        if rpc is not None:
            await self._close_rpc_client(rpc, clear_pid=pid)

    async def _close_rpc_client(self, rpc: AioPresence, *, clear_pid: Optional[int]) -> None:
        if rpc.sock_writer is None:
            return

        if clear_pid is not None:
            try:
                await rpc.clear(pid=clear_pid)
            except Exception as exc:
                print(f"failed to clear Discord presence: {exc}", file=sys.stderr, flush=True)

        try:
            rpc.send_data(2, {"v": 1, "client_id": rpc.client_id})
        except (AssertionError, OSError, RuntimeError):
            pass

        writer = rpc.sock_writer
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):
            pass


class KWinWindowEvents(ServiceInterface):
    def __init__(
        self,
        *,
        json_output: bool,
        detectables: DetectableIndex,
        presence_manager: DiscordPresenceManager,
    ) -> None:
        super().__init__(DBUS_INTERFACE)
        self._json_output = json_output
        self._detectables = detectables
        self._presence_manager = presence_manager

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

        argv = read_argv(pid)
        exe_path = read_exe_path(pid)
        is_wine = is_wine_exe_path(exe_path)
        detectable_os = "win32" if is_wine else "linux"
        detected_process = detect_process(
            self._detectables,
            os_name=detectable_os,
            argv=argv,
            exe_path=exe_path,
        )
        if detected_process is not None:
            self._presence_manager.set_detected_process(detected_process, pid=pid)

        if self._json_output:
            payload = {
                "pid": pid,
                "argv": argv,
                "is_wine": is_wine,
                "detectable_os": detectable_os,
                "detected_process": detectable_process_to_json(detected_process),
            }
            print(json.dumps(payload, separators=(",", ":")), flush=True)
        else:
            detected_process_text = format_detected_process(detected_process)
            print(
                f"{pid}\twine={str(is_wine).lower()}\tos={detectable_os}"
                f"\tdetected={detected_process_text}\t{shlex.join(argv)}",
                flush=True,
            )

        return True


def read_argv(pid: int) -> list[str]:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        print(f"failed to read /proc/{pid}/cmdline: {exc}", file=sys.stderr, flush=True)
        return []

    if not cmdline:
        return []

    if cmdline.endswith(b"\0"):
        cmdline = cmdline[:-1]

    return [
        arg.decode("utf-8", errors="replace")
        for arg in cmdline.split(b"\0")
    ]


def read_exe_path(pid: int) -> Optional[Path]:
    try:
        return Path(f"/proc/{pid}/exe").readlink()
    except OSError:
        return None


def is_wine_exe_path(exe_path: Optional[Path]) -> bool:
    if exe_path is None:
        return False

    return exe_path.name.startswith("wine")


def is_wine_process(pid: int) -> bool:
    return is_wine_exe_path(read_exe_path(pid))


def load_detectables(path: Path) -> DetectableIndex:
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise DetectableDataError(f"failed to read detectable data {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DetectableDataError(f"failed to parse detectable data {path}: {exc}") from exc

    if not isinstance(data, list):
        raise DetectableDataError(f"detectable data must be a list: {path}")

    index: DetectableIndex = {}
    for application in data:
        if not isinstance(application, dict):
            continue

        application_id = application.get("id")
        if not isinstance(application_id, str):
            continue

        application_name_value = application.get("name")
        application_name = application_name_value if isinstance(application_name_value, str) else None

        executables = application.get("executables")
        if not isinstance(executables, list):
            continue

        for executable in executables:
            if not isinstance(executable, dict):
                continue

            os_name = executable.get("os")
            executable_name = executable.get("name")
            if not isinstance(os_name, str) or not isinstance(executable_name, str):
                continue

            normalized_name = normalize_executable_name(executable_name)
            if not normalized_name:
                continue

            process = DetectableProcess(
                application_id=application_id,
                application_name=application_name,
                os=os_name,
                executable_name=executable_name,
            )
            index.setdefault(os_name, {}).setdefault(normalized_name, []).append(process)

    return index


def normalize_executable_name(name: str) -> str:
    return name.strip().replace("\\", "/").casefold()


def process_executable_candidates(
    *,
    os_name: str,
    argv: list[str],
    exe_path: Optional[Path],
) -> list[str]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        normalized = normalize_executable_name(candidate)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    if exe_path is not None:
        add(str(exe_path))
        add(exe_path.name)

    if not argv:
        return candidates

    if os_name == "win32":
        for arg in argv:
            normalized = normalize_executable_name(arg)
            if normalized.endswith(".exe") or "/" in normalized:
                add(arg)
    else:
        add(argv[0])

    return candidates


def executable_name_suffixes(candidate: str) -> list[str]:
    parts = [part for part in candidate.split("/") if part]
    return ["/".join(parts[index:]) for index in range(len(parts))]


def unique_process_match(matches: Optional[list[DetectableProcess]]) -> Optional[DetectableProcess]:
    if not matches:
        return None

    application_ids = {match.application_id for match in matches}
    if len(application_ids) != 1:
        return None

    return matches[0]


def detect_process(
    detectables: DetectableIndex,
    *,
    os_name: str,
    argv: list[str],
    exe_path: Optional[Path],
) -> Optional[DetectableProcess]:
    os_detectables = detectables.get(os_name, {})
    if not os_detectables:
        return None

    for candidate in process_executable_candidates(os_name=os_name, argv=argv, exe_path=exe_path):
        for suffix in executable_name_suffixes(candidate):
            match = unique_process_match(os_detectables.get(suffix))
            if match is not None:
                return match

    return None


def detectable_process_to_json(process: Optional[DetectableProcess]) -> Optional[dict[str, object]]:
    if process is None:
        return None

    return {
        "id": process.application_id,
        "name": process.application_name,
        "os": process.os,
        "executable": process.executable_name,
    }


def format_detected_process(process: Optional[DetectableProcess]) -> str:
    if process is None:
        return "-"

    if process.application_name is None:
        return f"{process.application_id}/{process.executable_name}"

    return f"{process.application_id}/{process.application_name}/{process.executable_name}"


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
    detectable_path: Path,
) -> None:
    detectables = load_detectables(detectable_path)
    presence_manager = DiscordPresenceManager()
    bus = await MessageBus().connect()
    loaded_kwin_script = False

    try:
        name_reply = await bus.request_name(DBUS_SERVICE)
        if name_reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise KWinScriptError(f"could not own D-Bus service {DBUS_SERVICE}: {name_reply}")

        bus.export(
            DBUS_PATH,
            KWinWindowEvents(
                json_output=json_output,
                detectables=detectables,
                presence_manager=presence_manager,
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
