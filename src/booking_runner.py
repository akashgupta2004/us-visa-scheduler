"""
=============================================================
  Booking Runner — OFC Appointment Trigger (Extension-Delegated)
  ─────────────────────────────────────────────────────────
  Connects to an authenticated Chrome session, parks on the portal,
  and watches a shared state file for a 'pending' trigger from the
  monitor. Uses 'extension_running' flag to signal busy state.

  State file: src/state_<customer>.json
  Schema:
    {
      "extension_running": false,   <- managed by this runner
      "pending": false,             <- set by monitor, cleared here
      "ofcCities": [...],
      "ofcStartDate": "...",
      "ofcEndDate": "...",
      "consularCities": [...],
      "consularStartDate": "...",
      "consularEndDate": "...",
      "customer_name": "..."
    }
=============================================================
"""

import asyncio
import json
import os
import sys
import time
import logging
import random
import argparse
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Ensure project root is on the path for top-level imports (slack.py) and src.* imports
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.auth.browser import connect_to_chrome
from src.booking.cdp_client import ensure_on_portal
from src.booking.executor import (
    trigger_extension_booking,
    trigger_extension_reschedule,
    trigger_extension_sniper_consular_only,
    trigger_extension_consular_scout,
    trigger_extension_consular_scout_slots,
)
from src.common.utils import safe_id
from src.common.state import (
    read_state as _read_state,
    write_state as _write_state,
    update_state as _update_state,
    try_queue_local_trigger,
)
from src.common.config import ACCOUNTS_FILE
from slack import (
    send_slack,
    send_ofc_booked_alert,
    send_full_booking_to_ofc,
)
from src.common.db_logger import MongoDBHandler, MongoDBLogger

# Add new imports for recovery
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question
from src.polling_runner import fetch_dates_via_browser
from src.common.scout_state import (
    get_due_scout_window,
    is_window_stopped,
    claim_scout_hit,
    get_due_consular_scout_window,
    claim_consular_scout_hit,
    CONSULAR_SCOUT_STARTS,
    CONSULAR_SCOUT_CYCLES,
)

load_dotenv()

POLL_INTERVAL = 0.01   # seconds between state file checks
# ── Consular Scout protection ─────────────────────────────────────────────────
#
# These values DO NOT change the existing OFC hold behaviour.
# The existing 50-minute WAIT MODE remains authoritative.
CONSULAR_SCOUT_HOLD_SECONDS = 50 * 60

# A Scout-only 429 should not put the account into normal booking rest.
# It only suppresses additional Scout probes briefly.
CONSULAR_SCOUT_RATE_LIMIT_BACKOFF_SECONDS = 90

# Match the existing OFC Scout city-to-city pacing.
CONSULAR_SCOUT_CITY_GAP_SECONDS = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOOKING_RUNNER] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        MongoDBHandler()
    ],
)
log = logging.getLogger("booking_runner")
db_logger = MongoDBLogger()

# Keep background Slack tasks alive until they finish.
_background_tasks = set()


def _queue_background_task(coroutine):
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _send_ofc_alert_in_background(
    state_file: Path,
    customer: str,
    booked_ofc_date: str,
    priority_city: str,
    alert_key: str,
):
    """
    Send Slack in a separate thread so it cannot delay Consular handling.
    """
    try:
        sent = await asyncio.to_thread(
            send_ofc_booked_alert,
            customer,
            booked_ofc_date,
            priority_city,
        )

        if sent:
            _update_state(
                state_file,
                {
                    "lastOfcBookedAlertKey": alert_key,
                    "ofcBookedAlertQueuedKey": None,
                },
            )

            log.info(
                f"✅ OFC-booked Slack alert sent for "
                f"'{customer}' ({booked_ofc_date})."
            )
        else:
            _update_state(
                state_file,
                {"ofcBookedAlertQueuedKey": None},
            )

            log.warning(
                f"⚠️ OFC-booked Slack alert was not sent for "
                f"'{customer}', but booking flow was unaffected."
            )

    except Exception as error:
        _update_state(
            state_file,
            {"ofcBookedAlertQueuedKey": None},
        )

        log.warning(
            f"⚠️ OFC-booked Slack alert failed for "
            f"'{customer}', but booking flow was unaffected: "
            f"{error}"
        )


def _get_booking_rest_seconds() -> float:
    """
    Read the common booking-rest duration.

    Supports the old rest_hours value for backward compatibility.
    """
    polling_state_file = (
        Path(__file__).parent / "polling_state.json"
    )
    default_minutes = 60.0

    if not polling_state_file.exists():
        return default_minutes * 60

    try:
        data = json.loads(
            polling_state_file.read_text(encoding="utf-8")
        )

        if "rest_minutes" in data:
            minutes = float(
                data.get("rest_minutes", default_minutes)
            )
        elif "rest_hours" in data:
            minutes = float(
                data.get("rest_hours", 1.0)
            ) * 60
        else:
            minutes = default_minutes

        return max(minutes, 0) * 60

    except Exception:
        return default_minutes * 60


def _enter_booking_rest(
    state_file: Path,
    customer: str,
    reason: str,
) -> float:
    """
    Block CVS and self-booking while background polling continues.
    """
    rest_seconds = _get_booking_rest_seconds()
    now = time.time()
    rest_until = now + rest_seconds
    rest_minutes = rest_seconds / 60

    _update_state(
        state_file,
        {
            "rest_until": rest_until,
            "pending": False,
            "extension_running": False,
            "last_booking_failure_at": now,
            "last_booking_failure_reason": str(reason)[:500],
        },
    )

    log.warning(
        f"💤 Booking failed for '{customer}'. "
        f"Booking rest active for {rest_minutes:g} minute(s), "
        f"until {datetime.fromtimestamp(rest_until).strftime('%H:%M:%S')}. "
        f"Background polling remains active."
    )

    return rest_until

# ─── State file helpers (wrappers around shared state module) ─────────────────


class SessionExpiredError(RuntimeError):
    """Raised when the authenticated browser session has silently expired
    (OFC date fetch returns the login HTML page instead of JSON).
    The booking runner main loop catches this and runs recover_session()."""


# ─── Session Recovery ─────────────────────────────────────────────────────────

def _load_account_config(username: str) -> dict:
    """Load the account config from accounts.json."""
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        for c in raw:
            if c.get("username") == username:
                return c
    except Exception:
        pass
    return {}

def _match_polled_ofc_dates(results: dict, config: dict) -> tuple[bool, str, str]:
    """Check if any polled OFC dates match the account's criteria."""
    ofc_cities = []
    for c in config.get("ofcCities", []):
        c_upper = c.upper()
        if c_upper == "DELHI":
            ofc_cities.append("NEW DELHI")
        else:
            ofc_cities.append(c_upper)
    start = config.get("ofcStartDate", "")
    end = config.get("ofcEndDate", "")
    
    # Apply prevent_immediate constraint if enabled
    if config.get("prevent_immediate"):
        dynamic_start = (datetime.today() + timedelta(days=3)).strftime("%Y-%m-%d")
        if not start or start < dynamic_start:
            start = dynamic_start
    
    if not ofc_cities or not start or not end:
        return False, "", ""
        
    for city, dates in results.items():
        if city.upper() not in ofc_cities:
            continue
        if not isinstance(dates, list):
            continue
        for d in dates:
            date_str = d.get("Date", "")
            if start <= date_str <= end:
                return True, city, date_str
                
    return False, "", ""
def _get_scout_position(username: str) -> tuple[int, int]:
    """
    Stable local scout order from enabled OFC-capable accounts.

    RESCHEDULE_CONSULAR-only accounts are excluded because this
    scout is exclusively for OFC detection.
    """
    if not ACCOUNTS_FILE.exists():
        return -1, 0

    try:
        raw = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return -1, 0

    scout_accounts = []

    for account in raw:
        if not account.get("enabled", True):
            continue

        acct_username = str(
            account.get("username", "")
        ).strip()

        if not acct_username:
            continue

        action_mode = str(
            account.get("action_mode", "SNIPER")
        ).strip().upper()

        if action_mode == "RESCHEDULE_CONSULAR":
            continue

        if not account.get("ofcCities"):
            continue

        if not account.get("ofcStartDate"):
            continue

        if not account.get("ofcEndDate"):
            continue

        scout_accounts.append(acct_username)

    try:
        return scout_accounts.index(username), len(scout_accounts)
    except ValueError:
        return -1, len(scout_accounts)

def _normalize_consular_scout_city(city: str) -> str:
    """
    Normalize city names exactly for Consular Scout matching.

    Keep DELHI / NEW DELHI equivalent without changing the
    configured account values.
    """
    normalized = str(city or "").strip().upper()

    if normalized == "DELHI":
        return "NEW DELHI"

    return normalized


def _get_consular_scout_position(
    username: str,
) -> tuple[int, int]:
    """
    Stable local Consular Scout order.

    IMPORTANT:
    This roster includes BOTH:
      - normal SNIPER accounts
      - RESCHEDULE_FULL accounts

    RESCHEDULE_CONSULAR is intentionally excluded because it is
    the standalone Consular-reschedule flow and does not represent
    the temporary post-OFC waitingForConsular state we are scouting.

    We deliberately use a stable config-based roster rather than
    constantly rebuilding the order from only currently-waiting
    accounts. That prevents account positions from shifting while
    a Scout cycle is already running.
    """
    if not ACCOUNTS_FILE.exists():
        return -1, 0

    try:
        raw = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return -1, 0

    scout_accounts = []

    for account in raw:
        if not account.get("enabled", True):
            continue

        acct_username = str(
            account.get("username", "")
        ).strip()

        if not acct_username:
            continue

        action_mode = str(
            account.get("action_mode", "SNIPER")
        ).strip().upper()

        # Consular Scout applies only to:
        #
        #   SNIPER
        #       -> later SNIPER_CONSULAR_ONLY
        #
        #   RESCHEDULE_FULL
        #       -> later RESCHEDULE_FULL_CONSULAR_ONLY
        #
        if action_mode not in (
            "SNIPER",
            "RESCHEDULE_FULL",
        ):
            continue

        if not account.get("consularCities") and not account.get(
            "consular_fallback",
            False,
        ):
            continue

        # Normal accounts still require configured Consular bounds.
        #
        # Fallback accounts do NOT require these fields because
        # _get_consular_scout_criteria() dynamically expands them to:
        #
        #   start = booked OFC date + 1 day
        #   end   = today + 365 days
        #
        # and allows all five Consular cities.
        if not account.get("consular_fallback", False):
            if not account.get("consularStartDate"):
                continue

            if not account.get("consularEndDate"):
                continue

        scout_accounts.append(acct_username)

    try:
        return (
            scout_accounts.index(username),
            len(scout_accounts),
        )
    except ValueError:
        return -1, len(scout_accounts)


def _consular_scout_hold_is_active(
    state: dict,
) -> bool:
    """
    Consular Scout may operate ONLY while the existing temporary
    OFC hold is genuinely active.

    This function NEVER modifies state.
    """
    if not state.get("waitingForConsular"):
        return False

    if not state.get("bookedOfcDate"):
        return False

    if state.get("completed"):
        return False

    try:
        wait_start = float(
            state.get("waitStartTime") or 0
        )
    except (TypeError, ValueError):
        return False

    if not wait_start:
        return False

    hold_age = time.time() - wait_start

    return (
        0 <= hold_age
        < CONSULAR_SCOUT_HOLD_SECONDS
    )


def _get_consular_scout_criteria(
    config: dict,
    booked_ofc_date: str,
) -> tuple[list[str], str, str]:
    """
    Mirror the EXISTING CVS post-OFC Consular criteria exactly.

    Normal flow:
        configured cities
        max(configured Consular start,
            prevent_immediate,
            booked OFC + 1 day)
        configured Consular end

    consular_fallback=True:
        all five cities
        booked OFC + 1 day
        today + 365 days
    """
    booked_dt = None

    try:
        if booked_ofc_date:
            booked_dt = datetime.strptime(
                str(booked_ofc_date)[:10],
                "%Y-%m-%d",
            )
    except (TypeError, ValueError):
        booked_dt = None

    if config.get("consular_fallback", False):
        cities = [
            "CHENNAI",
            "MUMBAI",
            "HYDERABAD",
            "NEW DELHI",
            "KOLKATA",
        ]

        if booked_dt:
            start_dt = booked_dt + timedelta(days=1)
        else:
            start_dt = datetime.today().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

        end_dt = (
            datetime.today()
            + timedelta(days=365)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        return (
            cities,
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
        )

    cities = []

    for city in config.get(
        "consularCities",
        [],
    ):
        normalized = (
            _normalize_consular_scout_city(city)
        )

        if (
            normalized
            and normalized not in cities
        ):
            cities.append(normalized)

    start = str(
        config.get(
            "consularStartDate",
            "",
        )
        or ""
    )[:10]

    end = str(
        config.get(
            "consularEndDate",
            "",
        )
        or ""
    )[:10]

    # Preserve existing prevent_immediate behaviour.
    if config.get("prevent_immediate"):
        dynamic_start = (
            datetime.today()
            + timedelta(days=3)
        ).strftime("%Y-%m-%d")

        if not start or start < dynamic_start:
            start = dynamic_start

    # Existing post-OFC rule:
    # Consular must be at least one day AFTER OFC.
    if booked_dt:
        minimum_consular_date = (
            booked_dt
            + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        if (
            not start
            or start < minimum_consular_date
        ):
            start = minimum_consular_date

    return cities, start, end


def _get_consular_scout_assigned_city(
    config: dict,
    booked_ofc_date: str,
    account_position: int,
    window_id: str,
) -> str:
    """
    Assign ONE Consular city per account per Scout cycle.

    Rotate across BOTH:
      - Scout anchor windows
      - cycle 1 / cycle 2

    This prevents the same account from repeatedly checking only
    the same two cities throughout its temporary OFC hold.
    """
    cities, _, _ = (
        _get_consular_scout_criteria(
            config,
            booked_ofc_date,
        )
    )

    if not cities:
        return ""

    cycle_number = 1

    try:
        cycle_number = int(
            str(window_id).rsplit(
                "-c",
                1,
            )[-1]
        )
    except (TypeError, ValueError):
        cycle_number = 1

    cycle_offset = max(
        cycle_number - 1,
        0,
    )

    # Example window:
    # consular-20260828-112555-c1
    #
    # Extract 112555 -> minute=25, second=55,
    # then determine which configured Scout anchor this is.
    anchor_offset = 0

    try:
        anchor_hhmmss = (
            str(window_id)
            .rsplit("-c", 1)[0]
            .rsplit("-", 1)[-1]
        )

        anchor_minute = int(anchor_hhmmss[2:4])
        anchor_second = int(anchor_hhmmss[4:6])

        anchor_offset = list(
            CONSULAR_SCOUT_STARTS
        ).index(
            (
                anchor_minute,
                anchor_second,
            )
        )

    except (
        ValueError,
        TypeError,
        IndexError,
    ):
        anchor_offset = 0

    city_index = (
        account_position
        + (
            anchor_offset
            * CONSULAR_SCOUT_CYCLES
        )
        + cycle_offset
    ) % len(cities)

    return cities[city_index]
def _get_consular_scout_city_order(
    config: dict,
    booked_ofc_date: str,
    account_position: int,
    window_id: str,
) -> list[str]:
    """
    Sweep all effective Consular cities during this account's Scout turn.

    Preserve the existing distributed city assignment as the
    STARTING city so different accounts do not all hit the same
    city at the same instant.

    Fallback accounts will sweep all five cities.
    Normal accounts will sweep only their configured Consular cities.
    """
    cities, _, _ = (
        _get_consular_scout_criteria(
            config,
            booked_ofc_date,
        )
    )

    if not cities:
        return []

    first_city = _get_consular_scout_assigned_city(
        config,
        booked_ofc_date,
        account_position,
        window_id,
    )

    if first_city not in cities:
        return cities

    start_index = cities.index(first_city)

    return (
        cities[start_index:]
        + cities[:start_index]
    )

def _get_qualifying_consular_scout_dates(
    city: str,
    dates,
    config: dict,
    booked_ofc_date: str,
) -> list[str]:
    """
    Return ALL qualifying Consular dates for this Scout city.

    This uses exactly the same effective criteria as the
    existing post-OFC Consular flow.

    Dates are de-duplicated and sorted so Scout may try Slots
    for every qualifying date before moving to the next city.
    """
    (
        effective_cities,
        effective_start,
        effective_end,
    ) = _get_consular_scout_criteria(
        config,
        booked_ofc_date,
    )

    normalized_city = (
        _normalize_consular_scout_city(city)
    )

    if normalized_city not in effective_cities:
        return []

    if not effective_start or not effective_end:
        return []

    if not isinstance(dates, list):
        return []

    qualifying_dates = []

    for item in dates:
        if isinstance(item, dict):
            date_str = str(
                item.get("Date")
                or item.get("date")
                or item.get("StartDate")
                or ""
            )[:10]
        else:
            date_str = str(
                item or ""
            )[:10]

        if (
            date_str
            and effective_start
            <= date_str
            <= effective_end
            and date_str not in qualifying_dates
        ):
            qualifying_dates.append(
                date_str
            )

    qualifying_dates.sort()

    return qualifying_dates


def _match_consular_scout_dates(
    city: str,
    dates,
    config: dict,
    booked_ofc_date: str,
) -> tuple[bool, str]:
    """
    Backward-compatible single-date matcher used by the
    existing broadcast/eligibility paths.

    Consular Scout itself uses
    _get_qualifying_consular_scout_dates() so it can test Slots
    for every qualifying date.
    """
    qualifying_dates = (
        _get_qualifying_consular_scout_dates(
            city,
            dates,
            config,
            booked_ofc_date,
        )
    )

    if not qualifying_dates:
        return False, ""

    return True, qualifying_dates[0]
async def _broadcast_scout_hit(
    matched_city: str,
    matched_date: str,
    detected_by: str,
    window_id: str,
    only_username: str = "",
    exclude_username: str = "",
    scout_fastpath=None,
):
    """
    Queue the normal OFC booking flow for every qualifying account.

    Existing try_queue_local_trigger() remains the authority for
    pending/running/rest/completed protection.
    """
    if not ACCOUNTS_FILE.exists():
        return 0

    try:
        all_accounts = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        log.error(
            f"[SCOUT] Could not read accounts.json: {exc}"
        )
        return 0

    candidate_results = {
        matched_city: [
            {
                "Date": matched_date,
            }
        ]
    }

    triggered_count = 0

    for acct_config in all_accounts:
        if not acct_config.get("enabled", True):
            continue

        acct_username = str(
            acct_config.get("username", "")
        ).strip()

        if not acct_username:
            continue

        # Optional scout targeting:
        # - only_username queues only the detecting account
        # - exclude_username broadcasts to everyone except detector
        if only_username and acct_username != only_username:
            continue

        if exclude_username and acct_username == exclude_username:
            continue

        acct_customer = str(
            acct_config.get("customer_name", "")
        ).strip() or acct_username

        action_mode = str(
            acct_config.get("action_mode", "SNIPER")
        ).strip().upper()

        # Consular-only accounts must not receive an OFC trigger.
        if action_mode == "RESCHEDULE_CONSULAR":
            continue

        matched, _, _ = _match_polled_ofc_dates(
            candidate_results,
            acct_config,
        )

        if not matched:
            continue

        acct_uid = safe_id(acct_username)
        acct_state_file = (
            Path(__file__).parent
            / f"state_{acct_uid}.json"
        )

        acct_state = _read_state(acct_state_file)

        # An account already holding OFC and waiting for Consular
        # must remain completely untouched.
        if acct_state.get("waitingForConsular"):
            continue

        action_type = (
            "RESCHEDULE_FULL"
            if action_mode == "RESCHEDULE_FULL"
            else "SNIPER"
        )

        trigger_key = (
            f"scout|{window_id}|{acct_uid}|"
            f"{matched_city.upper()}|"
            f"{matched_date}|{action_type}"
        )

        trigger_updates = {
            "pending": True,
            "trigger_timestamp": time.time(),
            "trigger_key": trigger_key,
            "action_type": action_type,
            "ofcCities": acct_config.get(
                "ofcCities", []
            ),
            "ofcPriorityCity": matched_city,
            "ofcPriorityDate": matched_date,
            "ofcStartDate": acct_config.get(
                "ofcStartDate", ""
            ),
            "ofcEndDate": acct_config.get(
                "ofcEndDate", ""
            ),
            "consularCities": acct_config.get(
                "consularCities", []
            ),
            "consularPriorityCity": acct_config.get(
                "consularPriorityCity", ""
            ),
            "consularStartDate": acct_config.get(
                "consularStartDate", ""
            ),
            "consularEndDate": acct_config.get(
                "consularEndDate", ""
            ),
            "customer_name": acct_customer,
            "prevent_immediate": acct_config.get(
                "prevent_immediate", False
            ),
            "multiPerson": acct_config.get(
                "multiPerson", False
            ),
        }

        # Scout fast-path data belongs ONLY to the detector account.
        #
        # Dates token remains available as the compatibility fallback:
        #
        #   preserved Slots -> Book
        #       ↓ non-fatal failure
        #   preserved Dates token -> Slots -> Book
        #
        # Fleet accounts never receive another account's tokens/slots.
        if only_username and scout_fastpath:
            scout_token = str(
                scout_fastpath.get(
                    "token",
                    "",
                )
                or ""
            ).strip()

            scout_slots = (
                scout_fastpath.get(
                    "slots",
                    [],
                )
                or []
            )

            scout_slots_token = str(
                scout_fastpath.get(
                    "slotsToken",
                    "",
                )
                or ""
            ).strip()

            fastpath_updates = {}

            if scout_token:
                fastpath_updates.update(
                    {
                        "scoutOfcToken": scout_token,
                        "scoutOfcAppd": str(
                            scout_fastpath.get(
                                "appd",
                                "",
                            )
                            or ""
                        ).strip(),
                        "scoutOfcTokenCity": (
                            matched_city
                        ),
                        "scoutOfcTokenDate": (
                            matched_date
                        ),
                        "scoutOfcTokenIsReschedule": bool(
                            scout_fastpath.get(
                                "isReschedule",
                                False,
                            )
                        ),
                        "scoutOfcTokenCapturedAt": int(
                            scout_fastpath.get(
                                "capturedAt",
                                0,
                            )
                            or 0
                        ),
                    }
                )

            if (
                isinstance(scout_slots, list)
                and scout_slots
                and scout_slots_token
            ):
                fastpath_updates.update(
                    {
                        "scoutOfcSlots": (
                            scout_slots
                        ),
                        "scoutOfcSlotsToken": (
                            scout_slots_token
                        ),
                        "scoutOfcSlotsCapturedAt": int(
                            scout_fastpath.get(
                                "slotsCapturedAt",
                                0,
                            )
                            or 0
                        ),
                    }
                )

            if fastpath_updates:
                trigger_updates.update(
                    fastpath_updates
                )


        queued, reason = try_queue_local_trigger(
            acct_state_file,
            trigger_updates,
        )

        if queued:
            triggered_count += 1
            log.info(
                f"[SCOUT] ⚡ Queued {acct_customer} for "
                f"{matched_city} {matched_date}"
            )
        else:
            log.info(
                f"[SCOUT] ⏭️ {acct_customer} not queued: "
                f"{reason}"
            )

    log.info(
        f"[SCOUT] 🚀 {matched_city} {matched_date} detected "
        f"by {detected_by}; queued "
        f"{triggered_count} qualifying account(s)."
    )
    return triggered_count

async def _broadcast_consular_scout_hit(
    matched_city: str,
    matched_date: str,
    detected_by: str,
    window_id: str,
    only_username: str = "",
    exclude_username: str = "",
    scout_fastpath=None,
):
    """
    Send a detected Consular opportunity to every eligible
    waitingForConsular account.

    IMPORTANT:
    - OFC hold must already be active.
    - Existing try_queue_local_trigger() remains the authority.
    - If an account is already doing SNIPER_CONSULAR_ONLY,
      try_queue_local_trigger() uses the existing
      priority-update mechanism instead of starting a second scan.
    - Supports both normal and RESCHEDULE_FULL post-OFC flows.
    """
    if not ACCOUNTS_FILE.exists():
        return 0

    try:
        all_accounts = json.loads(
            ACCOUNTS_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        log.error(
            f"[CONSULAR-SCOUT] Could not read "
            f"accounts.json: {exc}"
        )
        return 0

    triggered_count = 0

    for acct_config in all_accounts:
        if not acct_config.get(
            "enabled",
            True,
        ):
            continue

        acct_username = str(
            acct_config.get(
                "username",
                "",
            )
        ).strip()

        if not acct_username:
            continue

        if (
            only_username
            and acct_username != only_username
        ):
            continue

        if (
            exclude_username
            and acct_username == exclude_username
        ):
            continue

        action_mode = str(
            acct_config.get(
                "action_mode",
                "SNIPER",
            )
        ).strip().upper()

        # This Scout is ONLY for post-OFC waiting mode.
        if action_mode not in (
            "SNIPER",
            "RESCHEDULE_FULL",
        ):
            continue

        acct_uid = safe_id(
            acct_username
        )

        acct_state_file = (
            Path(__file__).parent
            / f"state_{acct_uid}.json"
        )

        acct_state = _read_state(
            acct_state_file
        )

        # Absolute protection:
        # only accounts genuinely holding OFC may receive this.
        if not _consular_scout_hold_is_active(
            acct_state
        ):
            continue

        booked_ofc_date = str(
            acct_state.get(
                "bookedOfcDate",
                "",
            )
            or ""
        )

        matched, _ = (
            _match_consular_scout_dates(
                matched_city,
                [
                    {
                        "Date": matched_date,
                    }
                ],
                acct_config,
                booked_ofc_date,
            )
        )

        if not matched:
            continue

        (
            effective_cities,
            effective_start,
            effective_end,
        ) = _get_consular_scout_criteria(
            acct_config,
            booked_ofc_date,
        )

        action_type = (
            "RESCHEDULE_FULL_CONSULAR_ONLY"
            if action_mode == "RESCHEDULE_FULL"
            else "SNIPER_CONSULAR_ONLY"
        )

        detected_at = time.time()

        trigger_key = (
            f"consular-scout|{window_id}|"
            f"{acct_uid}|"
            f"{matched_city.upper()}|"
            f"{matched_date}|"
            f"{action_type}"
        )

        trigger_updates = {
            "pending": True,
            "trigger_timestamp": detected_at,
            "trigger_key": trigger_key,
            "triggerSource": "CONSULAR_SCOUT",
            "action_type": action_type,

            "targetConsularCity": (
                matched_city
            ),
            "targetConsularDate": (
                matched_date
            ),
            "targetConsularDetectedAt": (
                detected_at
            ),
            "consularPriorityUpdatedAt": (
                detected_at
            ),

            "consularCities": (
                effective_cities
            ),
            "consularPriorityCity": (
                matched_city
            ),
            "consularStartDate": (
                effective_start
            ),
            "consularEndDate": (
                effective_end
            ),

            "customer_name": str(
                acct_config.get(
                    "customer_name",
                    "",
                )
            ).strip() or acct_username,

            "prevent_immediate": (
                acct_config.get(
                    "prevent_immediate",
                    False,
                )
            ),
            "multiPerson": (
                acct_config.get(
                    "multiPerson",
                    False,
                )
            ),
        }
        # Consular Scout token fast-path is ONLY for the
        # detector account that made this exact Dates request.
        #
        # Fleet accounts must perform their own normal
        # Consular Dates request and must never reuse another
        # account's token.
        if only_username and scout_fastpath:
            scout_token = str(
                scout_fastpath.get(
                    "token",
                    "",
                )
                or ""
            ).strip()

            if scout_token:
                trigger_updates.update(
                    {
                        "scoutConsularToken": (
                            scout_token
                        ),
                        "scoutConsularPrimaryId": str(
                            scout_fastpath.get(
                                "primaryId",
                                "",
                            )
                            or ""
                        ).strip(),
                        "scoutConsularAppd": str(
                            scout_fastpath.get(
                                "appd",
                                "",
                            )
                            or ""
                        ).strip(),
                        "scoutConsularApplications": (
                            scout_fastpath.get(
                                "applications",
                                [],
                            )
                            or []
                        ),
                        "scoutConsularTokenCity": (
                            matched_city
                        ),
                        "scoutConsularTokenDate": (
                            matched_date
                        ),
                        "scoutConsularTokenIsReschedule": bool(
                            scout_fastpath.get(
                                "isReschedule",
                                False,
                            )
                        ),
                        "scoutConsularTokenCapturedAt": int(
                            scout_fastpath.get(
                                "capturedAt",
                                0,
                            )
                            or 0
                        ),

                        # Exact viable Slots returned immediately
                        # after this detector's Dates HIT.
                        "scoutConsularSlots": (
                            scout_fastpath.get(
                                "slots",
                                [],
                            )
                            or []
                        ),
                        "scoutConsularSlotsToken": str(
                            scout_fastpath.get(
                                "slotsToken",
                                "",
                            )
                            or ""
                        ).strip(),
                        "scoutConsularSlotsCapturedAt": int(
                            scout_fastpath.get(
                                "slotsCapturedAt",
                                0,
                            )
                            or 0
                        ),
                    }
                )
        queued, reason = (
            try_queue_local_trigger(
                acct_state_file,
                trigger_updates,
            )
        )

        if queued:
            triggered_count += 1

            if reason == "priority_updated":
                log.info(
                    f"[CONSULAR-SCOUT] ⚡ "
                    f"Updated active Consular scan for "
                    f"{acct_username}: "
                    f"{matched_city} {matched_date}"
                )
            else:
                log.info(
                    f"[CONSULAR-SCOUT] ⚡ Queued "
                    f"{acct_username}: "
                    f"{matched_city} {matched_date} "
                    f"[{action_type}]"
                )

        else:
            log.info(
                f"[CONSULAR-SCOUT] ⏭️ "
                f"{acct_username} not queued: "
                f"{reason}"
            )

    log.info(
        f"[CONSULAR-SCOUT] 🚀 "
        f"{matched_city} {matched_date} "
        f"detected by {detected_by}; "
        f"released to {triggered_count} "
        f"eligible WAIT MODE account(s)."
    )

    return triggered_count
async def _try_pre_consular_scout(
    page,
    customer: str,
    username: str,
    state: dict,
    account_position: int,
    account_count: int,
    last_window_id: str,
) -> str:
    """
    Perform a rapid multi-city official Consular Dates Scout sweep.

    Only runs during a valid temporary OFC hold.

    Scout itself:
        - NEVER clears waitingForConsular
        - NEVER clears bookedOfcDate
        - NEVER clears waitStartTime
        - NEVER enters normal booking rest

    Each scheduled account starts from its existing assigned city,
    then sweeps the remaining effective Consular cities with the
    same 1.5-second city gap used by OFC Scout.

    CVS / real booking triggers always pre-empt Scout immediately.
    """

    due = get_due_consular_scout_window(
        account_position,
        account_count,
        last_window_id,
    )

    if not due:
        return last_window_id

    window_id = due["window_id"]

    # Mark immediately so this runner cannot perform
    # the same scheduled Scout turn twice.
    last_window_id = window_id

    live_state_file = (
        Path(__file__).parent
        / f"state_{safe_id(username)}.json"
    )

    # Scout must operate ONLY during the existing OFC hold.
    if not _consular_scout_hold_is_active(
        state
    ):
        return last_window_id

    if (
        state.get("extension_running")
        or state.get("pending")
        or state.get("completed")
    ):
        log.info(
            f"[CONSULAR-SCOUT] ⏭️ "
            f"{customer} busy; Scout sweep skipped."
        )
        return last_window_id

    # Scout-specific rate-limit protection.
    try:
        backoff_until = float(
            state.get(
                "consularScoutBackoffUntil",
                0,
            )
            or 0
        )
    except (TypeError, ValueError):
        backoff_until = 0

    if (
        backoff_until
        and time.time() < backoff_until
    ):
        remaining = int(
            backoff_until - time.time()
        )

        log.info(
            f"[CONSULAR-SCOUT] ⏭️ "
            f"{customer} Scout backoff active "
            f"for another {max(remaining, 0)}s."
        )

        return last_window_id

    my_config = _load_account_config(
        username
    )

    if not my_config:
        return last_window_id

    booked_ofc_date = str(
        state.get(
            "bookedOfcDate",
            "",
        )
        or ""
    )

    scout_cities = (
        _get_consular_scout_city_order(
            my_config,
            booked_ofc_date,
            account_position,
            window_id,
        )
    )

    if not scout_cities:
        return last_window_id

    (
        _,
        effective_start,
        effective_end,
    ) = _get_consular_scout_criteria(
        my_config,
        booked_ofc_date,
    )

    action_mode = str(
        my_config.get(
            "action_mode",
            "SNIPER",
        )
    ).strip().upper()

    is_reschedule = (
        action_mode == "RESCHEDULE_FULL"
    )

    log.info(
        f"[CONSULAR-SCOUT] 🔎 "
        f"{customer} starting rapid Consular Scout sweep "
        f"{scout_cities} "
        f"(position {account_position + 1}/"
        f"{account_count}, window {window_id}, IST, "
        f"reschedule={is_reschedule})."
    )

    for city_index, assigned_city in enumerate(
        scout_cities
    ):
        # -----------------------------------------------------
        # BEFORE EACH CITY
        #
        # CVS / booking always wins and OFC hold must still
        # be active.
        # -----------------------------------------------------
        live_state = _read_state(
            live_state_file
        )

        if live_state.get("pending"):
            log.info(
                f"[CONSULAR-SCOUT] ⚡ "
                f"Booking trigger arrived before "
                f"{customer} could Scout {assigned_city}. "
                "Scout sweep pre-empted immediately."
            )
            return last_window_id

        if not _consular_scout_hold_is_active(
            live_state
        ):
            log.info(
                f"[CONSULAR-SCOUT] ⏭️ "
                f"OFC hold ended during "
                f"{customer}'s Scout sweep."
            )
            return last_window_id

        log.info(
            f"[CONSULAR-SCOUT] 🔎 "
            f"{customer} polling official Consular API "
            f"for {assigned_city} "
            f"({city_index + 1}/{len(scout_cities)}, "
            f"position {account_position + 1}/"
            f"{account_count}, window {window_id}, IST, "
            f"reschedule={is_reschedule})."
        )

        scout_config = {
            "customer_name": customer,
            "city": assigned_city,
            "consularStartDate": (
                effective_start
            ),
            "consularEndDate": (
                effective_end
            ),
            "bookedOfcDate": (
                booked_ofc_date
            ),
            "isReschedule": (
                is_reschedule
            ),
            "multiPerson": bool(
                my_config.get(
                    "multiPerson",
                    False,
                )
            ),
        }

        try:
            scout_task = asyncio.create_task(
                trigger_extension_consular_scout(
                    page,
                    scout_config,
                    log,
                )
            )

            while not scout_task.done():
                live_state = _read_state(
                    live_state_file
                )

                # CVS or any real booking trigger ALWAYS wins.
                if live_state.get("pending"):
                    scout_task.cancel()

                    try:
                        await scout_task
                    except asyncio.CancelledError:
                        pass

                    log.info(
                        f"[CONSULAR-SCOUT] ⚡ "
                        f"Booking trigger arrived while "
                        f"{customer} was scouting. "
                        "Scout pre-empted immediately."
                    )

                    return last_window_id

                # Hold may expire while request is running.
                if not _consular_scout_hold_is_active(
                    live_state
                ):
                    scout_task.cancel()

                    try:
                        await scout_task
                    except asyncio.CancelledError:
                        pass

                    log.info(
                        f"[CONSULAR-SCOUT] ⏭️ "
                        f"OFC hold ended while "
                        f"{customer} was scouting."
                    )

                    return last_window_id

                await asyncio.sleep(
                    POLL_INTERVAL
                )

            result = await scout_task

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.warning(
                f"[CONSULAR-SCOUT] "
                f"Scout request failed for "
                f"{customer} | {assigned_city}: {exc}"
            )

            result = {}

        result = result or {}

        # -----------------------------------------------------
        # SCOUT-ONLY 429
        #
        # Stop this account's entire sweep.
        # Do not enter booking rest.
        # OFC hold and CVS remain untouched.
        # -----------------------------------------------------
        if result.get("rateLimited"):
            backoff_until = (
                time.time()
                + CONSULAR_SCOUT_RATE_LIMIT_BACKOFF_SECONDS
            )

            _update_state(
                live_state_file,
                {
                    "consularScoutBackoffUntil": (
                        backoff_until
                    ),
                    "consularScoutLast429At": (
                        time.time()
                    ),
                },
            )

            log.warning(
                f"[CONSULAR-SCOUT] ⚠️ "
                f"429/rate limit for {customer}. "
                f"Scout-only backoff for "
                f"{CONSULAR_SCOUT_RATE_LIMIT_BACKOFF_SECONDS}s. "
                "Temporary OFC hold and CVS eligibility "
                "remain untouched."
            )

            return last_window_id

        # -----------------------------------------------------
        # SCOUT SESSION EXPIRY
        #
        # Recover immediately.
        # Preserve OFC hold.
        # After successful recovery, continue with the NEXT city.
        # -----------------------------------------------------
        if result.get("sessionExpired"):
            log.warning(
                f"[CONSULAR-SCOUT] ⚠️ "
                f"Session expired for {customer}. "
                "Preserving temporary OFC hold and "
                "attempting in-place recovery."
            )

            recovered = await recover_session(
                page,
                customer,
                username,
            )

            # ABSOLUTELY DO NOT clear:
            #
            # waitingForConsular
            # bookedOfcDate
            # waitStartTime
            #
            _update_state(
                live_state_file,
                {
                    "pending": False,
                    "extension_running": False,
                    "consularScoutLastSessionExpiryAt": (
                        time.time()
                    ),
                },
            )

            if not recovered:
                log.warning(
                    f"[CONSULAR-SCOUT] ⚠️ "
                    f"Session recovery failed for {customer}. "
                    "OFC hold state is still preserved."
                )

                return last_window_id

            log.info(
                f"[CONSULAR-SCOUT] ✅ "
                f"Session recovered for {customer}. "
                "OFC hold preserved; resuming Scout sweep."
            )

            live_state = _read_state(
                live_state_file
            )

            if live_state.get("pending"):
                return last_window_id

            if not _consular_scout_hold_is_active(
                live_state
            ):
                return last_window_id

            # Session recovered successfully.
            # Continue directly to the next city.
            continue

        # -----------------------------------------------------
        # 500 / 524 / Access Denied / other non-usable result
        #
        # Do NOT kill the whole sweep.
        # Continue to another city.
        # -----------------------------------------------------
        if not result.get("success"):
            log.info(
                f"[CONSULAR-SCOUT] "
                f"No usable Scout result for "
                f"{customer} in {assigned_city}. "
                "Continuing Scout sweep."
            )

        else:
            result_city = (
                _normalize_consular_scout_city(
                    result.get("city")
                    or assigned_city
                )
            )

            dates = result.get("dates") or []

            qualifying_dates = (
                _get_qualifying_consular_scout_dates(
                    result_city,
                    dates,
                    my_config,
                    booked_ofc_date,
                )
            )

            if not qualifying_dates:
                log.info(
                    f"[CONSULAR-SCOUT] "
                    f"No qualifying Consular date found by "
                    f"{customer} in {result_city}. "
                    "Continuing Scout sweep."
                )

            else:
                log.info(
                    f"[CONSULAR-SCOUT] 📅 "
                    f"{customer} found "
                    f"{len(qualifying_dates)} qualifying "
                    f"date(s) in {result_city}: "
                    f"{qualifying_dates}. "
                    "Checking Slots for each date."
                )

                # =================================================
                # TRY EVERY QUALIFYING DATE IN THIS CITY
                #
                # Slots responses may rotate the token.
                # Carry the newest token into the next date.
                # =================================================
                rolling_token = str(
                    result.get(
                        "token",
                        "",
                    )
                    or ""
                ).strip()

                rolling_token_captured_at = int(
                    result.get(
                        "capturedAt",
                        0,
                    )
                    or 0
                )

                if not rolling_token:
                    log.warning(
                        f"[CONSULAR-SCOUT] "
                        f"{customer} received qualifying Dates "
                        f"for {result_city}, but the Dates response "
                        "did not contain a usable token. "
                        "Continuing to next Scout city."
                    )
                    continue

                for (
                    date_index,
                    matched_date,
                ) in enumerate(
                    qualifying_dates
                ):
                    # CVS / real booking ALWAYS wins.
                    live_state = _read_state(
                        live_state_file
                    )

                    if live_state.get("pending"):
                        log.info(
                            f"[CONSULAR-SCOUT] ⚡ "
                            f"Booking trigger arrived before "
                            f"{customer} could check Slots for "
                            f"{result_city} {matched_date}. "
                            "Scout pre-empted immediately."
                        )

                        return last_window_id

                    if not _consular_scout_hold_is_active(
                        live_state
                    ):
                        return last_window_id

                    scout_applications = (
                        result.get(
                            "applications",
                            [],
                        )
                        or []
                    )

                    scout_number_of_people = 1

                    if my_config.get(
                        "multiPerson",
                        False,
                    ):
                        scout_number_of_people = max(
                            1,
                            len(scout_applications),
                        )

                    # Preserve the exact token/context used for
                    # THIS date's Slots request.
                    token_for_this_date = (
                        rolling_token
                    )

                    token_for_this_date_captured_at = (
                        rolling_token_captured_at
                    )

                    slots_config = {
                        "date": matched_date,

                        "token": (
                            token_for_this_date
                        ),

                        "appd": str(
                            result.get(
                                "appd",
                                "",
                            )
                            or ""
                        ).strip(),

                        "numberOfPeople": (
                            scout_number_of_people
                        ),

                        "isReschedule": bool(
                            result.get(
                                "isReschedule",
                                is_reschedule,
                            )
                        ),
                    }

                    log.info(
                        f"[CONSULAR-SCOUT] 📅 "
                        f"{customer} checking Slots for "
                        f"{result_city} {matched_date} "
                        f"({date_index + 1}/"
                        f"{len(qualifying_dates)} qualifying dates)."
                    )

                    try:
                        slots_task = asyncio.create_task(
                            trigger_extension_consular_scout_slots(
                                page,
                                slots_config,
                                log,
                            )
                        )

                        while not slots_task.done():
                            live_state = _read_state(
                                live_state_file
                            )

                            # CVS / real booking always pre-empts.
                            if live_state.get("pending"):
                                slots_task.cancel()

                                try:
                                    await slots_task
                                except asyncio.CancelledError:
                                    pass

                                log.info(
                                    f"[CONSULAR-SCOUT] ⚡ "
                                    f"Booking trigger arrived while "
                                    f"{customer} was fetching "
                                    f"Scout Slots for "
                                    f"{result_city} {matched_date}. "
                                    "Scout pre-empted immediately."
                                )

                                return last_window_id

                            if not _consular_scout_hold_is_active(
                                live_state
                            ):
                                slots_task.cancel()

                                try:
                                    await slots_task
                                except asyncio.CancelledError:
                                    pass

                                log.info(
                                    f"[CONSULAR-SCOUT] ⏭️ "
                                    f"OFC hold ended while "
                                    f"{customer} was fetching "
                                    "Scout Slots."
                                )

                                return last_window_id

                            await asyncio.sleep(
                                POLL_INTERVAL
                            )

                        slots_result = await slots_task

                    except asyncio.CancelledError:
                        raise

                    except Exception as exc:
                        log.warning(
                            f"[CONSULAR-SCOUT] "
                            f"Slots request failed for "
                            f"{customer} | "
                            f"{result_city} {matched_date}: "
                            f"{exc}"
                        )

                        slots_result = {}

                    slots_result = (
                        slots_result
                        or {}
                    )

                    # ---------------------------------------------
                    # SLOT-SCOUT 429
                    # ---------------------------------------------
                    if slots_result.get(
                        "rateLimited"
                    ):
                        backoff_until = (
                            time.time()
                            + CONSULAR_SCOUT_RATE_LIMIT_BACKOFF_SECONDS
                        )

                        _update_state(
                            live_state_file,
                            {
                                "consularScoutBackoffUntil": (
                                    backoff_until
                                ),
                                "consularScoutLast429At": (
                                    time.time()
                                ),
                            },
                        )

                        log.warning(
                            f"[CONSULAR-SCOUT] ⚠️ "
                            f"429/rate limit while fetching "
                            f"Slots for {customer}. "
                            f"Scout-only backoff for "
                            f"{CONSULAR_SCOUT_RATE_LIMIT_BACKOFF_SECONDS}s. "
                            "Temporary OFC hold and CVS "
                            "eligibility remain untouched."
                        )

                        return last_window_id

                    # ---------------------------------------------
                    # SLOT-SCOUT SESSION EXPIRY
                    # ---------------------------------------------
                    if slots_result.get(
                        "sessionExpired"
                    ):
                        log.warning(
                            f"[CONSULAR-SCOUT] ⚠️ "
                            f"Session expired while fetching "
                            f"Slots for {customer}. "
                            "Preserving OFC hold and recovering."
                        )

                        recovered = await recover_session(
                            page,
                            customer,
                            username,
                        )

                        _update_state(
                            live_state_file,
                            {
                                "pending": False,
                                "extension_running": False,
                                "consularScoutLastSessionExpiryAt": (
                                    time.time()
                                ),
                            },
                        )

                        if not recovered:
                            log.warning(
                                f"[CONSULAR-SCOUT] ⚠️ "
                                f"Session recovery failed for "
                                f"{customer}. OFC hold preserved."
                            )

                            return last_window_id

                        log.info(
                            f"[CONSULAR-SCOUT] ✅ "
                            f"Session recovered for {customer}. "
                            "Old Dates/Slots token discarded; "
                            "moving to next Scout city."
                        )

                        live_state = _read_state(
                            live_state_file
                        )

                        if live_state.get("pending"):
                            return last_window_id

                        if not _consular_scout_hold_is_active(
                            live_state
                        ):
                            return last_window_id

                        # IMPORTANT:
                        # Dates token came from the expired session.
                        # Do NOT use it for another date.
                        break

                    # ---------------------------------------------
                    # 500 / 524 / timeout / other unusable Slots
                    #
                    # Try the NEXT qualifying date in this SAME city.
                    # ---------------------------------------------
                    if not slots_result.get(
                        "success"
                    ):
                        log.info(
                            f"[CONSULAR-SCOUT] "
                            f"No usable Slots result for "
                            f"{customer} in "
                            f"{result_city} {matched_date}. "
                            "Trying next qualifying date."
                        )

                        continue

                    scout_slots = (
                        slots_result.get(
                            "slots",
                            [],
                        )
                        or []
                    )

                    scout_slots_token = str(
                        slots_result.get(
                            "slotsToken",
                            "",
                        )
                        or ""
                    ).strip()

                    scout_slots_captured_at = int(
                        slots_result.get(
                            "slotsCapturedAt",
                            0,
                        )
                        or 0
                    )

                    # Slots response token becomes the input token
                    # for the NEXT qualifying date.
                    #
                    # This applies even when the current date has
                    # zero viable slots.
                    if scout_slots_token:
                        rolling_token = (
                            scout_slots_token
                        )

                        rolling_token_captured_at = (
                            scout_slots_captured_at
                        )

                    # ---------------------------------------------
                    # ZERO SLOTS
                    #
                    # Continue with next qualifying date in this city.
                    # ---------------------------------------------
                    if (
                        not isinstance(
                            scout_slots,
                            list,
                        )
                        or not scout_slots
                        or not scout_slots_token
                    ):
                        log.info(
                            f"[CONSULAR-SCOUT] "
                            f"{customer} found date "
                            f"{result_city} {matched_date}, "
                            "but Slots returned no viable "
                            "appointment time. "
                            "Trying next qualifying date."
                        )

                        continue

                    # =================================================
                    # CONFIRMED SLOT HIT
                    # =================================================
                    claimed, reason = (
                        claim_consular_scout_hit(
                            window_id,
                            result_city,
                            matched_date,
                            customer,
                        )
                    )

                    if not claimed:
                        log.info(
                            f"[CONSULAR-SCOUT] "
                            f"Slot HIT ignored: {reason} "
                            f"({result_city} {matched_date})."
                        )

                        return last_window_id

                    top_slot = scout_slots[0]

                    log.warning(
                        f"[CONSULAR-SCOUT] 🎯 SLOT HIT: "
                        f"{customer} found "
                        f"{result_city} {matched_date} | "
                        f"{len(scout_slots)} viable slot(s). "
                        f"Top: Time={top_slot.get('Time')}, "
                        f"Available="
                        f"{top_slot.get('EntriesAvailable')}, "
                        f"Num={top_slot.get('Num')}. "
                        "Detector booking gets first priority."
                    )

                    # Preserve BOTH response stages.
                    consular_scout_fastpath = {
                        # Exact token used to obtain the winning
                        # Slots response for this date.
                        "token": (
                            token_for_this_date
                        ),

                        "primaryId": str(
                            result.get(
                                "primaryId",
                                "",
                            )
                            or ""
                        ).strip(),

                        "appd": str(
                            result.get(
                                "appd",
                                "",
                            )
                            or ""
                        ).strip(),

                        "applications": (
                            scout_applications
                        ),

                        "isReschedule": bool(
                            result.get(
                                "isReschedule",
                                is_reschedule,
                            )
                        ),

                        "capturedAt": (
                            token_for_this_date_captured_at
                        ),

                        # Slots-response context
                        "slots": (
                            scout_slots
                        ),

                        "slotsToken": (
                            scout_slots_token
                        ),

                        "slotsCapturedAt": (
                            scout_slots_captured_at
                        ),
                    }

                    # =============================================
                    # DETECTOR FIRST
                    # =============================================
                    detector_count = (
                        await _broadcast_consular_scout_hit(
                            result_city,
                            matched_date,
                            customer,
                            window_id,
                            only_username=username,
                            scout_fastpath=(
                                consular_scout_fastpath
                            ),
                        )
                    )

                    if detector_count > 0:
                        _update_state(
                            live_state_file,
                            {
                                "consular_scout_detector_broadcast_pending": True,
                                "consular_scout_detector_city": (
                                    result_city
                                ),
                                "consular_scout_detector_date": (
                                    matched_date
                                ),
                                "consular_scout_detector_window_id": (
                                    window_id
                                ),
                                "consular_scout_detector_customer": (
                                    customer
                                ),
                            },
                        )

                        log.warning(
                            f"[CONSULAR-SCOUT] 🚀 DETECTOR FIRST: "
                            f"{customer} queued for "
                            f"{result_city} {matched_date} "
                            f"with {len(scout_slots)} preserved "
                            "Scout slot(s). "
                            "Fleet will release only after the "
                            "detector's Consular booking message "
                            "is sent to its extension."
                        )

                    else:
                        log.warning(
                            f"[CONSULAR-SCOUT] ⚠️ "
                            f"Detector {customer} could not be queued. "
                            "Broadcasting immediately to other "
                            "eligible WAIT MODE accounts."
                        )

                        await _broadcast_consular_scout_hit(
                            result_city,
                            matched_date,
                            customer,
                            window_id,
                            exclude_username=username,
                        )

                    # Confirmed SLOT HIT ends the entire Scout sweep.
                # A confirmed SLOT HIT ends this account's Scout sweep.
                return last_window_id

        # -----------------------------------------------------
        # CITY GAP
        #
        # Same pacing as OFC Scout: 1.5 seconds between cities.
        # But do not sleep after the final city.
        #
        # The gap remains fully pre-emptible by CVS.
        # -----------------------------------------------------
        if city_index < len(scout_cities) - 1:
            gap_until = (
                time.time()
                + CONSULAR_SCOUT_CITY_GAP_SECONDS
            )

            while time.time() < gap_until:
                live_state = _read_state(
                    live_state_file
                )

                if live_state.get("pending"):
                    log.info(
                        f"[CONSULAR-SCOUT] ⚡ "
                        f"Booking trigger arrived while "
                        f"{customer} was between Scout cities. "
                        "Scout sweep pre-empted immediately."
                    )

                    return last_window_id

                if not _consular_scout_hold_is_active(
                    live_state
                ):
                    return last_window_id

                await asyncio.sleep(
                    POLL_INTERVAL
                )

    return last_window_id
async def _try_pre_cvs_scout(
    page,
    customer: str,
    username: str,
    state: dict,
    account_position: int,
    account_count: int,
    last_window_id: str,
) -> str:
    """
    Perform this account's single assigned official OFC scout check.

    The scout never alters normal polling cooldowns.
    """
    
    due = get_due_scout_window(
        account_position,
        account_count,
        last_window_id,
    )   

    if not due:
        return last_window_id

    window_id = due["window_id"]
    window_start_epoch = due["window_start_epoch"]

    # Mark locally immediately so this process can never poll twice
    # in the same scout window.
    last_window_id = window_id

    if is_window_stopped(
        window_id,
        window_start_epoch,
    ):
        return last_window_id

    # Never interfere with active/pending bookings or Consular wait.
    if (
        state.get("extension_running")
        or state.get("pending")
        or state.get("waitingForConsular")
        or state.get("completed")
    ):
        log.info(
            f"[SCOUT] ⏭️ Assigned check skipped for "
            f"{customer}: account busy/unavailable."
        )
        return last_window_id

    my_config = _load_account_config(username)

    if not my_config:
        return last_window_id

    log.info(
        f"[SCOUT] 🔎 {customer} polling official OFC API "
        f"(position {account_position + 1}/{account_count}, "
        f"window {window_id}, IST)."
    )

    try:
        scout_fetch_task = asyncio.create_task(
            fetch_dates_via_browser(
                page,
                my_config,
                city_gap_ms=500,
                scout_slots=True,
            )
        )

        live_state_file = (
            Path(__file__).parent
            / f"state_{safe_id(username)}.json"
        )

        while not scout_fetch_task.done():
            live_state = _read_state(live_state_file)

            # Booking triggers always take priority over scouting.
            if live_state.get("pending"):
                scout_fetch_task.cancel()

                try:
                    await scout_fetch_task
                except asyncio.CancelledError:
                    pass

                log.info(
                    f"[SCOUT] ⚡ Booking trigger arrived while "
                    f"{customer} was scouting. "
                    "Scout pre-empted so booking can run immediately."
                )

                return last_window_id

            await asyncio.sleep(POLL_INTERVAL)

        res = await scout_fetch_task

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log.warning(
            f"[SCOUT] Official OFC check failed for "
            f"{customer}: {exc}"
        )
        return last_window_id

    # CVS may have arrived while this account was performing
    # the API fetch. In that case CVS owns the release.
    if is_window_stopped(
        window_id,
        window_start_epoch,
    ):
        log.info(
            f"[SCOUT] 🛑 Window stopped while {customer} "
            "was polling; ignoring scout result."
        )
        return last_window_id

    if _looks_like_expired_session(res):
        log.warning(
            f"[SCOUT] {customer} session was not usable; "
            "skipping this scout result."
        )
        return last_window_id

    results = (res or {}).get("results") or {}

    # Diagnostic only: log raw OFC dates before account filtering.
    raw_dates_found = []

    for city, dates in results.items():
        if not isinstance(dates, list) or not dates:
            continue

        raw_dates = []

        for item in dates:
            if isinstance(item, dict):
                date_value = item.get("Date", "")
            else:
                date_value = item

            date_value = str(date_value or "")[:10]

            if date_value:
                raw_dates.append(date_value)

        if raw_dates:
            raw_dates_found.append(
                f"{city}: {raw_dates}"
            )

    if raw_dates_found:
        log.info(
            f"[SCOUT] 📅 RAW OFC dates seen by {customer}: "
            + " | ".join(raw_dates_found)
        )

    # Scout-Slots mode is authoritative:
    #
    # earlyMatch exists ONLY when:
    #   Dates matched
    #   -> Slots was fetched immediately
    #   -> at least one viable slot exists
    #
    # Raw Dates in `results` must NOT create a Scout HIT.
    early_match = (
        (res or {}).get(
            "earlyMatch",
        )
        or {}
    )

    early_city = str(
        early_match.get(
            "city",
            "",
        )
        or ""
    ).strip()

    early_date = str(
        early_match.get(
            "date",
            "",
        )
        or ""
    )[:10]

    early_slots = (
        early_match.get(
            "slots",
            [],
        )
        or []
    )

    early_slots_token = str(
        early_match.get(
            "slotsToken",
            "",
        )
        or ""
    ).strip()

    if (
        not early_city
        or not early_date
        or not isinstance(
            early_slots,
            list,
        )
        or not early_slots
        or not early_slots_token
    ):
        log.info(
            f"[SCOUT] No qualifying OFC slot found by "
            f"{customer}."
        )

        return last_window_id

    # Defensive re-validation against the account's existing
    # OFC city/date criteria.
    candidate_results = {
        early_city: [
            {
                "Date": early_date,
            }
        ]
    }

    matched, matched_city, matched_date = (
        _match_polled_ofc_dates(
            candidate_results,
            my_config,
        )
    )

    if not matched:
        log.warning(
            f"[SCOUT] OFC Scout returned a slot for "
            f"{early_city} {early_date}, but it no longer "
            "matches this account's configured criteria. "
            "Ignoring result."
        )

        return last_window_id

    claimed, reason = claim_scout_hit(
        window_id,
        window_start_epoch,
        matched_city,
        matched_date,
        customer,
    )

    if not claimed:
        log.info(
            f"[SCOUT] Hit ignored because window is "
            f"already owned by {reason}."
        )
        return last_window_id

    top_slot = early_slots[0]

    log.warning(
        f"[SCOUT] 🎯 PRE-CVS SLOT HIT: "
        f"{customer} found "
        f"{matched_city} {matched_date} | "
        f"{len(early_slots)} viable slot(s). "
        f"Top: Time={top_slot.get('Time')}, "
        f"Available={top_slot.get('EntriesAvailable')}, "
        f"Num={top_slot.get('Num')}. "
        "Prioritizing detector booking first."
    )

    # ---------------------------------------------------------
    # DETECTOR-FIRST BOOKING
    #
    # Preserve BOTH response stages:
    #
    #   Dates token
    #       -> compatibility fallback
    #
    #   ordered Slots + Slots-response token
    #       -> primary direct-to-Book path
    # ---------------------------------------------------------

    scout_fastpath = None

    if (
        str(
            early_match.get(
                "city",
                "",
            )
        ).upper()
        == str(matched_city).upper()
        and str(
            early_match.get(
                "date",
                "",
            )
        )[:10]
        == str(matched_date)[:10]
        and early_match.get("token")
        and early_match.get("slotsToken")
        and isinstance(
            early_match.get("slots"),
            list,
        )
        and early_match.get("slots")
    ):
        scout_fastpath = {
            "token": (
                early_match.get(
                    "token"
                )
            ),

            "appd": (
                early_match.get(
                    "appd",
                    "",
                )
            ),

            "isReschedule": (
                early_match.get(
                    "isReschedule",
                    False,
                )
            ),

            "capturedAt": (
                early_match.get(
                    "capturedAt",
                    0,
                )
            ),

            "slots": (
                early_match.get(
                    "slots",
                    [],
                )
                or []
            ),

            "slotsToken": (
                early_match.get(
                    "slotsToken",
                    "",
                )
            ),

            "slotsCapturedAt": (
                early_match.get(
                    "slotsCapturedAt",
                    0,
                )
            ),
        }

        log.info(
            f"[SCOUT-SLOT-FAST] Preserved "
            f"{len(scout_fastpath['slots'])} OFC slot(s) "
            f"+ Slots token for {customer}: "
            f"{matched_city} {matched_date}."
        )
    detector_queued_count = await _broadcast_scout_hit(
        matched_city,
        matched_date,
        customer,
        window_id,
        only_username=username,
        scout_fastpath=scout_fastpath,
    )

    if detector_queued_count > 0:
        # Tell the normal pending-trigger path that once this
        # detector has entered booking, it should release the
        # same scout hit to all OTHER qualifying accounts.
        _update_state(
            live_state_file,
            {
                "scout_detector_broadcast_pending": True,
                "scout_detector_city": matched_city,
                "scout_detector_date": matched_date,
                "scout_detector_window_id": window_id,
                "scout_detector_customer": customer,
            },
        )

        log.warning(
            f"[SCOUT] 🚀 DETECTOR FIRST: {customer} queued for "
            f"{matched_city} {matched_date}. "
            "Fleet broadcast will start after detector enters booking."
        )

    else:
        # Detector could not book itself, for example because it
        # entered rest/running/pending state. Do NOT lose the
        # opportunity — broadcast immediately to everybody else.
        log.warning(
            f"[SCOUT] ⚠️ Detector {customer} could not be queued. "
            "Broadcasting immediately to other qualifying accounts."
        )

        await _broadcast_scout_hit(
            matched_city,
            matched_date,
            customer,
            window_id,
            exclude_username=username,
        )

    return last_window_id
async def recover_session(page, customer: str, username: str):
    log.info(f"🔄 Attempting in-place session recovery for '{customer}' ({username})...")
    
    # 1. Get credentials
    if not ACCOUNTS_FILE.exists():
        log.error("accounts.json not found for recovery.")
        return False
        
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        creds = next((c for c in raw if c.get("username") == username), None)
        if not creds:
            log.error(f"Credentials not found for username '{username}'.")
            return False
        password = creds.get("password", "")
    except Exception as e:
        log.error(f"Error reading accounts.json: {e}")
        return False
        
    fastcaptcha = os.getenv("FASTCAPTCHA_API_KEY", "")
    if not fastcaptcha:
        log.warning("FASTCAPTCHA_API_KEY missing for recovery. Captchas will fail.")
        
    # 2. Trigger redirect by navigating back to the pristine home page
    try:
        log.info("Navigating to home page to trigger session validation...")
        await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error(f"Failed during page reload: {e}")
    
    # 3. Handle waiting room
    try:
        await wait_for_waiting_room(page, log, timeout_minutes=120)
    except Exception as e:
        log.error(f"Error waiting for waiting room during recovery: {e}")
        return False
        
    # 4. Wait for automatic redirect (SPA)
    log.info("Waiting up to 10s for SPA to redirect to login if session is expired...")
    for _ in range(5):
        cur_url = page.url.lower()
        if any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
            break
        await asyncio.sleep(2)

    cur_url = page.url.lower()
    if not any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
        if "usvisascheduling.com" in cur_url and any(k in cur_url for k in ["/schedule", "/ofc-schedule", "/en-us"]):
            log.info("Already on home or schedule/reschedule page? Recovery maybe not needed.")
            return True
        log.error("Did not reach login page or home page during recovery.")
        return False
        
    # 5. Perform Login
    success = False
    for attempt in range(1, 4):
        log.info(f"Recovery login attempt {attempt}/3")
        success = await login(page, username, password, fastcaptcha, log)
        if success:
            break
        await page.reload()
        await asyncio.sleep(3)
        
    if not success:
        log.error("Recovery login failed.")
        return False
        
    # 6. Security Questions
    try:
        if not await handle_security_question(page, username, log):
            log.error("Security question failed during recovery.")
            return False
    except Exception as e:
        log.error(f"Error during security questions: {e}")
        return False
        
    log.info("✅ Security questions passed. Waiting for portal redirect...")
    try:
        await page.wait_for_url("**/*usvisascheduling.com/en-US*", timeout=30_000, wait_until="commit")
        log.info("✅ In-place session recovery successful!")
    except Exception as e:
        log.warning(f"Timeout waiting for portal redirect after login: {e}")
        
    # Wait for waiting room one more time in case it pops up after redirect
    try:
        await wait_for_waiting_room(page, log, timeout_minutes=120)
    except Exception as e:
        log.error(f"Error checking waiting room after security questions: {e}")
        
    # Clear the extension flag
    try:
        await page.evaluate("window._extensionSessionExpired = false")
    except Exception:
        pass
        
    return True


async def _broadcast_results(results: dict, customer: str):
    """Trigger every idle RESERVED_BOOKING account matching the polled dates."""
    if not ACCOUNTS_FILE.exists():
        return

    try:
        all_accounts = json.loads(
            ACCOUNTS_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(all_accounts, list):
            log.error("accounts.json must contain a JSON list.")
            return

        triggered_count = 0
        remote_mode = bool(
            os.getenv("REMOTE_TRIGGER_URL", "").strip()
        )
        remote_trigger_cooldown = int(
            os.getenv("REMOTE_TRIGGER_COOLDOWN_SECONDS", "300")
        )
        max_cross_triggers = int(
            os.getenv("MAX_CROSS_TRIGGERS", "0")
        )
        stagger_min = float(
            os.getenv("CROSS_TRIGGER_STAGGER_MIN_SECONDS", "0.2")
        )
        stagger_max = float(
            os.getenv("CROSS_TRIGGER_STAGGER_MAX_SECONDS", "0.5")
        )
        if stagger_max < stagger_min:
            stagger_max = stagger_min

        for acct_config in all_accounts:
            acct_customer = str(
                acct_config.get("customer_name", "")
            ).strip()
            acct_username = str(
                acct_config.get("username", "")
            ).strip()
            role = str(
                acct_config.get("role", "")
            ).strip().upper()

            if not acct_customer or not acct_username:
                continue

            # Only reserved booking accounts may receive cross-account triggers.
            if role != "RESERVED_BOOKING":
                continue

            matched, matched_city, earliest_date = (
                _match_polled_ofc_dates(results, acct_config)
            )

            if not matched:
                continue

            if (
                max_cross_triggers > 0
                and triggered_count >= max_cross_triggers
            ):
                log.info(
                    f"⏭️ Skipping {acct_customer}: maximum cross-account "
                    f"triggers ({max_cross_triggers}) reached."
                )
                continue

            acct_uid = safe_id(acct_username)
            acct_state_file = (
                Path(__file__).parent / f"state_{acct_uid}.json"
            )
            acct_state = _read_state(acct_state_file)

            if not remote_mode and acct_state.get("extension_running"):
                log.info(
                    f"⏭️ Skipping {acct_customer}: booking is already running."
                )
                continue

            if not remote_mode and acct_state.get("pending"):
                log.info(
                    f"⏭️ Skipping {acct_customer}: trigger already pending."
                )
                continue
            action_mode = str(
                acct_config.get("action_mode", "SNIPER")
            ).strip().upper()
            action_type = (
                "RESCHEDULE_FULL"
                if action_mode == "RESCHEDULE_FULL"
                else "SNIPER"
            )

            trigger_key = (
                f"background|{acct_uid}|{matched_city.upper()}|"
                f"{earliest_date}|{action_type}"
            )

            # In remote mode, suppress only repeated sends for the same slot
            # and account. A different slot may still trigger immediately.
            if remote_mode:
                last_trigger_key = str(
                    acct_state.get("trigger_key", "")
                )
                last_remote_trigger = float(
                    acct_state.get("remote_trigger_sent_at", 0)
                    or acct_state.get("trigger_timestamp", 0)
                    or 0
                )

                if (
                    last_trigger_key == trigger_key
                    and last_remote_trigger
                    and time.time() - last_remote_trigger
                    < remote_trigger_cooldown
                ):
                    remaining = int(
                        remote_trigger_cooldown
                        - (time.time() - last_remote_trigger)
                    )
                    log.info(
                        f"⏭️ Skipping {acct_customer}: same remote trigger "
                        f"was sent recently ({max(remaining, 0)}s remaining)."
                    )
                    continue

            log.info(
                f"🎯 POLLING AUTO-TRIGGER: {acct_customer} matched "
                f"{matched_city} (earliest: {earliest_date})"
            )

            trigger_updates = {
                "extension_running": False,
                "pending": True,
                "trigger_timestamp": time.time(),
                "trigger_key": trigger_key,
                "action_type": action_type,
                "ofcCities": acct_config.get("ofcCities", []),
                "ofcPriorityCity": matched_city,
                "ofcPriorityDate": "",
                "ofcStartDate": acct_config.get(
                    "ofcStartDate", ""
                ),
                "ofcEndDate": acct_config.get(
                    "ofcEndDate", ""
                ),
                "consularCities": acct_config.get(
                    "consularCities", []
                ),
                "consularPriorityCity": acct_config.get(
                    "consularPriorityCity", ""
                ),
                "consularStartDate": acct_config.get(
                    "consularStartDate", ""
                ),
                "consularEndDate": acct_config.get(
                    "consularEndDate", ""
                ),
                "customer_name": acct_customer,
                "prevent_immediate": acct_config.get(
                    "prevent_immediate", False
                ),
                "multiPerson": acct_config.get(
                    "multiPerson", False
                ),
            }

            # Queue the booking first. Slack must never delay or block it.
            _update_state(acct_state_file, trigger_updates)
            triggered_count += 1

            try:
                sent = send_slack(
                    f"🎯 *Cross-Account Auto-Trigger*\n"
                    f"*Booking ID:* `{acct_customer}`\n"
                    f"*Detected by:* `{customer}`\n"
                    f"*OFC:* {matched_city} — {earliest_date}\n"
                    f"*Action:* {action_type}"
                )
                if not sent:
                    log.warning(
                        f"⚠️ Slack alert was not sent for {acct_customer}, "
                        "but the booking trigger was queued."
                    )
            except Exception as slack_error:
                log.warning(
                    f"⚠️ Slack alert failed for {acct_customer}, "
                    f"but booking will continue: {slack_error}"
                )

            await asyncio.sleep(
                random.uniform(stagger_min, stagger_max)
            )

        if triggered_count:
            log.info(
                f"✅ Triggered {triggered_count} eligible booking account(s)."
            )

    except Exception as e:
        log.error(
            f"Error cross-triggering accounts: {e}",
            exc_info=True,
        )

def _looks_like_expired_session(result: dict) -> bool:
    """Detect when fetch_dates_via_browser returned HTML login pages instead of JSON.

    Each city entry becomes {'error': 'Not JSON. HTML Snippet: ...'} when the
    browser session has silently expired and the OFC API redirected to a login
    HTML page. This returns True if every city looks like that so the caller
    can trigger session recovery instead of logging meaningless garbage.
    """
    results = (result or {}).get("results") or {}
    if not results:
        # An error like 'Could not find primaryId or appd' is also session-related
        if (result or {}).get("error"):
            return True
        return False
    expired = 0
    for value in results.values():
        if isinstance(value, dict) and ("Not JSON" in str(value.get("error", "")) or "HTML" in str(value.get("error", ""))):
            expired += 1
    # All cities returned HTML → session is dead
    return expired > 0 and expired == len(results)


async def _try_background_poll(page, customer: str, username: str, last_background_poll: float, last_poll_debug: float) -> tuple[float, float, bool]:
    """Execute background API polling if permitted by global limits and personal cooldowns.

    Returns (last_background_poll, last_poll_debug). Raises SessionExpiredError
    when the OFC date fetch returns login HTML on every city, so the main loop
    can run session recovery immediately instead of silently polling a dead
    session.
    """
    polling_state_file = Path(__file__).parent / "polling_state.json"
    polling_active = False
    cooldown_seconds = 3600
    gap_seconds = 900
    global_last_poll = 0
    
    if polling_state_file.exists():
        try:
            with open(polling_state_file, "r") as f:
                pstate = json.load(f)
                polling_active = pstate.get("is_active", False)
                cooldown_seconds = int(pstate.get("cooldown", 600))
                gap_seconds = int(pstate.get("gap", 60))
                global_last_poll = float(pstate.get("global_last_poll", 0))
        except Exception:
            pass
            
    # To poll, we must pass our personal cooldown AND the global gap must have elapsed
    my_cooldown_passed = (time.time() - last_background_poll) > cooldown_seconds
    global_gap_passed = (time.time() - global_last_poll) > gap_seconds
    
    if polling_active and (time.time() - last_poll_debug) > 30:
        last_poll_debug = time.time()
        print(f"[POLLING-RESULT] 🔍 DEBUG: active={polling_active}, my_cd_passed={my_cooldown_passed}(last={last_background_poll:.0f}, cd={cooldown_seconds}s), gap_passed={global_gap_passed}(last_global={global_last_poll:.0f}, gap={gap_seconds}s)", flush=True)
    
    if polling_active and my_cooldown_passed and global_gap_passed:
        # Try to acquire the slot atomically
        got_slot = False
        polling_lock_file = Path(__file__).parent / "polling_state.lock"
        try:
            lock_fd = os.open(polling_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(lock_fd)
            try:
                # Double check inside lock
                with open(polling_state_file, "r") as f:
                    pstate = json.load(f)
                global_last_poll = float(pstate.get("global_last_poll", 0))
                gap_seconds = int(pstate.get("gap", 60))
                
                if (time.time() - global_last_poll) > gap_seconds:
                    pstate["global_last_poll"] = time.time()
                    with open(polling_state_file, "w") as f:
                        json.dump(pstate, f)
                    got_slot = True
            finally:
                try:
                    os.remove(polling_lock_file)
                except OSError:
                    pass
        except FileExistsError:
            # Cleanup stale lock
            try:
                if time.time() - os.path.getmtime(polling_lock_file) > 10:
                    os.remove(polling_lock_file)
            except OSError:
                pass
        except Exception:
            pass
            
        if got_slot:
            last_background_poll = time.time()
            try:
                my_config = _load_account_config(username)
                res = await fetch_dates_via_browser(page, my_config)
                if res and res.get("success"):
                    results = res["results"]
                    dates_found = False

                    # Detect a silently-expired session: every city returned a
                    # login HTML page instead of JSON. Don't log the garbage or
                    # keep polling on a dead session — raise so the main loop
                    # recovers the session now.
                    if _looks_like_expired_session(res):
                        print(f"[POLLING-RESULT] 🚨 Session expired during background poll for '{customer}' (OFC API returned login HTML). Raising for recovery.", flush=True)
                        raise SessionExpiredError("OFC date fetch returned login HTML for all cities")

                    print(f"[POLLING-RESULT] 🤖 Account '{customer}' just contributed polling data.", flush=True)

                    log_lines = [f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Polling by '{customer}':"]

                    for city, dates in results.items():
                        if isinstance(dates, list) and len(dates) > 0:
                            dates_found = True
                        print(f"[POLLING-RESULT] {city}: {dates}", flush=True)
                        log_lines.append(f"  {city}: {dates}")

                    # Write to file
                    try:
                        Path("logs").mkdir(exist_ok=True)
                        with open("logs/polling.log", "a", encoding="utf-8") as f:
                            f.write("\n".join(log_lines) + "\n")
                    except Exception as e:
                        print(f"[POLLING-RESULT] Error saving log: {e}")

                    if dates_found:
                        await _broadcast_results(results, customer)
                        
                        # --- SELF-BOOKING LOGIC ---
                        uid = safe_id(username)
                        state_file = (
                            Path(__file__).parent / f"state_{uid}.json"
                        )
                        instant_booking = True

                        polling_state_file = (
                            Path(__file__).parent / "polling_state.json"
                        )
                        if polling_state_file.exists():
                            try:
                                with open(polling_state_file, "r") as f:
                                    instant_booking = json.load(f).get(
                                        "instant_booking",
                                        True,
                                    )
                            except Exception:
                                pass

                        # The atomic queue helper checks rest/busy/pending state.
                        if instant_booking and ACCOUNTS_FILE.exists():
                            all_accounts = json.loads(
                                ACCOUNTS_FILE.read_text(encoding="utf-8")
                            )
                            my_config = next(
                                (
                                    acc
                                    for acc in all_accounts
                                    if acc.get("customer_name") == customer
                                    or acc.get("username") == username
                                ),
                                None,
                            )

                            if my_config:
                                matched, matched_city, earliest_date = (
                                    _match_polled_ofc_dates(
                                        results,
                                        my_config,
                                    )
                                )

                                if matched:
                                    action_mode = str(
                                        my_config.get(
                                            "action_mode",
                                            "SNIPER",
                                        )
                                    ).strip().upper()

                                    action_type = (
                                        "RESCHEDULE_FULL"
                                        if action_mode == "RESCHEDULE_FULL"
                                        else "SNIPER"
                                    )

                                    trigger_key = (
                                        f"background|{safe_id(username)}|"
                                        f"{matched_city.upper()}|"
                                        f"{earliest_date}|{action_type}"
                                    )

                                    trigger_updates = {
                                        "pending": True,
                                        "trigger_timestamp": time.time(),
                                        "trigger_key": trigger_key,
                                        "action_type": action_type,
                                        "ofcCities": my_config.get(
                                            "ofcCities", []
                                        ),
                                        "ofcPriorityCity": matched_city,
                                        "ofcPriorityDate": "",
                                        "ofcStartDate": my_config.get(
                                            "ofcStartDate", ""
                                        ),
                                        "ofcEndDate": my_config.get(
                                            "ofcEndDate", ""
                                        ),
                                        "consularCities": my_config.get(
                                            "consularCities", []
                                        ),
                                        "consularPriorityCity": my_config.get(
                                            "consularPriorityCity", ""
                                        ),
                                        "consularStartDate": my_config.get(
                                            "consularStartDate", ""
                                        ),
                                        "consularEndDate": my_config.get(
                                            "consularEndDate", ""
                                        ),
                                        "customer_name": customer,
                                        "prevent_immediate": my_config.get(
                                            "prevent_immediate", False
                                        ),
                                        "multiPerson": my_config.get(
                                            "multiPerson", False
                                        ),
                                    }

                                    queued, queue_reason = (
                                        try_queue_local_trigger(
                                            state_file,
                                            trigger_updates,
                                        )
                                    )

                                    if queued:
                                        print(
                                            f"[POLLING-RESULT] ⚡ "
                                            f"Self-booking triggered for "
                                            f"'{customer}'!",
                                            flush=True,
                                        )
                                        return (
                                            last_background_poll,
                                            last_poll_debug,
                                            True,
                                        )

                                    print(
                                        f"[POLLING-RESULT] ⏭️ "
                                        f"Self-booking not queued for "
                                        f"'{customer}': {queue_reason}",
                                        flush=True,
                                    )
                                    
                else:
                    print(f"[POLLING-RESULT] {res}", flush=True)
            except SessionExpiredError:
                # Propagate so the main watch loop runs recover_session().
                # Reset last_background_poll so we retry polling promptly after
                # the session is restored rather than waiting a full cooldown.
                last_background_poll = 0
                raise
            except Exception as e:
                print(f"[POLLING-RESULT] Error: {e}", flush=True)

    return last_background_poll, last_poll_debug, False

# ─── Main runner loop ─────────────────────────────────────────────────────────

async def run(cdp_port: int, customer: str, username: str):
    uid = safe_id(username)
    state_file = Path(__file__).parent / f"state_{uid}.json"
    
    # Custom logger formatting to show customer prefix
    log.name = f"booking:{customer}"
    
 
    # Initialise runner-owned fields only.
# IMPORTANT: Never erase an existing temporary OFC hold on runner restart.
    _update_state(
        state_file,
        {
            "extension_running": False,
            "customer_name": customer,
        },
    )

    
    last_background_poll = 0
    was_polling_active = False
    last_poll_debug = 0

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        def handle_console(msg):
            text = msg.text
            is_match = "Sniper" in text or "Consular" in text or "OFC" in text or "Booking" in text
            is_err = msg.type == "error" and "usvisascheduling.com" in text
            
            if is_match or is_err:
                log_file = Path("logs/extension.log")
                log_file.parent.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_file, "a", encoding="utf-8") as f:
                    if msg.type == "error":
                        prefix = "[ERROR]"
                    elif msg.type == "warning":
                        prefix = "[WARN]"
                    else:
                        prefix = "[INFO]"
                    f.write(f"[{timestamp}] {prefix} [{customer}] {text}\n")
                # Also write to MongoDB
                db_logger.log_extension_console(timestamp, prefix, customer, text)

        page.on("console", handle_console)

        # Inject listener for extension's session expiry broadcast
        listener_script = """
            // Always reset the flag on every navigation to prevent stale triggers
            window._extensionSessionExpired = false;
            if (!window.__sniperExpiryListenerAdded) {
                window.__sniperExpiryListenerAdded = true;
                window.addEventListener("message", (event) => {
                    if (event.source !== window || !event.data || !event.data.action) return;
                    if (event.data.action === "SESSION_EXPIRED") {
                        window._extensionSessionExpired = true; 
                    }
                });
            }
        """
        await page.add_init_script(listener_script)
        try:
            await page.evaluate(listener_script)
        except Exception as e:
            log.warning(f"Could not instantly bind expiry listener: {e}")

        log.info("Waiting for portal …")
        if not await ensure_on_portal(page, log):
            log.error("Could not reach portal. Exiting.")
            sys.exit(1)

        log.info("=" * 60)
        log.info(f"✅ Booking runner ready — watching {state_file.name}")
        log.info("=" * 60)

        runner_start_time = time.time()
        last_keep_alive = time.time()

        scout_position = -1
        scout_count = 0

        consular_scout_position = -1
        consular_scout_count = 0

        last_scout_roster_refresh = 0.0
        last_scout_window_id = ""
        last_consular_scout_window_id = ""

        while True:
            try:
                # ── Check Rest Period FIRST ───────────────────────────────
                state = _read_state(state_file)
                rest_until = state.get("rest_until", 0)
                is_resting = bool(rest_until and time.time() < rest_until)
                
                # If resting and pending, clear the pending state so we don't block polling
                if is_resting and state.get("pending"):
                    log.info("💤 Account is in a rest period. Ignoring and clearing pending booking trigger.")
                    _update_state(state_file, {"pending": False})
                    state["pending"] = False

                # ── Pre-CVS OFC scout layer ────────────────────────────────
                # Refresh local account order periodically so GUI/account
                # changes are picked up without restarting the runner.
                if (
                    time.time() - last_scout_roster_refresh
                    > 30
                ):
                    scout_position, scout_count = (
                        _get_scout_position(username)
                    )

                    (
                        consular_scout_position,
                        consular_scout_count,
                    ) = _get_consular_scout_position(
                        username
                    )

                    last_scout_roster_refresh = (
                        time.time()
                    )

                last_scout_window_id = (
                    await _try_pre_cvs_scout(
                        page,
                        customer,
                        username,
                        state,
                        scout_position,
                        scout_count,
                        last_scout_window_id,
                    )
                )

                # Re-read because OFC Scout / CVS may have altered
                # account state while the previous check was running.
                state = _read_state(
                    state_file
                )

                # ── PRE-CVS CONSULAR SCOUT ───────────────────────
                #
                # This does nothing unless:
                #
                # waitingForConsular=True
                # bookedOfcDate exists
                # waitStartTime is within 50-minute hold
                # account is idle
                #
                # It is therefore mutually exclusive with the
                # existing normal OFC Scout for this account.
                last_consular_scout_window_id = (
                    await _try_pre_consular_scout(
                        page,
                        customer,
                        username,
                        state,
                        consular_scout_position,
                        consular_scout_count,
                        last_consular_scout_window_id,
                    )
                )

                # A Scout hit may have just queued this same account.
                # Re-read state before entering the EXISTING trigger flow.
                state = _read_state(state_file)
                rest_until = state.get("rest_until", 0)
                is_resting = bool(
                    rest_until
                    and time.time() < rest_until
                )

                # ── Check for pending trigger ───────────────────────────────
                if state.get("pending") and not is_resting:
                    print("\n" + "=" * 60)

                    # Check the original trigger timestamp BEFORE modifying the state file.
                    trigger_ts = state.get("trigger_timestamp")
                    trigger_delay = None

                    if trigger_ts:
                        try:
                            trigger_delay = time.time() - float(trigger_ts)
                        except (TypeError, ValueError):
                            log.warning(
                                f"Could not read trigger timestamp: {trigger_ts!r}"
                            )

                    # Never execute a trigger that is more than 30 seconds old.
                    if trigger_delay is not None and trigger_delay > 30.0:
                        log.warning(
                            f"⏭️ Skipping stale trigger for '{customer}': "
                            f"it is {trigger_delay:.1f} seconds old."
                        )

                    
                        _update_state(
                            state_file,
                            {
                                "pending": False,
                                "extension_running": False,
                                # Never retain stale Scout fast-path data.
                                "scoutOfcToken": "",
                                "scoutOfcAppd": "",
                                "scoutOfcTokenCity": "",
                                "scoutOfcTokenDate": "",
                                "scoutOfcTokenIsReschedule": False,
                                "scoutOfcTokenCapturedAt": 0,
                                "scoutOfcSlots": [],
                                "scoutOfcSlotsToken": "",
                                "scoutOfcSlotsCapturedAt": 0,

                                "scoutConsularToken": "",
                                "scoutConsularPrimaryId": "",
                                "scoutConsularAppd": "",
                                "scoutConsularApplications": [],
                                "scoutConsularTokenCity": "",
                                "scoutConsularTokenDate": "",
                                "scoutConsularTokenIsReschedule": False,
                                "scoutConsularTokenCapturedAt": 0,
                                "scoutConsularSlots": [],
                                "scoutConsularSlotsToken": "",
                                "scoutConsularSlotsCapturedAt": 0,
                                # Existing OFC Scout detector cleanup.
                                "scout_detector_broadcast_pending": False,
                                "scout_detector_city": "",
                                "scout_detector_date": "",
                                "scout_detector_window_id": "",
                                "scout_detector_customer": "",

                                # Consular Scout detector cleanup.
                                # A stale detector must never release an old
                                # Consular slot on some later unrelated trigger.
                                "consular_scout_detector_broadcast_pending": False,
                                "consular_scout_detector_city": "",
                                "consular_scout_detector_date": "",
                                "consular_scout_detector_window_id": "",
                                "consular_scout_detector_customer": "",
                            },
                        )
                        last_keep_alive = time.time()
                        await asyncio.sleep(0.1)
                        continue

                    # The trigger is fresh. Mark the extension as running.
                    _update_state(
                        state_file,
                        {
                            "extension_running": True,
                            "pending": False,
                        },
                    )

                    log.info(
                        f"📥 Pending trigger detected for '{customer}'."
                    )

                    # -------------------------------------------------
                    # SCOUT DETECTOR-FIRST RELEASE
                    #
                    # Do NOT release the fleet merely because the
                    # detector has entered extension_running state.
                    #
                    # The detector must first send EXECUTE_SNIPER to
                    # its extension. executor.py will then invoke the
                    # callback below and release the remaining accounts.
                    #
                    # A 1-second fallback prevents a stuck detector
                    # from holding the whole fleet.
                    # -------------------------------------------------
                    scout_detector_release_callback = None
                    consular_scout_detector_release_callback = None
                    if state.get("scout_detector_broadcast_pending"):
                        scout_city = str(
                            state.get(
                                "scout_detector_city",
                                "",
                            )
                        ).strip()

                        scout_date = str(
                            state.get(
                                "scout_detector_date",
                                "",
                            )
                        ).strip()

                        scout_window_id = str(
                            state.get(
                                "scout_detector_window_id",
                                "",
                            )
                        ).strip()

                        scout_detected_by = str(
                            state.get(
                                "scout_detector_customer",
                                customer,
                            )
                        ).strip()

                        # Clear persistent state immediately.
                        # The local guarded callback now owns release.
                        _update_state(
                            state_file,
                            {
                                "scout_detector_broadcast_pending": False,
                                "scout_detector_city": "",
                                "scout_detector_date": "",
                                "scout_detector_window_id": "",
                                "scout_detector_customer": "",
                            },
                        )

                        if (
                            scout_city
                            and scout_date
                            and scout_window_id
                        ):
                            release_guard = {
                                "released": False,
                                "lock": asyncio.Lock(),
                            }

                            async def _release_scout_fleet(
                                reason: str,
                                guard=release_guard,
                                city=scout_city,
                                date=scout_date,
                                window_id=scout_window_id,
                                detected_by=scout_detected_by,
                                detector_username=username,
                                detector_customer=customer,
                            ):
                                async with guard["lock"]:
                                    if guard["released"]:
                                        return

                                    guard["released"] = True

                                _queue_background_task(
                                    _broadcast_scout_hit(
                                        city,
                                        date,
                                        detected_by,
                                        window_id,
                                        exclude_username=detector_username,
                                    )
                                )

                                log.info(
                                    f"[SCOUT] 📣 Fleet released after "
                                    f"{reason}: detector "
                                    f"{detector_customer}; "
                                    f"{city} {date}."
                                )

                            async def _release_after_detector_message(
                                release_func=_release_scout_fleet,
                            ):
                                await release_func(
                                    "detector extension message sent"
                                )

                            scout_detector_release_callback = (
                                _release_after_detector_message
                            )

                            async def _detector_release_fallback(
                                release_func=_release_scout_fleet,
                            ):
                                await asyncio.sleep(1.0)

                                await release_func(
                                    "1-second detector safety fallback"
                                )

                            _queue_background_task(
                                _detector_release_fallback()
                            )
                    # -------------------------------------------------
                    # CONSULAR SCOUT DETECTOR-FIRST RELEASE
                    # -------------------------------------------------
                    if state.get(
                        "consular_scout_detector_broadcast_pending"
                    ):
                        consular_scout_city = str(
                            state.get(
                                "consular_scout_detector_city",
                                "",
                            )
                        ).strip()

                        consular_scout_date = str(
                            state.get(
                                "consular_scout_detector_date",
                                "",
                            )
                        ).strip()

                        consular_scout_window_id = str(
                            state.get(
                                "consular_scout_detector_window_id",
                                "",
                            )
                        ).strip()

                        consular_scout_detected_by = str(
                            state.get(
                                "consular_scout_detector_customer",
                                customer,
                            )
                        ).strip()

                        # Persistent state can now be cleared.
                        # The guarded local callback owns release.
                        _update_state(
                            state_file,
                            {
                                "consular_scout_detector_broadcast_pending": False,
                                "consular_scout_detector_city": "",
                                "consular_scout_detector_date": "",
                                "consular_scout_detector_window_id": "",
                                "consular_scout_detector_customer": "",
                            },
                        )

                        if (
                            consular_scout_city
                            and consular_scout_date
                            and consular_scout_window_id
                        ):
                            consular_release_guard = {
                                "released": False,
                                "lock": asyncio.Lock(),
                            }

                            async def _release_consular_scout_fleet(
                                reason: str,
                                guard=consular_release_guard,
                                city=consular_scout_city,
                                date=consular_scout_date,
                                window_id=consular_scout_window_id,
                                detected_by=consular_scout_detected_by,
                                detector_username=username,
                                detector_customer=customer,
                            ):
                                async with guard["lock"]:
                                    if guard["released"]:
                                        return

                                    guard["released"] = True

                                _queue_background_task(
                                    _broadcast_consular_scout_hit(
                                        city,
                                        date,
                                        detected_by,
                                        window_id,
                                        exclude_username=detector_username,
                                    )
                                )

                                log.info(
                                    f"[CONSULAR-SCOUT] 📣 Fleet released "
                                    f"after {reason}: detector "
                                    f"{detector_customer}; "
                                    f"{city} {date}."
                                )

                            async def _release_after_consular_message(
                                release_func=(
                                    _release_consular_scout_fleet
                                ),
                            ):
                                await release_func(
                                    "detector Consular extension "
                                    "message sent"
                                )

                            consular_scout_detector_release_callback = (
                                _release_after_consular_message
                            )

                            # Same protection as current OFC Scout:
                            # detector cannot accidentally hold the
                            # whole fleet forever.
                            async def _consular_detector_release_fallback(
                                release_func=(
                                    _release_consular_scout_fleet
                                ),
                            ):
                                await asyncio.sleep(
                                    1.0
                                )

                                await release_func(
                                    "1-second detector "
                                    "safety fallback"
                                )

                            _queue_background_task(
                                _consular_detector_release_fallback()
                            )
                    if trigger_delay is not None:
                        if trigger_delay > 10.0:
                            log.warning(
                                f"⚠️ Trigger execution delayed by "
                                f"{trigger_delay:.1f} seconds! "
                                f"Reason: Bot was busy or in Cloudflare queue."
                            )
                        else:
                            log.info(
                                f"⚡ Trigger picked up swiftly in "
                                f"{trigger_delay:.3f} seconds."
                            )

                    action_type = state.get("action_type")

                    trigger = {k: state[k] for k in [
                        "action_type",
                        "ofcCities", "ofcPriorityCity", "ofcPriorityDate", "ofcStartDate", "ofcEndDate",
                        "consularCities", "consularPriorityCity", "consularStartDate", "consularEndDate",
                        "customer_name", "prevent_immediate", "multiPerson"
                    ] if k in state}
                    trigger = {k: state[k] for k in [
                        "action_type",
                        "ofcCities", "ofcPriorityCity", "ofcPriorityDate", "ofcStartDate", "ofcEndDate",
                        "consularCities", "consularPriorityCity", "consularStartDate", "consularEndDate",
                        "customer_name", "prevent_immediate", "multiPerson",
                        "scoutOfcToken",
                        "scoutOfcAppd",
                        "scoutOfcTokenCity",
                        "scoutOfcTokenDate",
                        "scoutOfcTokenIsReschedule",
                        "scoutOfcTokenCapturedAt",
                        "scoutOfcSlots",
                        "scoutOfcSlotsToken",
                        "scoutOfcSlotsCapturedAt",

                        "scoutConsularToken",
                        "scoutConsularPrimaryId",
                        "scoutConsularAppd",
                        "scoutConsularApplications",
                        "scoutConsularTokenCity",
                        "scoutConsularTokenDate",
                        "scoutConsularTokenIsReschedule",
                        "scoutConsularTokenCapturedAt",
                        "scoutConsularSlots",
                        "scoutConsularSlotsToken",
                        "scoutConsularSlotsCapturedAt",
                    ] if k in state}

# One-shot token: remove it from persistent state immediately.
                    # Consular Scout token is also one-shot.
                    # Once copied into this local trigger object,
                    # remove it from persistent state so a future
                    # CVS/Scout trigger can never reuse a stale token.
                    # OFC Scout token/slot data is one-shot.
                    # Once copied locally, it must never survive
                    # into a later CVS/polling/Scout trigger.
                    # OFC Scout token/slot data is one-shot.
                    if (
                        trigger.get("scoutOfcToken")
                        or trigger.get(
                            "scoutOfcSlotsToken"
                        )
                    ):
                        _update_state(
                            state_file,
                            {
                                "scoutOfcToken": "",
                                "scoutOfcAppd": "",
                                "scoutOfcTokenCity": "",
                                "scoutOfcTokenDate": "",
                                "scoutOfcTokenIsReschedule": False,
                                "scoutOfcTokenCapturedAt": 0,
                                "scoutOfcSlots": [],
                                "scoutOfcSlotsToken": "",
                                "scoutOfcSlotsCapturedAt": 0,
                            },
                        )

                    # Consular Scout token/slot data is also one-shot.
                    if (
                        trigger.get("scoutConsularToken")
                        or trigger.get(
                            "scoutConsularSlotsToken"
                        )
                    ):
                        _update_state(
                            state_file,
                            {
                                "scoutConsularToken": "",
                                "scoutConsularPrimaryId": "",
                                "scoutConsularAppd": "",
                                "scoutConsularApplications": [],
                                "scoutConsularTokenCity": "",
                                "scoutConsularTokenDate": "",
                                "scoutConsularTokenIsReschedule": False,
                                "scoutConsularTokenCapturedAt": 0,
                                "scoutConsularSlots": [],
                                "scoutConsularSlotsToken": "",
                                "scoutConsularSlotsCapturedAt": 0,
                            },
                        )
                    # ── Re-navigate if needed ──────────────────────────────────
                    try:
                        if not page.url.startswith("https://www.usvisascheduling.com"):
                            log.warning("Page navigated away from portal. Waiting …")
                            await ensure_on_portal(page, log)
                    except Exception:
                        log.warning("Page navigating — waiting for portal …")
                        await ensure_on_portal(page, log)
                        
                    # ── Ensure we are truly on the portal, not Cloudflare ───────────────
                    try:
                        title = (await page.title()).lower()
                        if "waiting room" in title or "moment" in title or "verify you are human" in title or "attention required" in title:
                            log.warning("⚠️ Cloudflare waiting room / captcha detected before trigger! Resolving...")
                            await wait_for_waiting_room(page, log, timeout_minutes=120)
                    except Exception as e:
                        log.error(f"Error checking Cloudflare before trigger: {e}")

                    # ── Execute action ─────────────────────────────────────────
                    success = False
                    context = {}
                    try:
                        if action_type == "SNIPER":
                            log.info(f"🎯 Action type: {action_type}")
                            success, context = await trigger_extension_booking(
                                page,
                                trigger,
                                log,
                                on_message_sent=scout_detector_release_callback,
                            )

                        elif action_type == "RESCHEDULE_FULL":
                            log.info(f"🔄 Action type: {action_type}")
                            success, context = await trigger_extension_booking(
                                page,
                                trigger,
                                log,
                                on_message_sent=scout_detector_release_callback,
                            )
                        elif action_type == "SNIPER_CONSULAR_ONLY":
                            log.info(
                                "🎯 Action type: "
                                "SNIPER_CONSULAR_ONLY (Fallback)"
                            )

                            bookedOfcDate = state.get(
                                "bookedOfcDate",
                                "",
                            )

                            success, context = (
                                await trigger_extension_sniper_consular_only(
                                    page,
                                    trigger,
                                    bookedOfcDate,
                                    log,
                                    state_file=state_file,
                                    on_message_sent=(
                                        consular_scout_detector_release_callback
                                    ),
                                )
                            )

                        elif action_type == "RESCHEDULE_FULL_CONSULAR_ONLY":
                            log.info(
                                "🔄 Action type: "
                                "RESCHEDULE_FULL_CONSULAR_ONLY (Fallback)"
                            )

                            bookedOfcDate = state.get(
                                "bookedOfcDate",
                                "",
                            )

                            success, context = (
                                await trigger_extension_sniper_consular_only(
                                    page,
                                    trigger,
                                    bookedOfcDate,
                                    log,
                                    state_file=state_file,
                                    on_message_sent=(
                                        consular_scout_detector_release_callback
                                    ),
                                )
                            )
                        elif action_type == "RESCHEDULE_CONSULAR":
                            log.info("🔄 Action type: RESCHEDULE_CONSULAR")
                            success = await trigger_extension_reschedule(page, trigger, log)
                        else:
                            log.error(f"❌ Unknown or missing action_type: {action_type!r} — skipping.")
                            success = False
                    except Exception as e:
                        log.error(f"Action error: {e}", exc_info=True)
                        success = False

                        if "429" in str(e):
                            is_waiting = state.get(
                                "waitingForConsular",
                                False,
                            )

                            if is_waiting:
                                log.warning(
                                    "⚠️ 429 during Consular WAIT MODE. "
                                    "The temporary OFC hold is being preserved. "
                                    "Returning to wait mode for the next CVS Consular alert."
                                )

                                _update_state(
                                    state_file,
                                    {
                                        "pending": False,
                                        "extension_running": False,
                                    },
                                )

                                # Do NOT clear waitingForConsular/bookedOfcDate/waitStartTime.
                                # Do NOT enter booking rest during the temporary OFC hold.
                                # A very short pause only avoids immediately hammering the endpoint.
                                await asyncio.sleep(5)
                                continue

                            log.error(
                                "429 Too Many Requests detected! "
                                "Exiting bot2 with code 42 to signal a restart."
                            )

                            _enter_booking_rest(
                                state_file,
                                customer,
                                "429 Too Many Requests during booking attempt",
                            )
                            sys.exit(42)

                       
                        if "Session expired" in str(e):
                            is_waiting = state.get(
                                "waitingForConsular",
                                False,
                            )

                            if is_waiting:
                                log.warning(
                                    "⚠️ Consular request reported session expiry while "
                                    "a temporary OFC hold is active. Preserving OFC "
                                    "WAIT MODE and validating the browser session."
                                )
                            else:
                                log.error(
                                    "Session expired during booking action. "
                                    "Starting recovery."
                                )

                            recovered = await recover_session(
                                page,
                                customer,
                                username,
                            )

                            if is_waiting:
                                # Regardless of what the failed Consular request reported,
                                # the OFC hold remains authoritative until the 50-minute
                                # hold window expires.
                                _update_state(
                                    state_file,
                                    {
                                        "pending": False,
                                        "extension_running": False,
                                    },
                                )

                                if not recovered:
                                    log.error(
                                        "Session validation/recovery failed during "
                                        "Consular WAIT MODE. Preserving the OFC hold "
                                        "state and restarting the runner."
                                    )
                                    sys.exit(1)

                                log.info(
                                    "✅ Browser session is usable/recovered. "
                                    "Temporary OFC hold preserved; waiting for the "
                                    "next qualifying CVS Consular alert."
                                )
                                continue

    # Normal non-waiting booking failure keeps existing behaviour.
                            # Normal non-waiting booking failure keeps existing behaviour.
                            _enter_booking_rest(
                                state_file,
                                customer,
                                "Session expired during booking attempt",
                            )

                            if not recovered:
                                log.error(
                                    "Recovery failed after booking action. "
                                    "Exiting to trigger orchestrator restart."
                                )
                                sys.exit(1)

                            log.info(
                                "Session recovered. Background polling will "
                                "continue, but CVS and self-booking remain "
                                "blocked until booking rest expires."
                            )
                            continue
                    if success:
                        log.info("=" * 60)
                        log.info(f"✅ ACTION COMPLETED SUCCESSFULLY for '{customer}'! [{action_type}]")
                        log.info("=" * 60)
                        
                        _update_state(
                            state_file,
                            {
                                "waitingForConsular": False,
                                "bookedOfcDate": None,
                                "waitStartTime": None,
                                "completed": True,
                                "rest_until": 0,
                            },
                        )
                        full_booking_message = (
                            "🎉 *BOOKING SUCCESSFUL* 🎉\n"
                            f"*Customer / ID:* `{customer}`\n"
                            f"*Type:* `{action_type}`\n"
                            "✅ The appointment has been successfully scheduled!"
                        )

                        # Existing main Slack channel notification.
                        send_slack(full_booking_message)

                        # Send the same full-booking notification to
                        # the OFC booking alerts channel.
                        send_full_booking_to_ofc(
                            full_booking_message
                        )
                    else:
                        if context.get("waitingForConsular"):
                            booked_ofc_date = str(
                                context.get("bookedOfcDate") or ""
                            ).strip()

                            priority_city = str(
                                trigger.get("ofcPriorityCity") or ""
                            ).strip()

                            booking_event_id = (
                                state.get("trigger_key")
                                or state.get("trigger_timestamp")
                                or time.time()
                            )

                            alert_key = (
                                f"{safe_id(username)}|"
                                f"{booked_ofc_date}|"
                                f"{booking_event_id}"
                                if booked_ofc_date
                                else ""
                            )

                            already_alerted = (
                                alert_key
                                and state.get(
                                    "lastOfcBookedAlertKey"
                                ) == alert_key
                            )

                            already_queued = (
                                alert_key
                                and state.get(
                                    "ofcBookedAlertQueuedKey"
                                ) == alert_key
                            )

                            should_send_alert = bool(
                                alert_key
                                and not already_alerted
                                and not already_queued
                            )

                            wait_state_updates = {
                                "waitingForConsular": True,
                                "bookedOfcDate": booked_ofc_date,
                                "waitStartTime": (
                                    state.get("waitStartTime")
                                    or time.time()
                                ),
                                "extension_running": False,
                                "pending": False,
                            }

                            if should_send_alert:
                                wait_state_updates[
                                    "ofcBookedAlertQueuedKey"
                                ] = alert_key

                            # Save the confirmed OFC state first.
                            _update_state(
                                state_file,
                                wait_state_updates,
                            )

                            log.warning("=" * 60)
                            log.warning(
                                f"⏳ PARTIAL BOOKING / STILL WAITING "
                                f"for '{customer}'! Transitioning to "
                                f"WAIT MODE..."
                            )
                            log.warning("=" * 60)

                            # Slack runs separately and cannot delay the bot.
                            if should_send_alert:
                                _queue_background_task(
                                    _send_ofc_alert_in_background(
                                        state_file,
                                        customer,
                                        booked_ofc_date,
                                        priority_city,
                                        alert_key,
                                    )
                                )

                            last_keep_alive = time.time()
                            await asyncio.sleep(0.5)
                            continue
                        
                        else:
                            log.error(
                                f"❌ Action failed for '{customer}'. "
                                f"[{action_type}]"
                            )

                            if state.get("waitingForConsular"):
                                log.warning(
                                    "⏳ Consular-only attempt failed. "
                                    "Temporary OFC hold is still active; "
                                    "returning to WAIT MODE."
                                )

                                _update_state(
                                    state_file,
                                    {
                                        "pending": False,
                                        "extension_running": False,
                                    },
                                )

                                last_keep_alive = time.time()
                                await asyncio.sleep(0.5)
                                continue

                            _enter_booking_rest(
                                state_file,
                                customer,
                                f"{action_type or 'UNKNOWN'} booking attempt failed",
                            )

                    # Mark extension as done
                    _update_state(state_file, {"extension_running": False})
                    last_keep_alive = time.time()
                    
                    await asyncio.sleep(0.5)
                    continue

                # ── If NO trigger, do maintenance ──────────────────────────────
                await asyncio.sleep(POLL_INTERVAL)
                
                # ── Consular wait mode ────────────────────────────────────────
                # Do not automatically attempt Consular after OFC booking.
                # Keep waitingForConsular active and wait only for a CVS trigger.
                state = _read_state(state_file)

                # A temporarily-booked OFC is held for approximately 50 minutes.
                # During that entire window, preserve Consular-only wait mode.
                # Once the window expires, release the Python-side hold so the
                # account may book OFC again.
                if state.get("waitingForConsular"):
                    wait_start = float(
                        state.get("waitStartTime") or 0
                    )

                    if (
                        wait_start
                        and time.time() - wait_start >= 50 * 60
                    ):
                        log.warning(
                            f"⌛ 50-minute temporary OFC hold window expired "
                            f"for '{customer}'. Clearing WAIT MODE so OFC "
                            "can be booked again."
                        )

                        _update_state(
                            state_file,
                            {
                                "waitingForConsular": False,
                                "bookedOfcDate": None,
                                "bookedOfcCity": None,
                                "waitStartTime": None,
                                "pending": False,
                                "extension_running": False,
                                "rest_until": 0,
                            },
                        )

                        state = _read_state(state_file)

                # ── Background API Polling ────────────────────────────────────                # ── Background API Polling ────────────────────────────────────
                current_account_role = "POLLING_ONLY"
                if ACCOUNTS_FILE.exists():
                    try:
                        _accts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                        for _ac in _accts:
                            if _ac.get("customer_name") == customer and _ac.get("username") == username:
                                current_account_role = _ac.get("role", "POLLING_ONLY")
                                break
                    except Exception:
                        pass

                if not state.get("waitingForConsular") and not state.get("pending") and current_account_role != "RESERVED_BOOKING":
                    try:
                        last_background_poll, last_poll_debug, self_triggered = await _try_background_poll(
                            page, customer, username, last_background_poll, last_poll_debug
                        )
                        if self_triggered:
                            continue
                    except SessionExpiredError:
                        # Background poll detected a dead session (OFC API
                        # returned login HTML). Recover now instead of waiting
                        # for the keep-alive health check to notice minutes later.
                        print("")  # visual break
                        log.warning("🚨 Session expired during background polling! Triggering recovery...")
                        success = await recover_session(page, customer, username)
                        if not success:
                            log.error("Recovery failed after background-poll session expiry. Exiting to trigger orchestrator restart...")
                            sys.exit(1)
                        last_keep_alive = time.time()
                        continue

                # ── Keep-alive & Content Health Check ──────────────────────
                now = time.time()
                is_waiting = state.get("waitingForConsular", False)
                if now - last_keep_alive > 30.0:
                    try:
                        # 0. Check for extension's session expiry broadcast
                        expired_flag = await page.evaluate("window._extensionSessionExpired || false")
                        if expired_flag:
                            if is_waiting:
                                print("")  # visual break
                                log.warning(
                                    "🚨 Extension heartbeat detected session expiry in WAIT MODE. "
                                    "Preserving temporary OFC hold and triggering recovery..."
                                )

                                # Clear only the extension's one-shot expiry signal.
                                # DO NOT clear:
                                #   waitingForConsular
                                #   bookedOfcDate
                                #   waitStartTime
                                await page.evaluate(
                                    "window._extensionSessionExpired = false"
                                )

                                success = await recover_session(
                                    page,
                                    customer,
                                    username,
                                )

                                if not success:
                                    log.error(
                                        "Recovery failed during Consular WAIT MODE. "
                                        "Temporary OFC hold state remains preserved. "
                                        "Exiting to trigger orchestrator restart..."
                                    )
                                    sys.exit(1)

                                log.info(
                                    "✅ Session recovered during Consular WAIT MODE. "
                                    "Temporary OFC hold preserved; account remains ready "
                                    "for Scout/CVS Consular triggers."
                                )

                                last_keep_alive = time.time()
                                continue

                            else:
                                print("") # visual break
                                log.warning("🚨 Extension heartbeat detected session expiry! Triggering recovery...")
                                success = await recover_session(page, customer, username)
                                if not success:
                                    log.error("Recovery failed. Exiting to trigger orchestrator restart...")
                                    sys.exit(1)
                                last_keep_alive = time.time()
                                continue

                        # 1. Move mouse to prevent idle expiry
                        await page.mouse.move(
                            random.randint(100, 800),
                            random.randint(100, 600),
                        )
                        # 2. Check for silent expiry where URL didn't changee
                        body_text = (await page.inner_text("body")).lower()
                        matched_phrase = next((phrase for phrase in [
                            "session has expired", "please sign in", "sign in to continue", "unauthorized"
                        ] if phrase in body_text), None)
                        
                        if matched_phrase:
                            if is_waiting:
                                log.warning(f"Silent session expiry detected ('{matched_phrase}') from page content in WAIT MODE, ignoring as per preference.")
                            else:
                                print("") # visual break
                                log.warning(f"🚨 Silent session expiry detected ('{matched_phrase}') from page content! Triggering recovery...")
                                success = await recover_session(page, customer, username)
                                if not success:
                                    log.error("Recovery failed. Exiting to trigger orchestrator restart...")
                                    sys.exit(1)
                                last_keep_alive = time.time()
                                continue
                    except Exception as e:
                        log.warning(f"Keep-alive / health check failed: {e}")
                    last_keep_alive = now

                # ── Session expiry check ───────────────────────────────────
                try:
                    cur_url = page.url.lower()
                    if any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
                        if is_waiting:
                            log.warning("Session expired (URL redirect) in WAIT MODE, ignoring as per preference.")
                        else:
                            print("") # visual break
                            log.warning("⚠️ Session expired — browser redirected to login page. Triggering recovery...")
                            
                            success = await recover_session(page, customer, username)
                            if not success:
                                log.error("Recovery failed. Exiting to trigger orchestrator restart...")
                                sys.exit(1)
                            last_keep_alive = time.time()
                except Exception:
                    pass

            except KeyboardInterrupt:
                log.info("Stopped by user.")
                break
            except Exception as e:
                log.error(
                    f"Unexpected error in watch loop: {e}",
                    exc_info=True,
                )

                latest_state = _read_state(state_file)

                if latest_state.get("extension_running"):
                    _enter_booking_rest(
                        state_file,
                        customer,
                        f"Unexpected error during booking: {e}",
                    )
                else:
                    _update_state(
                        state_file,
                        {"extension_running": False},
                    )

                await asyncio.sleep(5)

    # Cleanup on exit
    _update_state(state_file, {"extension_running": False, "pending": False})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OFC Appointment Booking Runner")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--customer",  type=str, default="default")
    parser.add_argument("--username",  type=str, required=True)
    args = parser.parse_args()
    log.info(f"Starting booking runner for customer '{args.customer}' ({args.username}) on Chrome port {args.cdp_port}")
    asyncio.run(run(args.cdp_port, args.customer, args.username))