import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sys
from typing import Optional

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
        self._process_exit_watch_tasks: dict[int, asyncio.Task[None]] = {}

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
                self._cancel_process_exit_watches(except_pid=pid)
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
            self._cancel_process_exit_watches(except_pid=pid)

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
