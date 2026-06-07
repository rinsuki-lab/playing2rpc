import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sys
from typing import Optional, Protocol

from pypresence import AioPresence


DEFAULT_DETECTABLE_PATH = Path(__file__).resolve().with_name("detectable.json")
DISCORD_CONNECTION_TIMEOUT = 5
DISCORD_RESPONSE_TIMEOUT = 5
PROCESS_EXIT_POLL_INTERVAL_SECONDS = 5
PROCESS_EXIT_POLL_ATTEMPTS = 12


class DetectableDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectableProcess:
    application_id: str
    application_name: Optional[str]
    os: Optional[str]
    executable_name: Optional[str]
    match_type: str
    steam_app_id: Optional[str] = None


@dataclass(frozen=True)
class DetectableIndex:
    executables: dict[str, dict[str, list[DetectableProcess]]]
    steam_app_ids: dict[str, list[DetectableProcess]]


class NotificationSender(Protocol):
    async def notify(self, *, summary: str, body: str) -> None:
        ...


class DiscordPresenceManager:
    def __init__(self, *, notifier: Optional[NotificationSender] = None) -> None:
        self._rpc: Optional[AioPresence] = None
        self._application_id: Optional[str] = None
        self._application_label: Optional[str] = None
        self._pid: Optional[int] = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._process_exit_watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._notifier = notifier

    def set_detected_process(self, process: DetectableProcess, *, pid: int) -> None:
        self._cancel_process_exit_watch(pid)
        task = asyncio.create_task(self._set_application(process, pid=pid))
        self._tasks.add(task)
        task.add_done_callback(self._handle_task_done)

    def watch_process_exit_after_windows_closed(self, *, pid: int) -> None:
        if pid in self._process_exit_watch_tasks:
            return

        task = asyncio.create_task(self._watch_process_exit_after_windows_closed(pid))
        self._process_exit_watch_tasks[pid] = task
        self._tasks.add(task)
        task.add_done_callback(lambda task, pid=pid: self._handle_process_exit_watch_done(pid, task))
        task.add_done_callback(self._handle_task_done)

    def _handle_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"failed to update Discord presence: {exc}", file=sys.stderr, flush=True)

    def _handle_process_exit_watch_done(self, pid: int, task: asyncio.Task[None]) -> None:
        if self._process_exit_watch_tasks.get(pid) is task:
            del self._process_exit_watch_tasks[pid]

    def _cancel_process_exit_watch(self, pid: int) -> None:
        task = self._process_exit_watch_tasks.pop(pid, None)
        if task is not None and not task.done():
            task.cancel()

    def _cancel_process_exit_watches(self, *, except_pid: Optional[int] = None) -> None:
        for pid in list(self._process_exit_watch_tasks):
            if pid == except_pid:
                continue
            self._cancel_process_exit_watch(pid)

    async def _set_application(self, process: DetectableProcess, *, pid: int) -> None:
        async with self._lock:
            process_label = process_display_name(process)
            if self._application_id == process.application_id and self._rpc is not None:
                if self._pid != pid:
                    try:
                        await self._rpc.update(pid=pid, name=process.application_name)
                    except Exception as exc:
                        await self._close_rpc()
                        raise RuntimeError(
                            f"{process.application_id} ({process_display_name(process)}): {exc}"
                        ) from exc
                    self._pid = pid
                self._application_label = process_label
                self._cancel_process_exit_watches(except_pid=pid)
                return

            previous_application_label = self._application_label
            presence_was_active = self._rpc is not None
            await self._close_rpc(notify_end=False)

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
                if presence_was_active:
                    await self._notify_presence_ended(previous_application_label)
                raise RuntimeError(
                    f"{process.application_id} ({process_display_name(process)}): {exc}"
                ) from exc

            self._rpc = rpc
            self._application_id = process.application_id
            self._application_label = process_label
            self._pid = pid
            self._cancel_process_exit_watches(except_pid=pid)
            if presence_was_active:
                await self._notify_presence_switched(
                    old_label=previous_application_label,
                    new_label=process_label,
                )
            else:
                await self._notify_presence_started(process_label)

    async def _watch_process_exit_after_windows_closed(self, pid: int) -> None:
        for _ in range(PROCESS_EXIT_POLL_ATTEMPTS):
            await asyncio.sleep(PROCESS_EXIT_POLL_INTERVAL_SECONDS)
            if not process_has_exited(pid):
                continue

            async with self._lock:
                if self._pid == pid:
                    await self._close_rpc()
            return

    async def close(self) -> None:
        self._cancel_process_exit_watches()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        async with self._lock:
            await self._close_rpc()

    async def _close_rpc(self, *, notify_end: bool = True) -> None:
        rpc = self._rpc
        pid = self._pid
        application_label = self._application_label
        self._rpc = None
        self._application_id = None
        self._application_label = None
        self._pid = None

        if rpc is not None:
            await self._close_rpc_client(rpc, clear_pid=pid)
            if notify_end:
                await self._notify_presence_ended(application_label)

    async def _notify_presence_started(self, application_label: str) -> None:
        await self._notify(
            summary="Rich Presence started",
            body=application_label,
        )

    async def _notify_presence_switched(
        self,
        *,
        old_label: Optional[str],
        new_label: str,
    ) -> None:
        if old_label is None:
            await self._notify_presence_started(new_label)
            return

        await self._notify(
            summary="Rich Presence switched",
            body=f"{old_label} -> {new_label}",
        )

    async def _notify_presence_ended(self, application_label: Optional[str]) -> None:
        await self._notify(
            summary="Rich Presence ended",
            body=application_label or "Unknown application",
        )

    async def _notify(self, *, summary: str, body: str) -> None:
        if self._notifier is None:
            return

        try:
            await self._notifier.notify(summary=summary, body=body)
        except Exception as exc:
            print(f"failed to send desktop notification: {exc}", file=sys.stderr, flush=True)

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


class DiscordWindowActivityHandler:
    def __init__(
        self,
        *,
        json_output: bool,
        detectables: DetectableIndex,
        presence_manager: DiscordPresenceManager,
    ) -> None:
        self._json_output = json_output
        self._detectables = detectables
        self._presence_manager = presence_manager

    def window_added(self, pid: int) -> bool:
        argv = read_argv(pid)
        environ = read_environ(pid)
        exe_path = read_exe_path(pid)
        is_wine = is_wine_exe_path(exe_path)
        detectable_os = "win32" if is_wine else "linux"
        detected_process = detect_process(
            self._detectables,
            os_name=detectable_os,
            argv=argv,
            exe_path=exe_path,
            environ=environ,
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

    def windows_closed(self, pid: int) -> bool:
        self._presence_manager.watch_process_exit_after_windows_closed(pid=pid)
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


def read_environ(pid: int) -> dict[str, str]:
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        print(f"failed to read /proc/{pid}/environ: {exc}", file=sys.stderr, flush=True)
        return {}

    if not environ:
        return {}

    if environ.endswith(b"\0"):
        environ = environ[:-1]

    result: dict[str, str] = {}
    for item in environ.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if not separator:
            continue

        key_text = key.decode("utf-8", errors="replace")
        value_text = value.decode("utf-8", errors="replace")
        result[key_text] = value_text

    return result


def read_exe_path(pid: int) -> Optional[Path]:
    try:
        return Path(f"/proc/{pid}/exe").readlink()
    except OSError:
        return None


def process_has_exited(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"failed to check /proc/{pid}/stat: {exc}", file=sys.stderr, flush=True)
        return False

    state = process_stat_state(stat)
    if state is None:
        return False

    return state in {"X", "Z"}


def process_stat_state(stat: str) -> Optional[str]:
    _, separator, rest = stat.rpartition(")")
    if not separator:
        return None

    fields = rest.strip().split()
    if not fields:
        return None

    return fields[0]


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

    executable_index: dict[str, dict[str, list[DetectableProcess]]] = {}
    steam_app_id_index: dict[str, list[DetectableProcess]] = {}
    for application in data:
        if not isinstance(application, dict):
            continue

        application_id = application.get("id")
        if not isinstance(application_id, str):
            continue

        application_name_value = application.get("name")
        application_name = application_name_value if isinstance(application_name_value, str) else None

        executables = application.get("executables")
        if isinstance(executables, list):
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
                    match_type="executable",
                )
                os_index = executable_index.setdefault(os_name, {})
                os_index.setdefault(normalized_name, []).append(process)

        third_party_skus = application.get("third_party_skus")
        if isinstance(third_party_skus, list):
            for sku in third_party_skus:
                if not isinstance(sku, dict):
                    continue

                distributor = sku.get("distributor")
                sku_id = sku.get("id")
                if distributor != "steam" or not isinstance(sku_id, str):
                    continue

                process = DetectableProcess(
                    application_id=application_id,
                    application_name=application_name,
                    os=None,
                    executable_name=None,
                    match_type="steam_app_id",
                    steam_app_id=sku_id,
                )
                steam_app_id_index.setdefault(sku_id, []).append(process)

    return DetectableIndex(
        executables=executable_index,
        steam_app_ids=steam_app_id_index,
    )


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


def steam_app_id_process(
    steam_match: DetectableProcess,
    *,
    steam_app_id: str,
    executable_match: Optional[DetectableProcess] = None,
) -> DetectableProcess:
    return DetectableProcess(
        application_id=steam_match.application_id,
        application_name=steam_match.application_name,
        os=executable_match.os if executable_match is not None else None,
        executable_name=executable_match.executable_name if executable_match is not None else None,
        match_type="steam_app_id",
        steam_app_id=steam_app_id,
    )


def first_process_with_application_id(
    matches: list[DetectableProcess],
    *,
    application_id: str,
) -> Optional[DetectableProcess]:
    for match in matches:
        if match.application_id == application_id:
            return match

    return None


def detect_executable_process(
    os_detectables: dict[str, list[DetectableProcess]],
    *,
    os_name: str,
    argv: list[str],
    exe_path: Optional[Path],
    application_ids: Optional[set[str]] = None,
) -> Optional[DetectableProcess]:
    if not os_detectables:
        return None

    candidates = process_executable_candidates(os_name=os_name, argv=argv, exe_path=exe_path)
    for candidate in candidates:
        for suffix in executable_name_suffixes(candidate):
            matches = os_detectables.get(suffix)
            if application_ids is not None and matches is not None:
                matches = [
                    match
                    for match in matches
                    if match.application_id in application_ids
                ]

            match = unique_process_match(matches)
            if match is not None:
                return match

    return None


def detect_process(
    detectables: DetectableIndex,
    *,
    os_name: str,
    argv: list[str],
    exe_path: Optional[Path],
    environ: dict[str, str],
) -> Optional[DetectableProcess]:
    os_detectables = detectables.executables.get(os_name, {})
    steam_app_id = environ.get("SteamAppId")
    if steam_app_id is not None:
        steam_matches = detectables.steam_app_ids.get(steam_app_id)
        if steam_matches:
            steam_match = unique_process_match(steam_matches)
            if steam_match is not None:
                return steam_app_id_process(steam_match, steam_app_id=steam_app_id)

            executable_match = detect_executable_process(
                os_detectables,
                os_name=os_name,
                argv=argv,
                exe_path=exe_path,
                application_ids={match.application_id for match in steam_matches},
            )
            if executable_match is not None:
                steam_match = first_process_with_application_id(
                    steam_matches,
                    application_id=executable_match.application_id,
                )
                if steam_match is not None:
                    return steam_app_id_process(
                        steam_match,
                        steam_app_id=steam_app_id,
                        executable_match=executable_match,
                    )

            return None

    return detect_executable_process(
        os_detectables,
        os_name=os_name,
        argv=argv,
        exe_path=exe_path,
    )


def detectable_process_to_json(process: Optional[DetectableProcess]) -> Optional[dict[str, object]]:
    if process is None:
        return None

    result: dict[str, object] = {
        "id": process.application_id,
        "name": process.application_name,
        "match_type": process.match_type,
    }
    if process.os is not None:
        result["os"] = process.os
    if process.executable_name is not None:
        result["executable"] = process.executable_name
    if process.steam_app_id is not None:
        result["steam_app_id"] = process.steam_app_id

    return result


def process_display_name(process: DetectableProcess) -> str:
    if process.application_name is not None:
        return process.application_name

    if process.executable_name is not None:
        return process.executable_name

    if process.steam_app_id is not None:
        return f"Steam {process.steam_app_id}"

    return process.application_id


def format_detected_process(process: Optional[DetectableProcess]) -> str:
    if process is None:
        return "-"

    parts = [process.application_id]
    if process.application_name is not None:
        parts.append(process.application_name)

    if process.match_type == "steam_app_id":
        parts.append(f"steam:{process.steam_app_id}")
        if process.executable_name is not None:
            parts.append(process.executable_name)
    elif process.executable_name is not None:
        parts.append(process.executable_name)

    return "/".join(parts)
