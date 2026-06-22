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
from src.common.state import read_state as _read_state, write_state as _write_state, set_flag as _set_flag, update_state as _update_state
from src.common.config import ACCOUNTS_FILE
from slack import send_slack
from slack import send_slack_error

# Add new imports for recovery
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question

load_dotenv()

POLL_INTERVAL = 0.5   # seconds between state file checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOOKING_RUNNER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("booking_runner")

# ─── State file helpers (wrappers around shared state module) ─────────────────

# ─── Session Recovery ─────────────────────────────────────────────────────────

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
        await page.wait_for_url("**/*usvisascheduling.com/en-US*", timeout=30_000)
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

    last_polling_time = 0  # For the 12-minute delayed polling

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        from datetime import datetime
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
        last_activity_time = time.time()

        while True:
            try:
                # ── Check for pending trigger FIRST ───────────────────────────────
                state = _read_state(state_file)

                if state.get("pending"):
                    print("\n" + "=" * 60) # Visual break
                    # Mark extension as running before we start
                    _set_flag(state_file, extension_running=True, pending=False)
                    last_activity_time = time.time()
                    log.info(f"📥 Pending trigger detected for '{customer}'.")

                    trigger_ts = state.get("trigger_timestamp")
                    if trigger_ts:
                        delay = time.time() - trigger_ts
                        if delay > 10.0:
                            reason = "Unknown"
                            if time.time() - runner_start_time < delay + 10:
                                reason = "Bot was in the middle of restarting and logging in when the slot dropped."
                            else:
                                reason = "Bot was busy performing a scheduled keep-alive / stuck in a Cloudflare queue."
                                
                            log.warning(f"⚠️ Trigger execution delayed by {delay:.1f} seconds! Reason: {reason}")
                        else:
                            log.info(f"⚡ Trigger picked up swiftly in {delay:.3f} seconds.")

                    action_type = state.get("action_type")

                    trigger = {k: state[k] for k in [
                        "action_type",
                        "ofcCities", "ofcPriorityCity", "ofcStartDate", "ofcEndDate",
                        "consularCities", "consularPriorityCity", "consularStartDate", "consularEndDate",
                        "customer_name", "prevent_immediate"
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
                        elif action_type == "SNIPER_CONSULAR_ONLY":
                            log.info("🎯 Action type: SNIPER_CONSULAR_ONLY (Fallback)")
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
                            await recover_session(page, customer, username)

                    if success:
                        log.info("=" * 60)
                        log.info(f"✅ ACTION COMPLETED SUCCESSFULLY for '{customer}'! [{action_type}]")
                        log.info("=" * 60)
                        
                        # Fully complete
                        _update_state(state_file, {
                            "waitingForConsular": False,
                            "bookedOfcDate": None,
                            "waitStartTime": None
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
                                "waitStartTime": state.get("waitStartTime", time.time()), # Preserve start time if already waiting
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
                    _set_flag(state_file, extension_running=False)
                    last_keep_alive = time.time()
                    
                    await asyncio.sleep(0.5)
                    continue

                # ── If NO trigger, do maintenance ──────────────────────────────
                await asyncio.sleep(0.2)
                
                # ── Delayed Polling for Consular ──────────────────────────────
                state = _read_state(state_file)
                if state.get("waitingForConsular") and not state.get("pending"):
                    wait_start = state.get("waitStartTime", time.time())
                    elapsed = time.time() - wait_start
                    # After 12 minutes (720 seconds), poll every 4 minutes (240 seconds)
                    if elapsed > 720 and (time.time() - last_polling_time) > 240:
                        log.info(f"⏱️ 12-minute wait exceeded ({elapsed:.0f}s elapsed). Triggering manual Consular poll.")
                        _update_state(state_file, {
                            "pending": True,
                            "action_type": "SNIPER_CONSULAR_ONLY",
                            "trigger_timestamp": time.time()
                        })
                        last_polling_time = time.time()
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
                        body_text = (await page.content()).lower()
                        if any(phrase in body_text for phrase in [
                            "session has expired", "please sign in", "sign in to continue", "unauthorized"
                        ]):
                            if is_waiting:
                                log.warning("Silent session expiry detected from page content in WAIT MODE, ignoring as per preference.")
                            else:
                                print("") # visual break
                                log.warning("🚨 Silent session expiry detected from page content! Triggering recovery...")
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
                _set_flag(state_file, extension_running=False)
                await asyncio.sleep(5)

    # Cleanup on exit
    _set_flag(state_file, extension_running=False, pending=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OFC Appointment Booking Runner")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--customer",  type=str, default="default")
    parser.add_argument("--username",  type=str, required=True)
    args = parser.parse_args()
    log.info(f"Starting booking runner for customer '{args.customer}' ({args.username}) on Chrome port {args.cdp_port}")
    asyncio.run(run(args.cdp_port, args.customer, args.username))
