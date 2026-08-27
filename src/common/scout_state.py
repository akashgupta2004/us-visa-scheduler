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
SCOUT_CYCLES = 2
SCOUT_DUE_TOLERANCE_SECONDS = 1.5

# Consular Scout uses the same historical release windows as OFC,
# but is coordinated completely separately.
#
# These four anchors, together with the existing 2-cycle account
# staggering, cover the two observed Consular release regions:
#
#   :25–:34
#   :55–:04
#
# Do NOT merge this with SCOUT_STARTS. Keeping it separate allows
# Consular Scout timing to be changed later without affecting OFC Scout.
CONSULAR_SCOUT_STARTS = (
    (25, 55),
    (29, 55),
    (55, 55),
    (59, 55),
)

CONSULAR_SCOUT_SPACING_SECONDS = 2
CONSULAR_SCOUT_CYCLES = 2
CONSULAR_SCOUT_DUE_TOLERANCE_SECONDS = 1.5
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

def claim_consular_scout_hit(
    window_id: str,
    city: str,
    date_str: str,
    detected_by: str,
) -> tuple[bool, str]:
    """
    Atomically claim one Consular Scout city/date discovery.

    IMPORTANT:
    Unlike OFC Scout, one Consular hit does NOT stop the whole
    scout window.

    Example:
        HYDERABAD 2026-09-10 may be detected first,
        then NEW DELHI 2026-09-04 may appear seconds later.

    Both must remain actionable.

    Therefore deduplication is only:

        window_id + city + date

    This state is completely separate from existing OFC Scout
    coordination and from cvs_stop_at.
    """
    try:
        normalized_city = str(city or "").strip().upper()
        normalized_date = str(date_str or "").strip()

        if not window_id or not normalized_city or not normalized_date:
            return False, "invalid_hit"

        hit_key = (
            f"{window_id}|"
            f"{normalized_city}|"
            f"{normalized_date}"
        )

        now = time.time()

        with _state_lock():
            state = _read_unlocked()

            hits = state.get("consular_scout_hits")

            if not isinstance(hits, dict):
                hits = {}

            # Prevent scout_state.json from growing forever.
            # Anything older than six hours is irrelevant to
            # current Consular Scout coordination.
            cutoff = now - (6 * 60 * 60)

            cleaned_hits = {}

            for key, value in hits.items():
                try:
                    hit_at = float(
                        (value or {}).get("detected_at", 0)
                    )
                except (TypeError, ValueError):
                    hit_at = 0

                if hit_at >= cutoff:
                    cleaned_hits[key] = value

            hits = cleaned_hits

            if hit_key in hits:
                return False, "consular_scout_already_hit"

            hits[hit_key] = {
                "window_id": window_id,
                "city": normalized_city,
                "date": normalized_date,
                "detected_by": detected_by,
                "detected_at": now,
            }

            state["consular_scout_hits"] = hits

            _write_unlocked(state)

        return True, "claimed"

    except Exception:
        # Scout coordination failure must never interfere with
        # the existing booking / CVS flow.
        return False, "lock_timeout"
def get_due_scout_window(
    account_position: int,
    account_count: int,
    last_window_id: str,
):
    """
    Return the scout window due NOW for this account position.

    Each configured SCOUT_START runs SCOUT_CYCLES times.

    Example with 35 accounts and 2-second spacing:

    Cycle 1:
        Account 0  = +0 sec
        Account 1  = +2 sec
        ...
        Account 34 = +68 sec

    Cycle 2 starts immediately after the first account-start cycle:
        Account 0  = +70 sec
        Account 1  = +72 sec
        ...
        Account 34 = +138 sec

    Each cycle gets its own window_id and window_start_epoch so:
    - a Scout hit in cycle 1 does not permanently block cycle 2
    - a CVS stop during cycle 1 does not incorrectly stop cycle 2
    - existing Scout/CVS coordination remains unchanged

    All scheduling is explicitly Asia/Kolkata.
    """
    if account_position < 0 or account_count <= 0:
        return None

    now = datetime.now(IST)
    candidates = []

    # Previous hour is needed for the :59:55 window,
    # including cycle 2, which carries into the next hour.
    for hours_back in (0, 1):
        hour_base = (
            now - timedelta(hours=hours_back)
        ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        for minute, second in SCOUT_STARTS:
            anchor_start = hour_base.replace(
                minute=minute,
                second=second,
            )

            for cycle_number in range(
                1,
                SCOUT_CYCLES + 1,
            ):
                cycle_offset_seconds = (
                    (cycle_number - 1)
                    * account_count
                    * SCOUT_SPACING_SECONDS
                )

                cycle_start = anchor_start + timedelta(
                    seconds=cycle_offset_seconds
                )

                due = cycle_start + timedelta(
                    seconds=account_position
                    * SCOUT_SPACING_SECONDS
                )

                lateness = (now - due).total_seconds()

                if (
                    0 <= lateness
                    <= SCOUT_DUE_TOLERANCE_SECONDS
                ):
                    window_id = (
                        f"{anchor_start.strftime('%Y%m%d-%H%M%S')}"
                        f"-c{cycle_number}"
                    )

                    if window_id != last_window_id:
                        candidates.append(
                            (
                                lateness,
                                window_id,
                                cycle_start.timestamp(),
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
def get_due_consular_scout_window(
    account_position: int,
    account_count: int,
    last_window_id: str,
):
    """
    Return the Consular Scout window due NOW for this account.

    This intentionally mirrors the existing OFC Scout scheduling,
    but uses its own namespace and constants.

    Consular release coverage:

        :25:55
        :29:55
        :55:55
        :59:55

    Each anchor runs two cycles with the existing fleet-style
    account staggering.

    The window IDs are prefixed with "consular-" so they can
    never collide with normal OFC Scout window IDs.

    Scheduling is explicitly Asia/Kolkata.
    """

    if account_position < 0 or account_count <= 0:
        return None

    now = datetime.now(IST)

    candidates = []

    # Previous hour is required for the :59:55 anchor because
    # the two cycles can continue into the next hour.
    for hours_back in (0, 1):
        hour_base = (
            now - timedelta(hours=hours_back)
        ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        for minute, second in CONSULAR_SCOUT_STARTS:
            anchor_start = hour_base.replace(
                minute=minute,
                second=second,
            )

            for cycle_number in range(
                1,
                CONSULAR_SCOUT_CYCLES + 1,
            ):
                cycle_offset_seconds = (
                    (cycle_number - 1)
                    * account_count
                    * CONSULAR_SCOUT_SPACING_SECONDS
                )

                cycle_start = anchor_start + timedelta(
                    seconds=cycle_offset_seconds
                )

                due = cycle_start + timedelta(
                    seconds=(
                        account_position
                        * CONSULAR_SCOUT_SPACING_SECONDS
                    )
                )

                lateness = (
                    now - due
                ).total_seconds()

                if (
                    0 <= lateness
                    <= CONSULAR_SCOUT_DUE_TOLERANCE_SECONDS
                ):
                    window_id = (
                        "consular-"
                        f"{anchor_start.strftime('%Y%m%d-%H%M%S')}"
                        f"-c{cycle_number}"
                    )

                    if window_id != last_window_id:
                        candidates.append(
                            (
                                lateness,
                                window_id,
                                cycle_start.timestamp(),
                                due,
                            )
                        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    _, window_id, start_epoch, due = candidates[0]

    return {
        "window_id": window_id,
        "window_start_epoch": start_epoch,
        "due": due,
    }