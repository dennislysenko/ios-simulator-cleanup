#!/usr/bin/env python3
"""
Helper CLI for inspecting and pruning iOS Simulator storage.

The script avoids CoreSimulatorService dependencies (simctl) so it can run
even when the service is unavailable. All destructive actions are dry-run
unless --execute is passed explicitly.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import curses
import io
import json
import logging
import os
import queue
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from enum import Enum


HOME = Path.home()
DEVICE_ROOT = HOME / "Library/Developer/CoreSimulator/Devices"
LOG_ROOT = HOME / "Library/Logs/CoreSimulator"
CORE_SIM_ROOT = Path("/Library/Developer/CoreSimulator")
RUNTIME_VOLUMES_ROOT = CORE_SIM_ROOT / "Volumes"
CRYPDEX_BUNDLE_ROOT = CORE_SIM_ROOT / "Cryptex" / "Images" / "bundle"
DYLD_CACHE_PATH = CORE_SIM_ROOT / "Caches" / "dyld"
IB_SUPPORT_SIM_DEVICES = HOME / "Library/Developer/Xcode/UserData/IB Support/Simulator Devices"
CACHE_DIR = HOME / ".cache"
CACHE_FILE = CACHE_DIR / "sim_cleanup_cache.json"
CACHE_VERSION = 1
DETAIL_CACHE_TTL_SECONDS = 600

LOGGER = logging.getLogger("sim_cleanup")


def _configure_logging() -> None:
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
    LOGGER.addHandler(handler)
    level = logging.DEBUG if os.getenv("SIM_CLEANUP_DEBUG", "").lower() in {"1", "true", "yes", "on"} else logging.WARNING
    LOGGER.setLevel(level)
    LOGGER.propagate = False


_configure_logging()

STATE_MAP = {
    0: "Shutdown",
    1: "Booted",
    2: "Booting",
    3: "ShuttingDown",
}


@contextlib.contextmanager
def log_timing(label: str, **details: Any) -> Iterator[None]:
    if not LOGGER.isEnabledFor(logging.DEBUG):
        yield
        return
    detail_text = " ".join(f"{key}={value}" for key, value in details.items())
    message = f"{label}" if not detail_text else f"{label} ({detail_text})"
    LOGGER.debug("Starting %s", message)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        LOGGER.debug("Finished %s in %.2fs", message, elapsed)


class Spinner:
    """Lightweight terminal spinner."""

    def __init__(self, message: str, enabled: Optional[bool] = None, interval: float = 0.1) -> None:
        self.message = message
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        frames = "|/-\\"
        idx = 0
        while not self._stop_event.is_set():
            frame = frames[idx % len(frames)]
            sys.stdout.write(f"\r{self.message} {frame}")
            sys.stdout.flush()
            time.sleep(self.interval)
            idx += 1
        blank = " " * (len(self.message) + 2)
        sys.stdout.write(f"\r{blank}\r")
        sys.stdout.flush()

    def stop(self) -> None:
        if not self.enabled or self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

@dataclass
class DeviceInfo:
    udid: str
    name: str
    runtime: str
    raw_runtime: str
    device_type: str
    state: str
    last_booted_at: Optional[str]
    path: Path
    size_bytes: Optional[int]
    log_bytes: Optional[int]
    is_deleted: bool
    is_ephemeral: bool


class SizeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class DeviceRow:
    info: DeviceInfo
    size_status: SizeStatus = SizeStatus.PENDING
    size_bytes: Optional[int] = None
    progress_bytes: Optional[int] = None
    log_bytes: Optional[int] = None
    error: Optional[str] = None
    # Transient row-level message (e.g. "Cleaning caches…", "Reclaimed 1.7 GB").
    # When set, it takes precedence over size_label() in the TUI.
    row_message: Optional[str] = None

    def size_label(self) -> str:
        if self.row_message:
            return self.row_message
        if self.size_status == SizeStatus.DONE and self.size_bytes is not None:
            return format_size(self.size_bytes)
        if self.size_status == SizeStatus.RUNNING:
            if self.progress_bytes:
                return f"Sizing… ({format_size(self.progress_bytes)})"
            return "Sizing…"
        if self.size_status == SizeStatus.ERROR:
            return "Error"
        return "Pending"


@dataclass
class SizeProgressEvent:
    udid: str
    bytes_done: int


@dataclass
class SizeCompleteEvent:
    udid: str
    total_bytes: int
    log_bytes: Optional[int]


@dataclass
class SizeErrorEvent:
    udid: str
    message: str


@dataclass
class StatusMessageEvent:
    message: str
    duration: Optional[float] = 4.0


@dataclass
class RowMessageEvent:
    udid: str
    message: Optional[str]  # None clears the transient row message


@dataclass
class DetailOutputEvent:
    lines: List[str]
    overlay: bool = False


@dataclass
class ReloadDevicesEvent:
    selected_udid: Optional[str] = None


class CacheManager:
    def __init__(self, path: Path = CACHE_FILE) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {
            "version": CACHE_VERSION,
            "sort_mode": "default",
            "sizes": {},
            "details": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:  # pragma: no cover - best effort
            LOGGER.debug("cache load failed: %s", exc)
            return
        if payload.get("version") != CACHE_VERSION:
            return
        with self.lock:
            self.data.update(
                {
                    "sort_mode": payload.get("sort_mode", "default"),
                    "sizes": payload.get("sizes", {}),
                    "details": payload.get("details", {}),
                }
            )

    def _save_locked(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle)
        except Exception as exc:  # pragma: no cover - best effort
            LOGGER.debug("cache save failed: %s", exc)

    def get_sort_mode(self) -> str:
        with self.lock:
            return self.data.get("sort_mode", "default")

    def set_sort_mode(self, mode: str) -> None:
        with self.lock:
            if self.data.get("sort_mode") == mode:
                return
            self.data["sort_mode"] = mode
            self._save_locked()

    def get_cached_size(self, udid: str, path: Path) -> Optional[Tuple[int, Optional[int]]]:
        try:
            mtime = path.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            return None
        with self.lock:
            entry = self.data.get("sizes", {}).get(udid)
            if not entry:
                return None
            if entry.get("path") != str(path):
                return None
            cached_mtime = entry.get("mtime")
            if cached_mtime != mtime:
                return None
            size_bytes = entry.get("size_bytes")
            log_bytes = entry.get("log_bytes")
        return (size_bytes, log_bytes) if size_bytes is not None else None

    def set_cached_size(
        self,
        udid: str,
        path: Path,
        size_bytes: Optional[int],
        log_bytes: Optional[int],
    ) -> None:
        try:
            mtime = path.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            mtime = None
        with self.lock:
            self.data.setdefault("sizes", {})[udid] = {
                "path": str(path),
                "mtime": mtime,
                "size_bytes": size_bytes,
                "log_bytes": log_bytes,
                "cached_at": time.time(),
            }
            self._save_locked()

    def remove_cached_size(self, udid: str) -> None:
        with self.lock:
            if udid in self.data.get("sizes", {}):
                del self.data["sizes"][udid]
                self._save_locked()

    def get_cached_detail(self, udid: str, path: Path) -> Optional[List[str]]:
        try:
            mtime = path.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            return None
        now = time.time()
        with self.lock:
            entry = self.data.get("details", {}).get(udid)
            if not entry:
                return None
            if entry.get("path") != str(path):
                return None
            cached_mtime = entry.get("mtime")
            if cached_mtime != mtime:
                return None
            cached_at = entry.get("cached_at", 0)
            if now - cached_at > DETAIL_CACHE_TTL_SECONDS:
                return None
            lines = entry.get("lines")
            if not isinstance(lines, list):
                return None
            return list(lines)

    def set_cached_detail(self, udid: str, path: Path, lines: Sequence[str]) -> None:
        try:
            mtime = path.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            mtime = None
        with self.lock:
            self.data.setdefault("details", {})[udid] = {
                "path": str(path),
                "mtime": mtime,
                "lines": list(lines),
                "cached_at": time.time(),
            }
            self._save_locked()

    def remove_cached_detail(self, udid: str) -> None:
        with self.lock:
            if udid in self.data.get("details", {}):
                del self.data["details"][udid]
                self._save_locked()

@dataclass
class RestartSizeJobEvent:
    udid: str


def format_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "n/a"
    if num_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} EB"


def format_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def runtime_display(runtime_id: Optional[str]) -> str:
    if not runtime_id:
        return "unknown runtime"
    prefix = "com.apple.CoreSimulator.SimRuntime."
    if runtime_id.startswith(prefix):
        payload = runtime_id[len(prefix) :]
        parts = payload.split("-")
        if len(parts) >= 2:
            platform = parts[0]
            version = ".".join(part for part in parts[1:] if part)
            return f"{platform} {version}"
    return runtime_id


def safe_load_plist(path: Path) -> Optional[dict]:
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:  # pragma: no cover - defensive
        print(f"warning: failed to parse plist {path}: {exc}", file=sys.stderr)
        return None


def safe_dir_size(path: Path) -> Optional[int]:
    if not path.exists():
        LOGGER.debug("Skipping size measurement for %s (missing)", path)
        return None
    LOGGER.debug("Measuring size for %s", path)
    try:
        with log_timing("du", path=str(path)):
            result = subprocess.run(
                ["du", "-sk", str(path)],
                capture_output=True,
                text=True,
                check=True,
            )
    except FileNotFoundError:
        print("error: 'du' command not available", file=sys.stderr)
        LOGGER.debug("'du' command not available while measuring %s", path)
        return None
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"warning: unable to measure size for {path}: {detail}", file=sys.stderr)
        LOGGER.debug("du failed for %s: %s", path, detail)
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        LOGGER.debug("du produced no output for %s", path)
        return None
    first_line = output[0]
    try:
        size_kb = int(first_line.split()[0])
        size_bytes = size_kb * 1024
        LOGGER.debug("Measured %s -> %s KB", path, size_kb)
        return size_bytes
    except (IndexError, ValueError):
        print(f"warning: unexpected du output for {path}: {first_line}", file=sys.stderr)
        LOGGER.debug("Unexpected du output for %s: %s", path, first_line)
        return None


def walk_directory_size(
    path: Path,
    progress_callback: Optional[Callable[[int], None]] = None,
    report_interval: float = 0.25,
) -> int:
    if not path.exists():
        LOGGER.debug("walk_directory_size skipping missing path %s", path)
        if progress_callback:
            progress_callback(0)
        return 0

    total = 0
    last_report = time.perf_counter()
    stack = [path]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        LOGGER.debug("walk_directory_size lost entry %s", entry.path)
                        continue
                    except PermissionError as exc:
                        LOGGER.debug("walk_directory_size permission denied for %s: %s", entry.path, exc)
                        continue
                    except OSError as exc:
                        LOGGER.debug("walk_directory_size error statting %s: %s", entry.path, exc)
                        continue

                    if progress_callback and (time.perf_counter() - last_report) >= report_interval:
                        progress_callback(total)
                        last_report = time.perf_counter()
        except FileNotFoundError:
            LOGGER.debug("walk_directory_size directory vanished %s", current)
            continue
        except PermissionError as exc:
            LOGGER.debug("walk_directory_size permission denied for %s: %s", current, exc)
            continue

    if progress_callback:
        progress_callback(total)
    return total


def _simctl_state_map() -> Dict[str, str]:
    """Return {udid: state} from `xcrun simctl list devices -j`.

    The per-device ``device.plist`` is not a reliable source of truth for the
    boot state: CoreSimulator does not always rewrite it on shutdown, so stale
    plists frequently claim ``state=1`` (Booted) for devices that simctl
    correctly reports as Shutdown. Querying simctl avoids that stale value.
    """
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "-j"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        LOGGER.debug("simctl list devices failed: %s", exc)
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        LOGGER.debug("simctl list devices produced invalid JSON: %s", exc)
        return {}
    states: Dict[str, str] = {}
    for runtime_devices in (payload.get("devices") or {}).values():
        for entry in runtime_devices or []:
            udid = entry.get("udid")
            state = entry.get("state")
            if udid and state:
                states[udid] = state
    return states


def discover_devices(
    include_sizes: bool = True,
    include_logs: bool = True,
) -> List[DeviceInfo]:
    devices: List[DeviceInfo] = []
    if not DEVICE_ROOT.exists():
        LOGGER.debug("Device root %s does not exist", DEVICE_ROOT)
        return devices

    simctl_states = _simctl_state_map()

    LOGGER.debug("Discovering simulators under %s", DEVICE_ROOT)
    with log_timing("discover_devices", root=str(DEVICE_ROOT)):
        for entry in sorted(DEVICE_ROOT.iterdir()):
            if not entry.is_dir():
                continue
            LOGGER.debug("Inspecting simulator directory %s", entry)
            plist_path = entry / "device.plist"
            plist = safe_load_plist(plist_path)
            if plist is None:
                LOGGER.debug("Skipping %s: missing or invalid device.plist", entry)
                continue

            last_booted = plist.get("lastBootedAt")
            info = DeviceInfo(
                udid=entry.name,
                name=plist.get("name") or "Unknown",
                runtime=runtime_display(plist.get("runtime")),
                raw_runtime=plist.get("runtime") or "",
                device_type=plist.get("deviceType") or "",
                state=simctl_states.get(
                    entry.name,
                    STATE_MAP.get(plist.get("state"), str(plist.get("state"))),
                ),
                last_booted_at=format_datetime(last_booted),
                path=entry,
                size_bytes=None,
                log_bytes=None,
                is_deleted=bool(plist.get("isDeleted")),
                is_ephemeral=bool(plist.get("isEphemeral")),
            )
            if include_sizes:
                info.size_bytes = safe_dir_size(entry)
            if include_logs:
                info.log_bytes = safe_dir_size(LOG_ROOT / entry.name)
            LOGGER.debug(
                "Collected simulator %s size=%s log=%s",
                info.udid,
                info.size_bytes,
                info.log_bytes,
            )
            devices.append(info)

    devices.sort(key=lambda item: (item.size_bytes or 0), reverse=True)
    LOGGER.debug("Discovered %d simulators", len(devices))
    return devices


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def format_row(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(cells))

    lines = [format_row(headers)]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append(format_row(row))
    return "\n".join(lines)


def ensure_device(args_udid: str, devices: Sequence[DeviceInfo]) -> DeviceInfo:
    for device in devices:
        if device.udid == args_udid:
            return device
    print(f"error: simulator {args_udid} not found", file=sys.stderr)
    sys.exit(1)


def collect_device_breakdown(device: DeviceInfo) -> List[Tuple[str, Path, Optional[int]]]:
    base = device.path / "data"
    breakdown = [
        ("Containers/Data", base / "Containers" / "Data"),
        ("Containers/Bundle", base / "Containers" / "Bundle"),
        ("Containers/Shared", base / "Containers" / "Shared"),
        ("Library", base / "Library"),
        ("Media", base / "Media"),
        ("private", base / "private"),
        ("var", base / "var"),
        ("tmp", base / "tmp"),
        ("Downloads", base / "Downloads"),
        ("Documents", base / "Documents"),
    ]
    results: List[Tuple[str, Path, Optional[int]]] = []
    LOGGER.debug("Collecting device breakdown for %s", device.udid)
    with log_timing("collect_device_breakdown", udid=device.udid):
        for label, path in breakdown:
            if not path.exists():
                continue
            size = safe_dir_size(path)
            LOGGER.debug("Breakdown entry %s -> %s bytes", path, size)
            results.append((label, path, size))
    LOGGER.debug("Collected %d breakdown entries for %s", len(results), device.udid)
    return sorted(results, key=lambda item: (item[2] or 0), reverse=True)


def collect_top_app_containers(device: DeviceInfo, limit: int = 10) -> List[Tuple[str, Path, Optional[int]]]:
    data_root = device.path / "data" / "Containers" / "Data" / "Application"
    if not data_root.exists():
        LOGGER.debug("App containers root %s missing for %s", data_root, device.udid)
        return []
    rows: List[Tuple[str, Path, Optional[int]]] = []
    LOGGER.debug("Collecting top app containers for %s (limit=%s)", device.udid, limit)
    with log_timing("collect_top_app_containers", udid=device.udid, limit=limit):
        for entry in data_root.iterdir():
            if not entry.is_dir():
                continue
            meta = safe_load_plist(entry / ".com.apple.mobile_container_manager.metadata.plist")
            identifier = meta.get("MCMMetadataIdentifier") if meta else None
            label = identifier or entry.name
            size = safe_dir_size(entry)
            LOGGER.debug("App container %s -> %s bytes", entry, size)
            rows.append((label, entry, size))

    rows.sort(key=lambda item: (item[2] or 0), reverse=True)
    LOGGER.debug("Top app containers collected for %s: %d entries", device.udid, len(rows))
    return rows[:limit]


def clear_directory_contents(path: Path, dry_run: bool) -> int:
    if not path.exists():
        LOGGER.debug("Skipping clear_directory_contents for %s (missing)", path)
        return 0
    total = 0
    LOGGER.debug("Clearing contents of %s (dry_run=%s)", path, dry_run)
    with log_timing("clear_directory_contents", path=str(path), dry_run=dry_run):
        for entry in path.iterdir():
            entry_size = safe_dir_size(entry) if entry.is_dir() else entry.stat().st_size
            total += entry_size or 0
            if dry_run:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except FileNotFoundError:
                    continue
    LOGGER.debug("clear_directory_contents reclaimed %s bytes from %s", total, path)
    return total


def remove_path(path: Path, dry_run: bool) -> Optional[int]:
    if not path.exists():
        LOGGER.debug("Skipping remove_path for %s (missing)", path)
        return None
    size = safe_dir_size(path)
    if dry_run:
        LOGGER.debug("Dry run: would remove %s (%s bytes)", path, size)
        return size
    LOGGER.debug("Removing path %s", path)
    with log_timing("remove_path", path=str(path)):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    LOGGER.debug("Removed %s (%s bytes)", path, size)
    return size


def prompt_yes_no(message: str) -> bool:
    try:
        reply = input(f"{message} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return reply in {"y", "yes"}


def human_targets(targets: Sequence[str]) -> str:
    return ", ".join(sorted(targets)) if targets else "nothing"


def collect_scan_results(
    min_size_mb: Optional[int],
    top: Optional[int],
) -> Tuple[List[DeviceInfo], List[Dict[str, Any]]]:
    LOGGER.debug("collect_scan_results(min_size_mb=%s, top=%s) invoked", min_size_mb, top)
    with log_timing("collect_scan_results", min_size_mb=min_size_mb, top=top):
        devices = discover_devices()
        filtered: List[DeviceInfo] = []
        min_bytes = int(min_size_mb * 1024 * 1024) if min_size_mb else 0
        for device in devices:
            size = device.size_bytes or 0
            if size < min_bytes:
                continue
            filtered.append(device)
        if top:
            filtered = filtered[:top]
    LOGGER.debug(
        "collect_scan_results returning %d devices (min_size_mb=%s, top=%s)",
        len(filtered),
        min_size_mb,
        top,
    )
    globals_summary = collect_globals_summary()
    LOGGER.debug("collect_scan_results gathered %d global entries", len(globals_summary))
    return filtered, globals_summary


def format_scan_report(devices: Sequence[DeviceInfo], globals_summary: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    rows = [
        [
            format_size(device.size_bytes),
            device.udid,
            device.name,
            device.runtime,
            device.state,
            device.last_booted_at or "-",
        ]
        for device in devices
    ]
    if rows:
        lines.append(render_table(["Size", "UDID", "Name", "Runtime", "State", "Last Boot"], rows))
    else:
        lines.append("No simulators matched the filter.")

    globals_rows = [
        [item["category"], format_size(item.get("bytes")), item["identifier"], item["path"]]
        for item in globals_summary
    ]
    lines.append("")
    if globals_rows:
        lines.append(render_table(["Type", "Size", "Identifier", "Path"], globals_rows))
    else:
        lines.append("No global simulator resources found.")
    return "\n".join(lines).strip()




def capture_command_output(func: Callable[[argparse.Namespace], None], args: argparse.Namespace) -> str:
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            func(args)
    except SystemExit as exc:
        text = buffer.getvalue().strip()
        suffix = f"Command exited with status {exc.code}."
        return f"{text}\\n{suffix}".strip()
    except Exception as exc:  # pragma: no cover - defensive
        text = buffer.getvalue().strip()
        suffix = f"Error: {exc}"
        return f"{text}\\n{suffix}".strip()
    return buffer.getvalue().strip()


class SimulatorTui:
    def __init__(self, stdscr: "curses._CursesWindow", cache: CacheManager) -> None:
        self.stdscr = stdscr
        self.cache = cache
        self.rows: List[DeviceRow] = []
        self.row_lookup: Dict[str, DeviceRow] = {}
        self.event_queue: "queue.Queue[object]" = queue.Queue()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(2, min(8, (os.cpu_count() or 4)))
        )
        self.submitted_jobs: set[str] = set()
        self.selected_index = 0
        self.scroll_offset = 0
        self.status_message = ""
        self.status_expires_at = float("inf")
        self.running = True
        self._needs_draw = True
        self.page_size = 1
        self.activity_lines: List[str] = []
        self.detail_overlay_visible = False
        self.detail_overlay_title = ""
        self.detail_overlay_lines: List[str] = []
        self.sort_mode = self.cache.get_sort_mode()
        self.poll_timeout_ms = 150
        self.pending_tasks = 0
        self.pending_lock = threading.Lock()
        self.exit_requested = False
        self.multi_select_mode = False
        self.selected_udids: set[str] = set()
        self.colors_enabled = False
        self.size_color_small = 0
        self.size_color_medium = 0
        self.size_color_large = 0

    # Size color thresholds (bytes). Below small = green, between = yellow,
    # at or above large = red. Tuned for typical simulator sizes.
    SIZE_THRESHOLD_SMALL = 500 * 1024 * 1024  # 500 MB
    SIZE_THRESHOLD_LARGE = 2 * 1024 * 1024 * 1024  # 2 GB

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_YELLOW, bg)
            curses.init_pair(3, curses.COLOR_RED, bg)
        except curses.error:
            return
        self.size_color_small = curses.color_pair(1)
        self.size_color_medium = curses.color_pair(2)
        self.size_color_large = curses.color_pair(3)
        self.colors_enabled = True

    def _size_attr(self, size_bytes: Optional[int]) -> int:
        if not self.colors_enabled or size_bytes is None:
            return 0
        if size_bytes >= self.SIZE_THRESHOLD_LARGE:
            return self.size_color_large
        if size_bytes >= self.SIZE_THRESHOLD_SMALL:
            return self.size_color_medium
        return self.size_color_small

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()
        self.stdscr.timeout(self.poll_timeout_ms)
        self.refresh_devices(initial=True)
        while self.running:
            self._process_events()
            self._maybe_clear_status()
            if self.exit_requested and self._pending_task_count() == 0:
                self.running = False
                break
            if self._needs_draw:
                self._draw()
                self._needs_draw = False
            key = self.stdscr.getch()
            if key == -1:
                continue
            self._handle_key(key)
        self.executor.shutdown(wait=False)

    def refresh_devices(self, initial: bool = False, retain_udid: Optional[str] = None) -> None:
        previous_udid = retain_udid
        if previous_udid is None and self.rows:
            previous_udid = self.rows[self.selected_index].info.udid

        devices = discover_devices(include_sizes=False, include_logs=False)
        devices.sort(key=lambda d: (d.name.lower(), d.udid))

        self.rows = [DeviceRow(info=device) for device in devices]
        self.row_lookup = {row.info.udid: row for row in self.rows}
        self.selected_udids &= set(self.row_lookup.keys())
        self._apply_cached_sizes()
        self._sort_rows()
        self.activity_lines = []
        self.scroll_offset = 0
        self.submitted_jobs.clear()

        if self.rows:
            if previous_udid and previous_udid in self.row_lookup:
                self.selected_index = next(
                    idx for idx, row in enumerate(self.rows) if row.info.udid == previous_udid
                )
            else:
                self.selected_index = min(self.selected_index, len(self.rows) - 1)
            self._schedule_size_jobs()
        else:
            self.selected_index = 0

        if not initial:
            self._status(f"Discovered {len(self.rows)} simulators.", duration=3.0)
        self._needs_draw = True

    def _apply_cached_sizes(self) -> None:
        for row in self.rows:
            cached = self.cache.get_cached_size(row.info.udid, row.info.path)
            if not cached:
                continue
            size_bytes, log_bytes = cached
            row.size_status = SizeStatus.DONE
            row.size_bytes = size_bytes
            row.log_bytes = log_bytes
            row.progress_bytes = None

    def _schedule_size_jobs(self) -> None:
        for row in self.rows:
            if row.size_status == SizeStatus.DONE:
                continue
            self._submit_size_job(row)

    def _submit_size_job(self, row: DeviceRow) -> None:
        udid = row.info.udid
        if udid in self.submitted_jobs:
            return
        self.submitted_jobs.add(udid)
        self._submit_task(lambda: self._size_worker(row.info))

    def _restart_size_job(self, udid: str) -> None:
        row = self.row_lookup.get(udid)
        if not row:
            return
        row.size_status = SizeStatus.PENDING
        row.size_bytes = None
        row.log_bytes = None
        row.progress_bytes = None
        row.error = None
        self.submitted_jobs.discard(udid)
        self._submit_size_job(row)
        self._needs_draw = True

    def _size_worker(self, info: DeviceInfo) -> None:
        LOGGER.debug("Starting background size job for %s", info.udid)

        def progress(bytes_done: int) -> None:
            self.event_queue.put(SizeProgressEvent(info.udid, bytes_done))

        try:
            total = walk_directory_size(info.path, progress_callback=progress)
            log_path = LOG_ROOT / info.udid
            log_bytes = walk_directory_size(log_path) if log_path.exists() else 0
            self.event_queue.put(SizeCompleteEvent(info.udid, total, log_bytes))
            LOGGER.debug("Completed size job for %s (%s bytes)", info.udid, total)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("Size job failed for %s: %s", info.udid, exc, exc_info=True)
            self.event_queue.put(SizeErrorEvent(info.udid, str(exc)))

    def _process_events(self) -> None:
        processed = False
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            processed = True
            if isinstance(event, SizeProgressEvent):
                row = self.row_lookup.get(event.udid)
                if row and row.size_status != SizeStatus.DONE:
                    row.size_status = SizeStatus.RUNNING
                    row.progress_bytes = event.bytes_done
            elif isinstance(event, SizeCompleteEvent):
                self.submitted_jobs.discard(event.udid)
                row = self.row_lookup.get(event.udid)
                if row:
                    row.size_status = SizeStatus.DONE
                    row.size_bytes = event.total_bytes
                    row.log_bytes = event.log_bytes
                    row.progress_bytes = None
                    self.cache.set_cached_size(row.info.udid, row.info.path, row.size_bytes, row.log_bytes)
                    if self.sort_mode == "size":
                        self._resort_preserving_selection()
            elif isinstance(event, SizeErrorEvent):
                self.submitted_jobs.discard(event.udid)
                row = self.row_lookup.get(event.udid)
                if row:
                    row.size_status = SizeStatus.ERROR
                    row.error = event.message
                    row.progress_bytes = None
            elif isinstance(event, RowMessageEvent):
                row = self.row_lookup.get(event.udid)
                if row:
                    row.row_message = event.message
            elif isinstance(event, StatusMessageEvent):
                self._status(event.message, duration=event.duration)
            elif isinstance(event, DetailOutputEvent):
                if event.overlay:
                    self._set_detail_overlay_lines(event.lines)
                else:
                    self.activity_lines = list(event.lines)
                    self._needs_draw = True
            elif isinstance(event, ReloadDevicesEvent):
                self.refresh_devices(initial=False, retain_udid=event.selected_udid)
            elif isinstance(event, RestartSizeJobEvent):
                self._restart_size_job(event.udid)
        if processed:
            self._needs_draw = True

    def _draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if self.detail_overlay_visible:
            self._draw_detail_overlay(height, width)
            return

        title = "iOS Simulator Cleanup"
        count_suffix = f"  •  {len(self.rows)} simulator{'s' if len(self.rows) != 1 else ''}"
        total_len = len(title) + len(count_suffix)
        title_x = max(0, (width - total_len) // 2)
        self._safe_addnstr(0, title_x, title, width - title_x, curses.A_BOLD)
        self._safe_addnstr(0, title_x + len(title), count_suffix, width - title_x - len(title), curses.A_DIM)

        # Bottom reserves: divider + hint line 1 + hint line 2 + status = 4 rows
        list_top = 1
        list_height = max(4, height - 10)
        data_rows = max(1, list_height - 1)
        self.page_size = data_rows
        self._ensure_selection_visible(data_rows)

        mark_width = 2
        size_width = 22
        runtime_width = 18
        state_width = 10
        last_boot_width = 18
        name_width = max(
            10,
            width - (mark_width + size_width + runtime_width + state_width + last_boot_width + 12),
        )

        header = (
            f"{'':<{mark_width}}  "
            f"{'Size':>{size_width}}  "
            f"{'Name':<{name_width}}  "
            f"{'Runtime':<{runtime_width}}  "
            f"{'State':<{state_width}}  "
            f"{'Last Boot':<{last_boot_width}}"
        )
        self._safe_addnstr(list_top, 2, header[: width - 4], width - 4, curses.A_BOLD)

        visible_rows = self.rows[self.scroll_offset : self.scroll_offset + data_rows]
        for idx, row in enumerate(visible_rows):
            y = list_top + 1 + idx
            attr = curses.A_REVERSE if (self.scroll_offset + idx) == self.selected_index else curses.A_NORMAL
            marked = row.info.udid in self.selected_udids
            mark_cell = (" *" if marked else "  ").ljust(mark_width)
            size_cell = self._truncate(row.size_label(), size_width).rjust(size_width)
            name_cell = self._truncate(row.info.name, name_width).ljust(name_width)
            runtime_cell = self._truncate(row.info.runtime, runtime_width).ljust(runtime_width)
            state_cell = self._truncate(row.info.state, state_width).ljust(state_width)
            last_boot = row.info.last_booted_at or "-"
            last_boot_cell = self._truncate(last_boot, last_boot_width).ljust(last_boot_width)
            size_attr = attr | self._size_attr(
                row.size_bytes if row.size_status == SizeStatus.DONE else None
            )
            mark_attr = attr | (curses.A_BOLD if marked else 0)
            if self.colors_enabled and marked:
                mark_attr |= self.size_color_large
            self._safe_addnstr(y, 2, mark_cell, width - 4, mark_attr)
            size_x = 2 + mark_width + 2
            self._safe_addnstr(y, size_x, size_cell, max(0, width - 4 - (mark_width + 2)), size_attr)
            rest = f"  {name_cell}  {runtime_cell}  {state_cell}  {last_boot_cell}"
            rest_x = size_x + size_width
            remaining = max(0, width - 2 - rest_x)
            if remaining:
                self._safe_addnstr(y, rest_x, rest[:remaining], remaining, attr)

        divider_y = list_top + data_rows + 1
        self._safe_addnstr(divider_y, 1, "─" * (width - 2), width - 2, curses.A_DIM)

        detail_top = divider_y + 1
        # Footer reserves 4 rows: divider + hint1 + hint2 + status.
        available_detail = max(1, height - detail_top - 4)
        for idx, line in enumerate(self._detail_lines()[:available_detail]):
            self._safe_addnstr(detail_top + idx, 2, line[: width - 4], width - 4)

        if self.selected_udids:
            sel_total = self._selected_total_bytes()
            sel_summary = (
                f"{len(self.selected_udids)} marked • {format_size(sel_total)} — press Enter/d to delete"
            )
        else:
            sel_summary = ""
        if self.status_message:
            status_text = self.status_message
        elif sel_summary:
            status_text = sel_summary
        else:
            status_text = ""

        self._draw_footer(height, width, status_text)
        self.stdscr.refresh()

    def _draw_footer(self, height: int, width: int, status_text: str) -> None:
        """Render the two-line bracketed help footer plus a status line.

        Layout (bottom-up): status line, hint line 2, hint line 1, divider.
        """
        if self.multi_select_mode:
            line1 = [
                ("↑↓", "navigate"),
                ("Space", "mark"),
                ("a", "mark outdated"),
                ("X", "clear"),
            ]
            line2 = [
                ("Enter/d", "delete marked"),
                ("Esc/q", "exit multi-select"),
            ]
        else:
            line1 = [
                ("↑↓", "navigate"),
                ("Enter", "detail"),
                ("m", "multi-select"),
                ("b", "boot"),
                ("o", "open"),
            ]
            line2 = [
                ("c", "clean"),
                ("d", "delete"),
                ("s", "sort"),
                ("r", "refresh"),
                ("q", "quit"),
            ]

        divider_y = height - 4
        hint1_y = height - 3
        hint2_y = height - 2
        status_y = height - 1

        self._safe_addnstr(divider_y, 1, "─" * (width - 2), width - 2, curses.A_DIM)
        self._draw_key_hints(hint1_y, width, line1)
        self._draw_key_hints(hint2_y, width, line2)
        self._safe_addnstr(status_y, 2, status_text[: width - 4], width - 4, curses.A_BOLD)

    def _draw_key_hints(
        self, y: int, width: int, items: Sequence[Tuple[str, str]]
    ) -> None:
        x = 2
        right_edge = width - 2
        first = True
        for key, label in items:
            sep = "" if first else "   "
            first = False
            if x + len(sep) >= right_edge:
                return
            if sep:
                self._safe_addnstr(y, x, sep, right_edge - x, curses.A_DIM)
                x += len(sep)
            bracket_left = "["
            bracket_right = "]"
            chunk = f"{bracket_left}{key}{bracket_right} {label}"
            if x + len(chunk) > right_edge:
                # Write what fits dimmed, then stop.
                self._safe_addnstr(y, x, chunk, right_edge - x, curses.A_DIM)
                return
            self._safe_addnstr(y, x, bracket_left, right_edge - x, curses.A_DIM)
            x += 1
            self._safe_addnstr(y, x, key, right_edge - x, curses.A_BOLD)
            x += len(key)
            self._safe_addnstr(y, x, bracket_right, right_edge - x, curses.A_DIM)
            x += 1
            self._safe_addnstr(y, x, " ", right_edge - x)
            x += 1
            self._safe_addnstr(y, x, label, right_edge - x)
            x += len(label)

    def _draw_detail_overlay(self, height: int, width: int) -> None:
        title = self.detail_overlay_title or "Simulator Detail"
        self._safe_addstr(0, max(0, (width - len(title)) // 2), title, curses.A_BOLD)
        max_lines = max(1, height - 3)
        for idx, line in enumerate(self.detail_overlay_lines[:max_lines]):
            self._safe_addnstr(1 + idx, 2, line[: width - 4], width - 4)
        footer = "Press Esc or q to close detail view."
        self._safe_addnstr(height - 2, 2, footer[: width - 4], width - 4, curses.A_DIM)
        self._safe_addnstr(height - 1, 2, "", width - 4)
        self.stdscr.refresh()

    def _detail_lines(self) -> List[str]:
        if not self.rows:
            return ["No simulators detected."]
        row = self.rows[self.selected_index]
        info = row.info
        lines = [
            f"{info.name} ({info.udid})",
            f"Runtime: {info.runtime}",
            f"State: {info.state} | Last boot: {info.last_booted_at or '-'}",
            f"Path: {info.path}",
        ]
        if row.size_status == SizeStatus.DONE and row.size_bytes is not None:
            size_line = f"Size: {format_size(row.size_bytes)}"
            if row.log_bytes:
                size_line += f" | Logs: {format_size(row.log_bytes)}"
            lines.append(size_line)
        elif row.size_status == SizeStatus.ERROR:
            lines.append(f"Size failed: {row.error or 'unknown error'}")
        else:
            lines.append(f"Size status: {row.size_label()}")

        if self.activity_lines:
            lines.append("")
            lines.extend(self.activity_lines)
        return lines

    def _handle_key(self, key: int) -> None:
        if self.detail_overlay_visible:
            self._handle_detail_overlay_key(key)
            return
        if self.multi_select_mode:
            self._handle_multi_select_key(key)
            return
        if key in (curses.KEY_UP, ord("k")):
            self._move_selection(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._move_selection(1)
        elif key in (curses.KEY_PPAGE,):
            self._move_selection(-self.page_size)
        elif key in (curses.KEY_NPAGE,):
            self._move_selection(self.page_size)
        elif key in (ord("g"),):
            self._set_selection(0)
        elif key in (ord("G"),):
            self._set_selection(len(self.rows) - 1)
        elif key in (ord("r"), ord("R")):
            self.refresh_devices()
        elif key in (ord("s"), ord("S")):
            self._toggle_sort_mode()
        elif key in (ord("b"), ord("B")):
            self._boot_selected()
        elif key in (ord("o"), ord("O")):
            self._open_selected_in_finder()
        elif key in (ord("c"), ord("C")):
            self._clean_selected()
        elif key in (ord("m"), ord("M")):
            self._enter_multi_select_mode()
        elif key in (ord("d"), ord("D")):
            self._delete_selected()
        elif key in (curses.KEY_ENTER, 10, 13):
            self._show_detail_report()
        elif key in (ord("q"), ord("Q"), 27):
            self._request_exit()
        self._needs_draw = True

    def _handle_detail_overlay_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q"), 27, curses.KEY_ENTER, 10, 13):
            self._close_detail_overlay()
        self._needs_draw = True

    def _move_selection(self, delta: int) -> None:
        if not self.rows:
            return
        self.selected_index = max(0, min(len(self.rows) - 1, self.selected_index + delta))
        self._ensure_selection_visible(self.page_size)
        self._clear_detail_output()

    def _set_selection(self, index: int) -> None:
        if not self.rows:
            return
        self.selected_index = max(0, min(len(self.rows) - 1, index))
        self._ensure_selection_visible(self.page_size)
        self._clear_detail_output()

    def _ensure_selection_visible(self, capacity: int) -> None:
        if not self.rows:
            self.scroll_offset = 0
            return
        capacity = max(1, capacity)
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + capacity:
            self.scroll_offset = self.selected_index - capacity + 1
        max_offset = max(0, len(self.rows) - capacity)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    def _sort_rows(self) -> None:
        if not self.rows:
            return
        if self.sort_mode == "size":
            def size_key(row: DeviceRow) -> Tuple[int, str, str]:
                size = row.size_bytes if row.size_bytes is not None else -1
                return (size, row.info.name.lower(), row.info.udid)

            self.rows.sort(key=size_key, reverse=True)
        else:
            self.rows.sort(key=lambda row: (row.info.name.lower(), row.info.udid))
        self.row_lookup = {row.info.udid: row for row in self.rows}

    def _resort_preserving_selection(self) -> None:
        selected = self._selected_row()
        selected_udid = selected.info.udid if selected else None
        self._sort_rows()
        if selected_udid:
            for idx, row in enumerate(self.rows):
                if row.info.udid == selected_udid:
                    self.selected_index = idx
                    break
        self._ensure_selection_visible(self.page_size)
        self._needs_draw = True

    def _toggle_sort_mode(self) -> None:
        self.sort_mode = "size" if self.sort_mode != "size" else "default"
        self.cache.set_sort_mode(self.sort_mode)
        self._resort_preserving_selection()
        mode_label = "size" if self.sort_mode == "size" else "name"
        self._status(f"Sorted by {mode_label}.", duration=3.0)

    def _open_detail_overlay(self, title: str, lines: Sequence[str]) -> None:
        self.detail_overlay_title = title
        self.detail_overlay_visible = True
        self.detail_overlay_lines = list(lines)
        self._needs_draw = True

    def _set_detail_overlay_lines(self, lines: Sequence[str]) -> None:
        if not self.detail_overlay_visible:
            return
        self.detail_overlay_lines = list(lines)
        self._needs_draw = True

    def _close_detail_overlay(self) -> None:
        self.detail_overlay_visible = False
        self.detail_overlay_lines = []
        self.detail_overlay_title = ""
        self._needs_draw = True

    def _request_exit(self) -> None:
        if self.exit_requested:
            return
        self.exit_requested = True
        self._status("Exiting… finishing background tasks.", duration=None)
        self._needs_draw = True

    def _pending_task_count(self) -> int:
        with self.pending_lock:
            return self.pending_tasks

    def _submit_task(self, func: Callable[[], None]) -> None:
        with self.pending_lock:
            self.pending_tasks += 1

        def wrapped() -> None:
            try:
                func()
            finally:
                with self.pending_lock:
                    self.pending_tasks -= 1

        self.executor.submit(wrapped)

    def _status(self, message: str, duration: Optional[float] = 4.0) -> None:
        self.status_message = message
        if duration is None:
            self.status_expires_at = float("inf")
        else:
            self.status_expires_at = time.perf_counter() + duration
        self._needs_draw = True

    def _maybe_clear_status(self) -> None:
        if self.status_message and self.status_expires_at != float("inf"):
            if time.perf_counter() >= self.status_expires_at:
                self.status_message = ""
                self._needs_draw = True

    def _selected_row(self) -> Optional[DeviceRow]:
        if not self.rows:
            return None
        return self.rows[self.selected_index]

    def _clear_detail_output(self) -> None:
        if self.activity_lines:
            self.activity_lines = []
            self._needs_draw = True

    def _prompt(self, message: str) -> Optional[str]:
        height, width = self.stdscr.getmaxyx()
        # Paint a highly visible prompt bar across the last 3 rows,
        # overwriting the key-hints footer for the duration of the prompt.
        bar_top = height - 3
        prompt_line = height - 2
        bar_bottom = height - 1
        prompt_text = f"  > {message.strip()} "
        blank = " " * max(0, width - 1)
        accent = curses.A_REVERSE | curses.A_BOLD
        for y in (bar_top, prompt_line, bar_bottom):
            self._safe_addnstr(y, 0, blank, width - 1, accent)
        self._safe_addnstr(prompt_line, 0, prompt_text, width - 1, accent)
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        start_x = min(width - 3, len(prompt_text))
        self.stdscr.move(prompt_line, start_x)
        self.stdscr.clrtoeol()
        self.stdscr.refresh()
        previous_timeout = self.poll_timeout_ms
        self.stdscr.timeout(-1)
        try:
            raw = self.stdscr.getstr(prompt_line, start_x, max(1, width - start_x - 2))
        except curses.error:
            raw = None
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.stdscr.timeout(previous_timeout)
        self._needs_draw = True
        if raw is None:
            return None
        return raw.decode(errors="ignore").strip()

    def _prompt_yes_no(self, message: str, default: bool = False) -> Optional[bool]:
        suffix = "Y/n" if default else "y/N"
        reply = self._prompt(f"{message} ({suffix}):")
        if reply is None:
            return None
        if not reply:
            return default
        value = reply.lower()
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        self._status("Please answer yes or no.", duration=3.0)
        return None

    def _run_cli_command(
        self,
        description: str,
        func: Callable[[argparse.Namespace], None],
        namespace: argparse.Namespace,
        *,
        refresh_devices: bool = False,
        retain_udid: Optional[str] = None,
        refresh_size_udids: Optional[Sequence[str]] = None,
        detail_device: Optional[DeviceInfo] = None,
        display_overlay: bool = False,
    ) -> None:
        self._status(description, duration=None)

        def job() -> None:
            try:
                output = capture_command_output(func, namespace)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("CLI command failed: %s", exc, exc_info=True)
                self.event_queue.put(DetailOutputEvent([f"{description} failed: {exc}"], overlay=display_overlay))
                self.event_queue.put(StatusMessageEvent(f"{description} failed: {exc}", duration=6.0))
                return

            lines = output.splitlines() or ["(no output)"]
            self.event_queue.put(DetailOutputEvent(lines, overlay=display_overlay))
            summary = lines[-1] if lines else f"{description} complete."
            self.event_queue.put(StatusMessageEvent(summary, duration=6.0))
            if detail_device and lines:
                self.cache.set_cached_detail(detail_device.udid, detail_device.path, lines)
            if refresh_devices:
                self.event_queue.put(ReloadDevicesEvent(selected_udid=retain_udid))
            if refresh_size_udids:
                for udid in refresh_size_udids:
                    self.event_queue.put(RestartSizeJobEvent(udid))

        self._submit_task(job)

    def _run_subprocess(
        self,
        description: str,
        command: Sequence[str],
        *,
        refresh_devices: bool = False,
        retain_udid: Optional[str] = None,
        refresh_size_udids: Optional[Sequence[str]] = None,
    ) -> None:
        self._status(description, duration=None)

        def job() -> None:
            try:
                result = subprocess.run(command, capture_output=True, text=True)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("Subprocess %s failed: %s", command, exc, exc_info=True)
                self.event_queue.put(DetailOutputEvent([f"{description} failed: {exc}"]))
                self.event_queue.put(StatusMessageEvent(f"{description} failed: {exc}", duration=6.0))
                return

            combined = "\\n".join(
                part.strip() for part in [result.stdout or "", result.stderr or ""] if part
            )
            lines = combined.splitlines()
            if lines:
                self.event_queue.put(DetailOutputEvent(lines))

            if result.returncode == 0:
                summary = lines[-1] if lines else f"{description} succeeded."
                self.event_queue.put(StatusMessageEvent(summary, duration=6.0))
                if refresh_devices:
                    self.event_queue.put(ReloadDevicesEvent(selected_udid=retain_udid))
                if refresh_size_udids:
                    for udid in refresh_size_udids:
                        self.event_queue.put(RestartSizeJobEvent(udid))
            else:
                error_text = lines[-1] if lines else f"exit status {result.returncode}"
                self.event_queue.put(StatusMessageEvent(f"{description} failed: {error_text}", duration=6.0))

        self._submit_task(job)

    def _show_detail_report(self) -> None:
        row = self._selected_row()
        if not row:
            self._status("No simulator selected.", duration=3.0)
            return
        args = argparse.Namespace(udid=row.info.udid, top=None)
        title = f"{row.info.name} ({row.info.udid})"
        self._open_detail_overlay(title, [f"Loading detail for {row.info.name}..."])
        cached_lines = self.cache.get_cached_detail(row.info.udid, row.info.path)
        description = f"Loading detail for {row.info.name}"
        if cached_lines:
            self._set_detail_overlay_lines(cached_lines)
            self._status("Showing cached details (refreshing...)", duration=3.0)
            description = f"Refreshing detail for {row.info.name}"
        self._run_cli_command(
            description,
            cmd_detail,
            args,
            detail_device=row.info,
            display_overlay=True,
        )

    def _boot_selected(self) -> None:
        row = self._selected_row()
        if not row:
            self._status("No simulator selected.", duration=3.0)
            return
        self._run_subprocess(
            f"Booting {row.info.name}",
            ["xcrun", "simctl", "boot", row.info.udid],
            refresh_devices=True,
            retain_udid=row.info.udid,
        )

    def _open_selected_in_finder(self) -> None:
        row = self._selected_row()
        if not row:
            self._status("No simulator selected.", duration=3.0)
            return
        self._run_subprocess(
            f"Opening {row.info.name} in Finder",
            ["open", str(row.info.path)],
        )

    def _clean_selected(self) -> None:
        row = self._selected_row()
        if not row:
            self._status("No simulator selected.", duration=3.0)
            return
        reply = self._prompt(
            "Clean mode — [c]aches (safe), [d]ata (wipes user data), [b]oth:"
        )
        if reply is None:
            self._status("Clean cancelled.", duration=2.0)
            return
        choice = reply.strip().lower()
        if choice in ("", "c", "caches"):
            mode_name, targets = "caches", list(CLEAN_CACHES_TARGETS)
        elif choice in ("d", "data"):
            mode_name, targets = "data", list(CLEAN_DATA_TARGETS)
        elif choice in ("b", "both"):
            mode_name, targets = "both", list(CLEAN_BOTH_TARGETS)
        else:
            self._status(f"Unknown mode: {reply!r}", duration=4.0)
            return
        execute = self._prompt_yes_no(
            f"Apply {mode_name} clean (otherwise dry-run)?", default=False
        )
        if execute is None:
            self._status("Clean cancelled.", duration=2.0)
            return
        self._run_clean_inline(row.info, mode_name, targets, execute)

    def _run_clean_inline(
        self,
        device: DeviceInfo,
        mode_name: str,
        targets: Sequence[str],
        execute: bool,
    ) -> None:
        """Run a clean on a background thread and surface progress in the row."""
        udid = device.udid
        target_list = list(targets)
        dry_run = not execute
        verb = "Cleaning" if execute else "Dry-run cleaning"
        self.event_queue.put(RowMessageEvent(udid, f"{verb} {mode_name}…"))
        self._needs_draw = True

        def job() -> None:
            total_reclaimed = 0
            failures: List[str] = []
            for target in target_list:
                handler = CLEAN_TARGETS.get(target)
                if handler is None:
                    failures.append(f"{target}: unknown target")
                    continue
                self.event_queue.put(
                    RowMessageEvent(udid, f"{verb} {mode_name}… ({target})")
                )
                try:
                    reclaimed = handler(device, dry_run=dry_run)
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.debug("Clean target %s failed: %s", target, exc, exc_info=True)
                    failures.append(f"{target}: {exc}")
                    continue
                if reclaimed:
                    total_reclaimed += reclaimed
            if failures:
                summary = (
                    f"{verb.capitalize()} {device.name} failed: {failures[0]}"
                    if len(failures) == 1
                    else f"{verb.capitalize()} {device.name} had {len(failures)} failures"
                )
                self.event_queue.put(
                    RowMessageEvent(udid, f"Error: {failures[0].split(':')[0]}")
                )
                self.event_queue.put(StatusMessageEvent(summary, duration=6.0))
            else:
                prefix = "Would reclaim" if dry_run else "Reclaimed"
                reclaimed_label = format_size(total_reclaimed) if total_reclaimed else "0 B"
                self.event_queue.put(
                    RowMessageEvent(udid, f"{prefix} {reclaimed_label}")
                )
                self.event_queue.put(
                    StatusMessageEvent(
                        f"{verb} {device.name}: {prefix.lower()} {reclaimed_label}.",
                        duration=5.0,
                    )
                )
            # Leave the summary visible long enough to read, then clear and
            # restart the size probe.
            time.sleep(6.0)
            self.event_queue.put(RowMessageEvent(udid, None))
            self.event_queue.put(RestartSizeJobEvent(udid))

        self._submit_task(job)

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if not row:
            self._status("No simulator selected.", duration=3.0)
            return
        confirm = self._prompt_yes_no(f"Delete {row.info.name}?", default=False)
        if confirm is not True:
            self._status("Delete cancelled.", duration=2.0)
            return
        force = False
        if row.info.state == "Booted":
            force_reply = self._prompt_yes_no("Simulator appears Booted. Force delete?", default=False)
            if force_reply is None:
                self._status("Delete cancelled.", duration=2.0)
                return
            force = force_reply
        execute = self._prompt_yes_no("Apply deletion (otherwise dry-run)?", default=False)
        if execute is None:
            self._status("Delete cancelled.", duration=2.0)
            return
        retain_udid = self._udid_after_delete()
        args = argparse.Namespace(
            udid=row.info.udid,
            execute=execute,
            yes=True,
            force=force,
        )
        self._run_cli_command(
            f"{'Deleting' if execute else 'Dry-run deleting'} {row.info.name}",
            cmd_delete_device,
            args,
            refresh_devices=True,
            retain_udid=retain_udid,
        )

    def _enter_multi_select_mode(self) -> None:
        if self.multi_select_mode:
            return
        self.multi_select_mode = True
        self.selected_udids.clear()
        self._status(
            "Multi-select mode — Space mark, a mark outdated, Enter/d delete, Esc cancel.",
            duration=None,
        )

    def _exit_multi_select_mode(self, message: str = "Multi-select cancelled.") -> None:
        if not self.multi_select_mode:
            return
        self.multi_select_mode = False
        self.selected_udids.clear()
        self._status(message, duration=3.0)

    def _handle_multi_select_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self._move_selection(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._move_selection(1)
        elif key == curses.KEY_PPAGE:
            self._move_selection(-self.page_size)
        elif key == curses.KEY_NPAGE:
            self._move_selection(self.page_size)
        elif key == ord("g"):
            self._set_selection(0)
        elif key == ord("G"):
            self._set_selection(len(self.rows) - 1)
        elif key == ord(" "):
            self._toggle_mark_current()
        elif key == ord("a"):
            self._mark_outdated()
        elif key in (ord("X"), ord("c"), ord("C")):
            self.selected_udids.clear()
            self._status("Marks cleared.", duration=2.0)
        elif key in (curses.KEY_ENTER, 10, 13, ord("d"), ord("D")):
            self._batch_delete_marked()
        elif key in (27, ord("q"), ord("Q")):
            self._exit_multi_select_mode()
        self._needs_draw = True

    def _toggle_mark_current(self) -> None:
        row = self._selected_row()
        if not row:
            return
        udid = row.info.udid
        if udid in self.selected_udids:
            self.selected_udids.discard(udid)
        else:
            self.selected_udids.add(udid)
        self._move_selection(1)

    def _selected_total_bytes(self) -> int:
        total = 0
        for udid in self.selected_udids:
            row = self.row_lookup.get(udid)
            if row and row.size_bytes is not None:
                total += row.size_bytes
        return total

    @staticmethod
    def _ios_runtime_version(raw_runtime: str) -> Optional[Tuple[int, ...]]:
        """Parse an ``iOS-<MAJOR>-<MINOR>`` runtime id into a version tuple.

        Returns None for non-iOS runtimes so callers can ignore them.
        """
        if not raw_runtime:
            return None
        suffix = raw_runtime.rsplit(".", 1)[-1]
        if not suffix.startswith("iOS-"):
            return None
        parts = suffix[len("iOS-"):].split("-")
        try:
            return tuple(int(p) for p in parts if p)
        except ValueError:
            return None

    def _mark_outdated(self) -> None:
        versions = [
            self._ios_runtime_version(row.info.raw_runtime) for row in self.rows
        ]
        available = [v for v in versions if v is not None]
        if not available:
            self._status("No iOS runtimes detected.", duration=3.0)
            return
        latest = max(available)
        added = 0
        for row, version in zip(self.rows, versions):
            if version is None or version >= latest:
                continue
            if row.info.udid not in self.selected_udids:
                self.selected_udids.add(row.info.udid)
                added += 1
        latest_label = ".".join(str(p) for p in latest)
        if added:
            self._status(
                f"Marked {added} simulator(s) older than iOS {latest_label}.",
                duration=4.0,
            )
        else:
            self._status(
                f"No additional outdated simulators (latest iOS {latest_label}).",
                duration=4.0,
            )

    def _batch_delete_marked(self) -> None:
        if not self.selected_udids:
            self._status("Nothing marked. Press Space to mark or Esc to exit.", duration=3.0)
            return
        targets: List[DeviceRow] = []
        booted_skipped: List[str] = []
        for udid in list(self.selected_udids):
            row = self.row_lookup.get(udid)
            if not row:
                self.selected_udids.discard(udid)
                continue
            if row.info.state == "Booted":
                booted_skipped.append(row.info.name)
                continue
            targets.append(row)
        if not targets:
            if booted_skipped:
                self._status(
                    f"All marked sims are Booted; skipping: {', '.join(booted_skipped)}",
                    duration=6.0,
                )
            else:
                self._status("Nothing to delete.", duration=3.0)
            return

        total_bytes = sum((r.size_bytes or 0) for r in targets)
        warn = f" ({len(booted_skipped)} Booted skipped)" if booted_skipped else ""
        confirm = self._prompt_yes_no(
            f"Delete {len(targets)} simulators reclaiming ~{format_size(total_bytes)}?{warn}",
            default=False,
        )
        if confirm is not True:
            self._status("Batch delete cancelled.", duration=2.0)
            return
        execute = self._prompt_yes_no("Apply deletion (otherwise dry-run)?", default=False)
        if execute is None:
            self._status("Batch delete cancelled.", duration=2.0)
            return

        target_udids = [r.info.udid for r in targets]
        target_names = {r.info.udid: r.info.name for r in targets}
        retain_udid = self._udid_after_delete()
        dry_run = not execute
        description = (
            f"{'Dry-run deleting' if dry_run else 'Deleting'} {len(target_udids)} simulators"
        )

        def job() -> None:
            successes = 0
            failures: List[str] = []
            reclaimed = 0
            for udid in target_udids:
                name = target_names.get(udid, udid)
                if dry_run:
                    self.event_queue.put(
                        StatusMessageEvent(f"(dry-run) would delete {name}", duration=2.0)
                    )
                    successes += 1
                    continue
                try:
                    result = subprocess.run(
                        ["xcrun", "simctl", "delete", udid],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except (FileNotFoundError, subprocess.SubprocessError) as exc:
                    failures.append(f"{name}: {exc}")
                    continue
                if result.returncode == 0:
                    successes += 1
                    row = self.row_lookup.get(udid)
                    if row and row.size_bytes:
                        reclaimed += row.size_bytes
                else:
                    err = (result.stderr or result.stdout or "unknown error").strip().splitlines()
                    failures.append(f"{name}: {err[-1] if err else 'unknown error'}")
            parts = [f"Deleted {successes}/{len(target_udids)}"]
            if not dry_run and reclaimed:
                parts.append(f"~{format_size(reclaimed)} reclaimed")
            if failures:
                parts.append(f"{len(failures)} failed")
            if booted_skipped:
                parts.append(f"{len(booted_skipped)} Booted skipped")
            self.event_queue.put(StatusMessageEvent(" • ".join(parts), duration=8.0))
            if failures:
                self.event_queue.put(
                    DetailOutputEvent(lines=["Batch delete failures:"] + failures)
                )
            self.event_queue.put(ReloadDevicesEvent(selected_udid=retain_udid))

        # Optimistically clear marks; refreshed device list will repopulate if any remain.
        self.selected_udids.difference_update(target_udids)
        self.multi_select_mode = False
        self._status(description, duration=None)
        self._submit_task(job)

    def _udid_after_delete(self) -> Optional[str]:
        if len(self.rows) <= 1:
            return None
        if self.selected_index < len(self.rows) - 1:
            return self.rows[self.selected_index + 1].info.udid
        return self.rows[self.selected_index - 1].info.udid

    def _truncate(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width == 1:
            return text[:1]
        return text[: width - 1] + "…"

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _safe_addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        if n <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, n, attr)
        except curses.error:
            pass

def cmd_scan(args: argparse.Namespace) -> None:
    LOGGER.debug("cmd_scan invoked (min_size_mb=%s, top=%s, json=%s)", args.min_size_mb, args.top, args.json)
    spinner = Spinner("Scanning simulators...", enabled=(not args.json and sys.stdout.isatty()))
    if spinner.enabled:
        spinner.start()
    try:
        filtered, globals_summary = collect_scan_results(args.min_size_mb, args.top)
    finally:
        spinner.stop()
    LOGGER.debug(
        "cmd_scan retrieved %d devices and %d global entries",
        len(filtered),
        len(globals_summary),
    )

    if args.json:
        payload = [
            {
                "udid": d.udid,
                "name": d.name,
                "runtime": d.runtime,
                "runtime_id": d.raw_runtime,
                "device_type": d.device_type,
                "state": d.state,
                "last_booted_at": d.last_booted_at,
                "size_bytes": d.size_bytes,
                "log_bytes": d.log_bytes,
                "path": str(d.path),
            }
            for d in filtered
        ]
        print(json.dumps({"devices": payload, "globals": globals_summary}, indent=2))
        return
    print(format_scan_report(filtered, globals_summary))


def cmd_detail(args: argparse.Namespace) -> None:
    LOGGER.debug("cmd_detail invoked for udid=%s top=%s", args.udid, args.top)
    devices = discover_devices()
    LOGGER.debug("cmd_detail discovered %d devices", len(devices))
    device = ensure_device(args.udid, devices)

    print(f"{device.name} ({device.udid})")
    print(f"Runtime: {device.runtime} ({device.raw_runtime})")
    print(f"State: {device.state}")
    if device.last_booted_at:
        print(f"Last booted: {device.last_booted_at}")
    print(f"Path: {device.path}")
    print(f"Total size: {format_size(device.size_bytes)}")
    if device.log_bytes is not None:
        print(f"Logs: {format_size(device.log_bytes)} ({LOG_ROOT / device.udid})")

    print("\nTop folders inside data/:")
    breakdown = collect_device_breakdown(device)
    limit = args.top or len(breakdown)
    rows = [
        [label, format_size(size), str(path)]
        for label, path, size in breakdown[:limit]
    ]
    if rows:
        print(render_table(["Folder", "Size", "Path"], rows))
    else:
        print("No data folders found.")

    print("\nLargest app containers:")
    app_rows = []
    for label, path, size in collect_top_app_containers(device, limit=args.top or 10):
        app_rows.append([label, format_size(size), str(path)])
    if app_rows:
        print(render_table(["Bundle ID", "Size", "Path"], app_rows))
    else:
        print("No app containers detected.")


def cmd_clean(args: argparse.Namespace) -> None:
    LOGGER.debug(
        "cmd_clean invoked for udid=%s targets=%s execute=%s yes=%s",
        args.udid,
        args.targets,
        args.execute,
        args.yes,
    )
    devices = discover_devices()
    LOGGER.debug("cmd_clean discovered %d devices", len(devices))
    device = ensure_device(args.udid, devices)

    mode = getattr(args, "mode", None)
    if mode:
        targets = set(CLEAN_MODES[mode])
    else:
        targets = set(args.targets)
    unknown = targets - CLEAN_TARGETS.keys()
    if unknown:
        print(f"error: unknown targets {', '.join(sorted(unknown))}", file=sys.stderr)
        sys.exit(1)

    dry_run = not args.execute
    if dry_run:
        print("Dry-run mode: no changes will be made. Use --execute to apply.")
    elif not args.yes:
        if not prompt_yes_no(f"Proceed with cleaning {human_targets(targets)} for {device.udid}?"):
            print("Aborted.")
            return

    total_reclaimed = 0
    for target in sorted(targets):
        handler = CLEAN_TARGETS[target]
        reclaimed = handler(device, dry_run=dry_run)
        if reclaimed is None:
            LOGGER.debug("Target %s has nothing to clean for %s", target, device.udid)
            print(f"{target}: nothing to clean.")
            continue
        total_reclaimed += reclaimed
        LOGGER.debug("Target %s reclaimed %s bytes for %s", target, reclaimed, device.udid)
        action = "Would reclaim" if dry_run else "Reclaimed"
        print(f"{action} {format_size(reclaimed)} from {target}")

    if total_reclaimed == 0:
        print("No data removed.")
    else:
        print(f"Total {'potential ' if dry_run else ''}reclaimed: {format_size(total_reclaimed)}")
    LOGGER.debug(
        "cmd_clean finished for %s (dry_run=%s) total=%s bytes",
        device.udid,
        dry_run,
        total_reclaimed,
    )


def verify_not_booted(device: DeviceInfo, force: bool) -> None:
    if device.state == "Booted" and not force:
        print("error: device is Booted. Stop the simulator or pass --force.", file=sys.stderr)
        sys.exit(2)


def cmd_delete_device(args: argparse.Namespace) -> None:
    LOGGER.debug(
        "cmd_delete_device invoked for udid=%s execute=%s yes=%s force=%s",
        args.udid,
        args.execute,
        args.yes,
        args.force,
    )
    devices = discover_devices()
    LOGGER.debug("cmd_delete_device discovered %d devices", len(devices))
    device = ensure_device(args.udid, devices)

    verify_not_booted(device, force=args.force)
    dry_run = not args.execute
    if dry_run:
        print("Dry-run mode: simulator will not be deleted. Use --execute to remove it.")
    elif not args.yes:
        message = f"Delete simulator {device.name} ({device.udid})? This removes all data."
        if not prompt_yes_no(message):
            print("Aborted.")
            return

    reclaimed = 0
    device_size = safe_dir_size(device.path)
    log_size = safe_dir_size(LOG_ROOT / device.udid)
    if dry_run:
        reclaimed = (device_size or 0) + (log_size or 0)
    else:
        reclaimed += remove_path(device.path, dry_run=False) or 0
        reclaimed += remove_path(LOG_ROOT / device.udid, dry_run=False) or 0

    print(f"{'Would remove' if dry_run else 'Removed'} device directory {device.path}")
    if log_size:
        print(f"{'Would remove' if dry_run else 'Removed'} logs at {LOG_ROOT / device.udid}")
    if reclaimed:
        print(f"Total {'potential ' if dry_run else ''}space freed: {format_size(reclaimed)}")
    LOGGER.debug(
        "cmd_delete_device completed for %s (dry_run=%s) reclaimed=%s",
        device.udid,
        dry_run,
        reclaimed,
    )


def collect_globals_summary() -> List[Dict[str, Any]]:
    items: List[Dict[str, str]] = []
    LOGGER.debug("Collecting global simulator resources")
    with log_timing("collect_globals_summary"):
        dyld_size = safe_dir_size(DYLD_CACHE_PATH)
        if dyld_size:
            items.append(
                {
                    "category": "dyld-cache",
                    "identifier": "dyld",
                    "path": str(DYLD_CACHE_PATH),
                    "bytes": dyld_size,
                }
            )

        log_size = safe_dir_size(LOG_ROOT)
        if log_size:
            items.append(
                {
                    "category": "logs",
                    "identifier": "CoreSimulator",
                    "path": str(LOG_ROOT),
                    "bytes": log_size,
                }
            )

        if IB_SUPPORT_SIM_DEVICES.exists():
            ib_size = safe_dir_size(IB_SUPPORT_SIM_DEVICES)
            if ib_size is not None:
                items.append(
                    {
                        "category": "ib-support",
                        "identifier": "IB Support Simulator Devices",
                        "path": str(IB_SUPPORT_SIM_DEVICES),
                        "bytes": ib_size or 0,
                    }
                )

        if RUNTIME_VOLUMES_ROOT.exists():
            for volume in sorted(RUNTIME_VOLUMES_ROOT.iterdir()):
                if not volume.is_dir():
                    continue
                size = safe_dir_size(volume)
                items.append(
                    {
                        "category": "runtime-volume",
                        "identifier": volume.name,
                        "path": str(volume),
                        "bytes": size or 0,
                    }
                )

        if CRYPDEX_BUNDLE_ROOT.exists():
            for bundle in sorted(CRYPDEX_BUNDLE_ROOT.iterdir()):
                if not bundle.is_dir():
                    continue
                size = safe_dir_size(bundle)
                items.append(
                    {
                        "category": "cryptex-bundle",
                        "identifier": bundle.name,
                        "path": str(bundle),
                        "bytes": size or 0,
                    }
                )

    LOGGER.debug("collect_globals_summary found %d entries", len(items))
    return items


def cmd_purge_globals(args: argparse.Namespace) -> None:
    LOGGER.debug(
        "cmd_purge_globals invoked (dyld=%s logs=%s volume=%s cryptex=%s execute=%s yes=%s)",
        args.dyld,
        args.logs,
        args.volume,
        args.cryptex,
        args.execute,
        args.yes,
    )
    dry_run = not args.execute
    targets = []

    if args.dyld:
        targets.append(("dyld cache", DYLD_CACHE_PATH))
    if args.logs:
        targets.append(("global logs", LOG_ROOT))
    for name in args.volume or []:
        path = RUNTIME_VOLUMES_ROOT / name
        targets.append((f"runtime volume {name}", path))
    for name in args.cryptex or []:
        path = CRYPDEX_BUNDLE_ROOT / name
        targets.append((f"cryptex bundle {name}", path))

    if not targets:
        print("Nothing to purge. Pass --dyld/--logs/--volume/--cryptex.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print("Dry-run mode: no files will be deleted. Use --execute to apply.")
    elif not args.yes:
        summary = "; ".join(title for title, _ in targets)
        if not prompt_yes_no(f"Proceed with deleting {summary}?"):
            print("Aborted.")
            return

    total = 0
    for title, path in targets:
        if not path.exists():
            LOGGER.debug("Target %s missing at %s", title, path)
            print(f"{title}: not found ({path})")
            continue
        size = safe_dir_size(path) or 0
        total += size
        LOGGER.debug("Purging %s at %s (dry_run=%s size=%s)", title, path, dry_run, size)
        if dry_run:
            print(f"Would remove {format_size(size)} at {path} ({title})")
        else:
            remove_path(path, dry_run=False)
            print(f"Removed {format_size(size)} at {path} ({title})")

    if total:
        print(f"Total {'potential ' if dry_run else ''}space reclaimed: {format_size(total)}")
    else:
        print("No space reclaimed.")
    LOGGER.debug("cmd_purge_globals complete (dry_run=%s) total=%s bytes", dry_run, total)


def clean_target_device_caches(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = device.path / "data" / "Library" / "Caches"
    if not path.exists():
        LOGGER.debug("No device caches at %s", path)
        return None
    LOGGER.debug("Cleaning device caches for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


def clean_target_app_caches(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    app_root = device.path / "data" / "Containers" / "Data" / "Application"
    if not app_root.exists():
        LOGGER.debug("No app containers at %s", app_root)
        return None
    total = 0
    LOGGER.debug("Cleaning app caches for %s (dry_run=%s)", device.udid, dry_run)
    for entry in app_root.iterdir():
        cache_dir = entry / "Library" / "Caches"
        if not cache_dir.exists():
            continue
        LOGGER.debug("Cleaning app cache %s", cache_dir)
        reclaimed = clear_directory_contents(cache_dir, dry_run=dry_run)
        total += reclaimed
    LOGGER.debug("App caches reclaimed %s bytes for %s", total, device.udid)
    return total or None


def clean_target_tmp(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    tmp_dir = device.path / "data" / "tmp"
    if not tmp_dir.exists():
        LOGGER.debug("No tmp directory at %s", tmp_dir)
        return None
    LOGGER.debug("Cleaning tmp for %s at %s (dry_run=%s)", device.udid, tmp_dir, dry_run)
    return clear_directory_contents(tmp_dir, dry_run=dry_run)


def clean_target_downloads(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = device.path / "data" / "Downloads"
    if not path.exists():
        LOGGER.debug("No downloads directory at %s", path)
        return None
    LOGGER.debug("Cleaning downloads for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


def clean_target_media(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = device.path / "data" / "Media"
    if not path.exists():
        LOGGER.debug("No media directory at %s", path)
        return None
    LOGGER.debug("Cleaning media for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


def clean_target_logs(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = LOG_ROOT / device.udid
    if not path.exists():
        LOGGER.debug("No logs found for %s at %s", device.udid, path)
        return None
    LOGGER.debug("Cleaning logs for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return remove_path(path, dry_run=dry_run)


def clean_target_shared(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = device.path / "data" / "Containers" / "Shared"
    if not path.exists():
        LOGGER.debug("No shared containers at %s", path)
        return None
    LOGGER.debug("Cleaning shared containers for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


def clean_target_bundles(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    path = device.path / "data" / "Containers" / "Bundle"
    if not path.exists():
        LOGGER.debug("No bundles directory at %s", path)
        return None
    LOGGER.debug("Cleaning bundles for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


def clean_target_app_data(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    """Clear Documents, Application Support, and tmp for every app container.

    Preserves Library/Preferences so NSUserDefaults (login tokens, onboarding
    flags, etc.) survive the clean. This is more destructive than app-caches
    because it wipes real user content stored by apps.
    """
    app_root = device.path / "data" / "Containers" / "Data" / "Application"
    if not app_root.exists():
        LOGGER.debug("No app containers at %s", app_root)
        return None
    total = 0
    LOGGER.debug("Cleaning app data for %s (dry_run=%s)", device.udid, dry_run)
    for entry in app_root.iterdir():
        if not entry.is_dir():
            continue
        for rel in ("Documents", "Library/Application Support", "tmp"):
            target = entry / rel
            if not target.exists():
                continue
            LOGGER.debug("Cleaning app data dir %s", target)
            total += clear_directory_contents(target, dry_run=dry_run)
    LOGGER.debug("App data reclaimed %s bytes for %s", total, device.udid)
    return total or None


def clean_target_mobile_assets(device: DeviceInfo, dry_run: bool) -> Optional[int]:
    """Clear downloaded iOS assets (Siri voices, dictation models, etc.).

    iOS re-downloads these on demand, so the first boot after cleaning can
    feel slower until the essentials are refetched.
    """
    path = device.path / "data" / "private" / "var" / "MobileAsset"
    if not path.exists():
        LOGGER.debug("No MobileAsset directory at %s", path)
        return None
    LOGGER.debug("Cleaning mobile assets for %s at %s (dry_run=%s)", device.udid, path, dry_run)
    return clear_directory_contents(path, dry_run=dry_run)


# Mode groupings used by the TUI and CLI --mode flag.
CLEAN_CACHES_TARGETS: Tuple[str, ...] = ("device-caches", "app-caches", "tmp")
CLEAN_DATA_TARGETS: Tuple[str, ...] = ("app-data", "mobile-assets")
CLEAN_BOTH_TARGETS: Tuple[str, ...] = CLEAN_CACHES_TARGETS + CLEAN_DATA_TARGETS

CLEAN_MODES: Dict[str, Tuple[str, ...]] = {
    "caches": CLEAN_CACHES_TARGETS,
    "data": CLEAN_DATA_TARGETS,
    "both": CLEAN_BOTH_TARGETS,
}

DEFAULT_CLEAN_TARGETS: Tuple[str, ...] = CLEAN_CACHES_TARGETS


CLEAN_TARGETS: Dict[str, Callable[[DeviceInfo, bool], Optional[int]]] = {
    "device-caches": clean_target_device_caches,
    "app-caches": clean_target_app_caches,
    "tmp": clean_target_tmp,
    "downloads": clean_target_downloads,
    "media": clean_target_media,
    "logs": clean_target_logs,
    "shared": clean_target_shared,
    "bundles": clean_target_bundles,
    "app-data": clean_target_app_data,
    "mobile-assets": clean_target_mobile_assets,
}


def cmd_menu(args: argparse.Namespace) -> None:
    cache = CacheManager()
    try:
        curses.wrapper(lambda stdscr: SimulatorTui(stdscr, cache).run())
    except curses.error as exc:
        print(f"error: unable to start TUI: {exc}", file=sys.stderr)
        sys.exit(1)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and clean iOS Simulator storage.",
    )
    parser.set_defaults(command="menu", func=cmd_menu)
    subparsers = parser.add_subparsers(dest="command")

    menu = subparsers.add_parser("menu", help="Launch the interactive TUI menu (default).")
    menu.set_defaults(func=cmd_menu)

    scan = subparsers.add_parser("scan", help="List simulators sorted by size.")
    scan.add_argument("--top", type=int, help="Limit output to the top N simulators.")
    scan.add_argument(
        "--min-size-mb",
        type=int,
        help="Only include simulators larger than this size (in MB).",
    )
    scan.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    scan.set_defaults(func=cmd_scan)

    detail = subparsers.add_parser("detail", help="Show detailed breakdown for a simulator.")
    detail.add_argument("udid", help="Simulator UDID.")
    detail.add_argument("--top", type=int, help="Show only the top N entries per section.")
    detail.set_defaults(func=cmd_detail)

    clean = subparsers.add_parser("clean", help="Remove temporary data from a simulator.")
    clean.add_argument("udid", help="Simulator UDID.")
    clean.add_argument(
        "--mode",
        choices=sorted(CLEAN_MODES.keys()),
        help="Preset target group: caches (safe), data (wipes user data), both.",
    )
    clean.add_argument(
        "--targets",
        nargs="+",
        default=list(DEFAULT_CLEAN_TARGETS),
        help="Cleanup targets. Choices: " + ", ".join(sorted(CLEAN_TARGETS.keys())),
    )
    clean.add_argument("--execute", action="store_true", help="Apply changes (default is dry-run).")
    clean.add_argument("--yes", action="store_true", help="Skip confirmation prompts.")
    clean.set_defaults(func=cmd_clean)

    delete_device = subparsers.add_parser("delete-device", help="Delete an entire simulator device.")
    delete_device.add_argument("udid", help="Simulator UDID.")
    delete_device.add_argument("--execute", action="store_true", help="Apply changes (default is dry-run).")
    delete_device.add_argument("--yes", action="store_true", help="Skip confirmation prompts.")
    delete_device.add_argument("--force", action="store_true", help="Allow removal even if the simulator is Booted.")
    delete_device.set_defaults(func=cmd_delete_device)

    purge = subparsers.add_parser("purge-global", help="Clean shared CoreSimulator assets.")
    purge.add_argument("--dyld", action="store_true", help="Delete the dyld cache.")
    purge.add_argument("--logs", action="store_true", help="Delete global CoreSimulator logs.")
    purge.add_argument(
        "--volume",
        action="append",
        help="Delete a runtime volume by folder name (e.g. iOS_23A343). Can be passed multiple times.",
    )
    purge.add_argument(
        "--cryptex",
        action="append",
        help="Delete a cryptex runtime bundle by folder name.",
    )
    purge.add_argument("--execute", action="store_true", help="Apply changes (default is dry-run).")
    purge.add_argument("--yes", action="store_true", help="Skip confirmation prompts.")
    purge.set_defaults(func=cmd_purge_globals)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
