import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

# Historical pre-CVS windows, explicitly in IST.
SCOUT_STARTS = (
    (25, 55),  # targets :26
    (29, 55),  # targets :30 / :31
    (55, 55),  # targets :56
    (59, 55),  # targets :00 / :01
)

SCOUT_SPACING_SECONDS = 2
SCOUT_DUE_TOLERANCE_SECONDS = 1.5

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCOUT_DIR = PROJECT_ROOT / "logs"
SCOUT_STATE_FILE = SCOUT_DIR / "scout_state.json"
SCOUT_LOCK_DIR = SCOUT_DIR / "scout_state.lock"


def _read_unlocked() -> dict:
    try:
        if not SCOUT_STATE_FILE.exists():
            return {}
        return json.loads(
            SCOUT_STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def _write_unlocked(state: dict) -> None:
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)

    tmp = SCOUT_STATE_FILE.with_name(
        f".{SCOUT_STATE_FILE.name}.{os.getpid()}.tmp"
    )

    tmp.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, SCOUT_STATE_FILE)


@contextmanager
def _state_lock(timeout: float = 0.35):
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    acquired = False

    while not acquired:
        try:
            SCOUT_LOCK_DIR.mkdir()
            acquired = True
        except FileExistsError:
            # Remove a stale lock left by a killed process.
            try:
                age = time.time() - SCOUT_LOCK_DIR.stat().st_mtime
                if age > 5:
                    SCOUT_LOCK_DIR.rmdir()
                    continue
            except Exception:
                pass

            if time.monotonic() - started >= timeout:
                raise TimeoutError("scout state lock timeout")

            time.sleep(0.005)

    try:
        yield
    finally:
        try:
            SCOUT_LOCK_DIR.rmdir()
        except Exception:
            pass


def mark_cvs_stop() -> bool:
    """
    Record that the normal CVS flow has fired.

    This ONLY stops a scout window that started before this timestamp.
    Future windows remain unaffected.
    """
    try:
        with _state_lock():
            state = _read_unlocked()
            state["cvs_stop_at"] = time.time()
            _write_unlocked(state)
        return True
    except Exception:
        # Scout coordination must never break the existing CVS path.
        return False


def is_window_stopped(
    window_id: str,
    window_start_epoch: float,
) -> bool:
    state = _read_unlocked()

    cvs_stop_at = float(state.get("cvs_stop_at", 0) or 0)

    if cvs_stop_at >= window_start_epoch:
        return True

    if state.get("scout_hit_window") == window_id:
        return True

    return False


def claim_scout_hit(
    window_id: str,
    window_start_epoch: float,
    city: str,
    date_str: str,
    detected_by: str,
) -> tuple[bool, str]:
    """
    Atomically let the first scout hit own this release.

    If CVS already fired for this window, CVS wins.
    """
    try:
        with _state_lock():
            state = _read_unlocked()

            cvs_stop_at = float(
                state.get("cvs_stop_at", 0) or 0
            )

            if cvs_stop_at >= window_start_epoch:
                return False, "cvs"

            if state.get("scout_hit_window") == window_id:
                return False, "scout_already_hit"

            state.update(
                {
                    "scout_hit_window": window_id,
                    "scout_hit_at": time.time(),
                    "scout_hit_city": city,
                    "scout_hit_date": date_str,
                    "scout_hit_by": detected_by,
                }
            )

            _write_unlocked(state)

        return True, "claimed"

    except Exception:
        return False, "lock_timeout"


def get_due_scout_window(
    account_position: int,
    last_window_id: str,
):
    """
    Return the scout window due NOW for this account position.

    Account 0 = +0 sec
    Account 1 = +2 sec
    Account 2 = +4 sec
    etc.

    All scheduling is explicitly Asia/Kolkata.
    """
    if account_position < 0:
        return None

    now = datetime.now(IST)
    candidates = []

    # previous hour is needed for the :59:55 window carrying
    # into the following hour.
    for hours_back in (0, 1):
        hour_base = (
            now - timedelta(hours=hours_back)
        ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        for minute, second in SCOUT_STARTS:
            start = hour_base.replace(
                minute=minute,
                second=second,
            )

            due = start + timedelta(
                seconds=account_position
                * SCOUT_SPACING_SECONDS
            )

            lateness = (now - due).total_seconds()

            if (
                0 <= lateness
                <= SCOUT_DUE_TOLERANCE_SECONDS
            ):
                window_id = start.strftime(
                    "%Y%m%d-%H%M%S"
                )

                if window_id != last_window_id:
                    candidates.append(
                        (
                            lateness,
                            window_id,
                            start.timestamp(),
                            due,
                        )
                    )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])

    _, window_id, start_epoch, due = candidates[0]

    return {
        "window_id": window_id,
        "window_start_epoch": start_epoch,
        "due": due,
    }