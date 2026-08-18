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
from src.booking.executor import trigger_extension_booking, trigger_extension_reschedule, trigger_extension_sniper_consular_only
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
)

load_dotenv()

POLL_INTERVAL = 0.01   # seconds between state file checks

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


async def _broadcast_scout_hit(
    matched_city: str,
    matched_date: str,
    detected_by: str,
    window_id: str,
    only_username: str = "",
    exclude_username: str = "",
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

    matched, matched_city, matched_date = (
        _match_polled_ofc_dates(
            results,
            my_config,
        )
    )

    if not matched:
        log.info(
            f"[SCOUT] No qualifying OFC date found by "
            f"{customer}."
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

    log.warning(
        f"[SCOUT] 🎯 PRE-CVS HIT: {customer} found "
        f"{matched_city} {matched_date}. "
        "Prioritizing detector booking first."
    )

    # ---------------------------------------------------------
    # DETECTOR-FIRST BOOKING
    #
    # Queue ONLY the account that actually detected the slot.
    # It must enter its own booking flow before the wider
    # scout broadcast is released.
    # ---------------------------------------------------------
    detector_queued_count = await _broadcast_scout_hit(
        matched_city,
        matched_date,
        customer,
        window_id,
        only_username=username,
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
    
    # Initialise state file — mark extension as not running on startup, but preserve pending triggers
    _update_state(state_file, {
        "extension_running": False,
        "customer_name": customer,
        "waitingForConsular": False,
        "bookedOfcDate": None,
        "waitStartTime": None
    })

    
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
        last_scout_roster_refresh = 0.0
        last_scout_window_id = ""

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
                    last_scout_roster_refresh = time.time()

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

                # A scout hit may have just queued this same account.
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
                                "scout_detector_broadcast_pending": False,
                                "scout_detector_city": "",
                                "scout_detector_date": "",
                                "scout_detector_window_id": "",
                                "scout_detector_customer": "",
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
                            log.error(
                                "429 Too Many Requests detected! "
                                "Exiting bot2 with code 42 to signal a restart."
                            )

                            if state.get("waitingForConsular"):
                                log.warning(
                                    "WAITING MODE is over (429 hit). "
                                    "Resetting flags."
                                )
                                _update_state(
                                    state_file,
                                    {
                                        "waitingForConsular": False,
                                        "bookedOfcDate": None,
                                        "waitStartTime": None,
                                    },
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
                                log.error(
                                    "🚨 Session expired during Consular "
                                    "WAIT MODE. Clearing the current wait "
                                    "state before recovery."
                                )
                                _update_state(
                                    state_file,
                                    {
                                        "waitingForConsular": False,
                                        "bookedOfcDate": None,
                                        "waitStartTime": None,
                                    },
                                )
                                context["waitingForConsular"] = False
                                context["bookedOfcDate"] = None
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
                            log.error(f"❌ Action failed for '{customer}'. [{action_type}]")
                            if state.get("waitingForConsular"):
                                log.warning("WAITING MODE is over (action failed completely). Resetting flags.")
                                _update_state(state_file, {
                                    "waitingForConsular": False,
                                    "bookedOfcDate": None,
                                    "waitStartTime": None
                                })
                                
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

                # ── Background API Polling ────────────────────────────────────
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
                                log.warning("Extension heartbeat detected session expiry in WAIT MODE, ignoring as per preference.")
                                await page.evaluate("window._extensionSessionExpired = false")
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
                        # 2. Check for silent expiry where URL didn't change
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