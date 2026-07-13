"""
Thread-safe state file I/O with Windows-compatible file locking.

State files (src/state_<uid>.json) are shared between the monitor runner
and the booking runner. This module provides locked read/write to prevent
race conditions.
"""

import json
import os
import time
from pathlib import Path


def _acquire_lock(lock_file: Path, timeout: float = 5.0) -> bool:
    """Acquire a cross-process lock using a dedicated .lock file."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            # os.O_EXCL ensures this fails if the file already exists
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    return False


def _release_lock(lock_file: Path) -> None:
    """Release the cross-process lock by deleting the .lock file."""
    try:
        lock_file.unlink()
    except OSError:
        pass


def read_state(state_file: Path) -> dict:
    """Read and parse a JSON state file. Returns {} on any error."""
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state_file: Path, state: dict) -> None:
    """Write state dict to a JSON file using atomic write pattern.
    
    Writes to a temporary file first, then replaces the target to minimize
    the window where a concurrent reader could see a partial write.
    """
    tmp_path = state_file.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # os.replace is atomic on the same filesystem
        os.replace(str(tmp_path), str(state_file))
    except Exception:
        # Fallback: direct write if atomic replace fails
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass



def update_state(state_file: Path, updates: dict) -> None:
    """Atomically read, merge updates, and write the state file using a cross-process lock."""

    if updates.get("pending") is True:
        remote_url = os.environ.get("REMOTE_TRIGGER_URL", "").strip()

        if remote_url and not _is_reserved_booking_state(state_file):
            username = state_file.stem.replace("state_", "")
            print(
                f"[STATE] ⏭️ Remote trigger blocked for '{username}': "
                "account is not RESERVED_BOOKING."
            )
            remote_url = ""

        if remote_url:
            import urllib.request
            import threading

            def send_remote():
                try:
                    username = state_file.stem.replace("state_", "")
                    payload = json.dumps({
                        "username": username,
                        "updates": updates
                    }).encode("utf-8")

                    req = urllib.request.Request(
                        remote_url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urllib.request.urlopen(req, timeout=3) as response:
                        print(
                            f"[STATE] ✅ Remote trigger sent for '{username}' "
                            f"(HTTP {response.status})"
                        )
                except Exception as e:
                    print(
                        f"[STATE] ❌ Failed to send remote trigger "
                        f"to {remote_url}: {e}"
                    )

            threading.Thread(target=send_remote, daemon=True).start()

    lock_file = state_file.with_suffix(".lock")

    if _acquire_lock(lock_file):
        try:
            state = read_state(state_file)
            state.update(updates)
            write_state(state_file, state)
        finally:
            _release_lock(lock_file)
    else:
        state = read_state(state_file)
        state.update(updates)
        write_state(state_file, state)