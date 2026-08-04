import os
import platform
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional


IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def find_playwright_chromium() -> Optional[str]:
    """
    Return the installed Playwright Chromium executable path.

    Supports:
    - Windows
    - macOS Apple Silicon / Intel
    - Linux
    """
    candidates: list[Path] = []

    if IS_WINDOWS:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            base = Path(local_app_data) / "ms-playwright"
            candidates.extend(
                base.glob("chromium-*/chrome-win*/chrome.exe")
            )

    elif IS_MAC:
        base = Path.home() / "Library" / "Caches" / "ms-playwright"

        candidates.extend(
            base.glob(
                "chromium-*/chrome-mac*/Google Chrome for Testing.app/"
                "Contents/MacOS/Google Chrome for Testing"
            )
        )

        candidates.extend(
            base.glob(
                "chromium-*/chrome-mac*/Chromium.app/"
                "Contents/MacOS/Chromium"
            )
        )

        candidates.extend(
            base.glob(
                "chromium-*/chrome-mac-arm64/Chromium.app/"
                "Contents/MacOS/Chromium"
            )
        )

        candidates.extend(
            base.glob(
                "chromium-*/chrome-mac-x64/Chromium.app/"
                "Contents/MacOS/Chromium"
            )
        )

    else:
        base = Path.home() / ".cache" / "ms-playwright"
        candidates.extend(
            base.glob("chromium-*/chrome-linux*/chrome")
        )

    valid_candidates = [
        candidate
        for candidate in candidates
        if candidate.exists() and os.access(candidate, os.X_OK)
    ]

    if not valid_candidates:
        return None

    valid_candidates.sort(
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    return str(valid_candidates[0])


def _mac_descendant_pids(parent_pid: int) -> list[int]:
    """
    Return all descendant process IDs for a process on macOS/Linux.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    children_by_parent: dict[int, list[int]] = {}

    for line in result.stdout.splitlines():
        parts = line.split()

        if len(parts) != 2:
            continue

        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue

        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    stack = list(children_by_parent.get(parent_pid, []))

    while stack:
        pid = stack.pop()
        descendants.append(pid)
        stack.extend(children_by_parent.get(pid, []))

    return descendants


def terminate_process_tree(
    pid: int,
    *,
    force_after_seconds: float = 2.0,
) -> None:
    """
    Terminate one process and all processes started beneath it.

    Windows uses taskkill.
    macOS/Linux recursively terminate descendants.
    """
    if not pid or pid <= 0:
        return

    if IS_WINDOWS:
        subprocess.run(
            [
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
            ],
            capture_output=True,
            check=False,
        )
        return

    descendants = _mac_descendant_pids(pid)

    # Kill children before the parent.
    for child_pid in reversed(descendants):
        try:
            os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return

    deadline = time.time() + force_after_seconds

    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return

        time.sleep(0.1)

    # Force-kill any remaining children and parent.
    remaining = _mac_descendant_pids(pid)

    for child_pid in reversed(remaining):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def get_pid_listening_on_port(port: int) -> Optional[int]:
    """
    Return the PID listening on the supplied TCP port.
    """
    if IS_WINDOWS:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            check=False,
        )

        for line in result.stdout.splitlines():
            if "LISTENING" not in line:
                continue

            parts = line.split()

            if (
                len(parts) >= 5
                and parts[1].endswith(f":{port}")
                and parts[-1].isdigit()
            ):
                return int(parts[-1])

        return None

    result = subprocess.run(
        [
            "lsof",
            "-nP",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-t",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    for line in result.stdout.splitlines():
        line = line.strip()

        if line.isdigit():
            return int(line)

    return None


def kill_process_by_port(port: int) -> Optional[int]:
    """
    Kill the process listening on the supplied port.

    Returns the killed PID, or None when no process was found.
    """
    pid = get_pid_listening_on_port(port)

    if pid is None:
        return None

    terminate_process_tree(pid)
    return pid
