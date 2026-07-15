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
from src.common.state import read_state as _read_state, write_state as _write_state, update_state as _update_state
from src.common.config import ACCOUNTS_FILE
from slack import send_slack
from src.common.db_logger import MongoDBHandler, MongoDBLogger

# Add new imports for recovery
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question
from src.polling_runner import fetch_dates_via_browser

load_dotenv()

POLL_INTERVAL = 0.5   # seconds between state file checks

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


async def _try_background_poll(page, customer: str, username: str, last_background_poll: float, last_poll_debug: float) -> tuple[float, float]:
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
                res = await fetch_dates_via_browser(page)
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

    return last_background_poll, last_poll_debug

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

    last_polling_time = 0
    next_poll_delay = random.randint(180, 240)
    
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

        while True:
            try:
                # ── Check for pending trigger FIRST ───────────────────────────────
                state = _read_state(state_file)

                if state.get("pending"):
                    print("\n" + "=" * 60) # Visual break
                    # Mark extension as running before we start
                    _update_state(state_file, {"extension_running": True, "pending": False})
                    log.info(f"📥 Pending trigger detected for '{customer}'.")

                    try:
                        file_age = time.time() - os.path.getmtime(state_file)
                        if file_age > 30.0:
                            log.warning(f"⏭️ Skipping trigger: it is {file_age:.1f} seconds old (> 30s limit).")
                            continue
                    except Exception as e:
                        log.warning(f"Could not check trigger file age: {e}")

                    trigger_ts = state.get("trigger_timestamp")
                    if trigger_ts:
                        delay = time.time() - trigger_ts
                        if delay > 120.0:
                            log.warning(f"⚠️ Trigger execution delayed by {delay:.1f} seconds! Continuing anyway (clock skew possible).")
                        elif delay > 10.0:
                            reason = "Bot was busy or in Cloudflare queue."
                            log.warning(f"⚠️ Trigger execution delayed by {delay:.1f} seconds! Reason: {reason}")
                        else:
                            log.info(f"⚡ Trigger picked up swiftly in {delay:.3f} seconds.")

                    action_type = state.get("action_type")

                    trigger = {k: state[k] for k in [
                        "action_type",
                        "ofcCities", "ofcPriorityCity", "ofcStartDate", "ofcEndDate",
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
                            success, context = await trigger_extension_booking(page, trigger, log)
                        elif action_type == "RESCHEDULE_FULL":
                            log.info(f"🔄 Action type: {action_type}")
                            success, context = await trigger_extension_booking(page, trigger, log)
                        elif action_type == "SNIPER_CONSULAR_ONLY":
                            log.info("🎯 Action type: SNIPER_CONSULAR_ONLY (Fallback)")
                            bookedOfcDate = state.get("bookedOfcDate", "")
                            success, context = await trigger_extension_sniper_consular_only(page, trigger, bookedOfcDate, log)
                        elif action_type == "RESCHEDULE_FULL_CONSULAR_ONLY":
                            log.info("🔄 Action type: RESCHEDULE_FULL_CONSULAR_ONLY (Fallback)")
                            bookedOfcDate = state.get("bookedOfcDate", "")
                            success, context = await trigger_extension_sniper_consular_only(page, trigger, bookedOfcDate, log)
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
                            log.error("429 Too Many Requests detected! Exiting bot2 with code 42 to signal a restart.")
                            if state.get("waitingForConsular"):
                                log.warning("WAITING MODE is over (429 hit). Resetting flags.")
                                _update_state(state_file, {
                                    "waitingForConsular": False,
                                    "bookedOfcDate": None,
                                    "waitStartTime": None
                                })
                            sys.exit(42)
                        if "Session expired" in str(e):
                            is_waiting = state.get("waitingForConsular", False)
                            if is_waiting:
                                log.error("🚨 Session expired during Consular WAIT MODE. The server will drop the OFC booking. Abandoning wait mode and restarting full flow...")
                                _update_state(state_file, {
                                    "waitingForConsular": False,
                                    "bookedOfcDate": None,
                                    "waitStartTime": None
                                })
                                context["waitingForConsular"] = False
                                context["bookedOfcDate"] = None
                            else:
                                log.error("Session expired during action. Triggering recovery...")
                            
                            # Trigger recovery for both cases
                            recovered = await recover_session(page, customer, username)
                            if not recovered:
                                log.error("Recovery failed after action. Exiting to trigger orchestrator restart...")
                                sys.exit(1)
                            else:
                                if is_waiting:
                                    log.info("Recovery successful! However, OFC slot is lost due to session expiry. Abandoning trigger.")
                                    _update_state(state_file, {"extension_running": False})
                                else:
                                    log.info("Recovery successful! Re-queueing the trigger to retry the booking.")
                                    _update_state(state_file, {"pending": True, "extension_running": False})
                                continue

                    if success:
                        log.info("=" * 60)
                        log.info(f"✅ ACTION COMPLETED SUCCESSFULLY for '{customer}'! [{action_type}]")
                        log.info("=" * 60)
                        
                        _update_state(state_file, {
                            "waitingForConsular": False,
                            "bookedOfcDate": None,
                            "waitStartTime": None,
                            "completed": True
                        })
                        send_slack(f"🎉 *BOOKING SUCCESSFUL* 🎉\n*Customer / ID:* `{customer}`\n*Type:* `{action_type}`\n✅ The appointment has been successfully scheduled!")
                    else:
                        if context.get("waitingForConsular"):
                            log.warning("=" * 60)
                            log.warning(f"⏳ PARTIAL BOOKING / STILL WAITING for '{customer}'! Transitioning to WAIT MODE...")
                            log.warning("=" * 60)
                            _update_state(state_file, {
                                "waitingForConsular": True,
                                "bookedOfcDate": context.get("bookedOfcDate"),
                                "waitStartTime": state.get("waitStartTime") or time.time(), # Preserve start time if already waiting
                                "extension_running": False,
                                "pending": False
                            })
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

                    # Mark extension as done
                    _update_state(state_file, {"extension_running": False})
                    last_keep_alive = time.time()
                    
                    await asyncio.sleep(0.5)
                    continue

                # ── If NO trigger, do maintenance ──────────────────────────────
                await asyncio.sleep(0.2)
                
                # ── Delayed Polling for Consular ──────────────────────────────
                state = _read_state(state_file)
                if state.get("waitingForConsular") and not state.get("pending"):
                    wait_start = state.get("waitStartTime")
                    if wait_start is None:
                        wait_start = time.time()
                    elapsed = time.time() - wait_start
                    # Poll continuously at intervals of roughly 1 minute
                    if (time.time() - last_polling_time) > next_poll_delay:
                        log.info(f"⏱️ Periodic Consular poll ({elapsed:.0f}s elapsed in wait mode). Triggering manual check.")
                        _update_state(state_file, {
                            "pending": True,
                            "action_type": "SNIPER_CONSULAR_ONLY",
                            "trigger_timestamp": time.time()
                        })
                        last_polling_time = time.time()
                        next_poll_delay = random.randint(55, 65)  # 1 minute +/- 5 seconds
                        continue

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
                        last_background_poll, last_poll_debug = await _try_background_poll(
                            page, customer, username, last_background_poll, last_poll_debug
                        )
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
                log.error(f"Unexpected error in watch loop: {e}", exc_info=True)
                _update_state(state_file, {"extension_running": False})
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