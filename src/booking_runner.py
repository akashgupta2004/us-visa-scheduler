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
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from src.auth.browser import connect_to_chrome
from src.booking.cdp_client import ensure_on_portal
from src.booking.executor import trigger_extension_booking, trigger_extension_reschedule
from slack import send_slack
from slack import send_slack_error

# Add new imports for recovery
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question

load_dotenv()

ACCOUNTS_FILE = Path(__file__).parent.parent / "accounts.json"

POLL_INTERVAL = 0.5   # seconds between state file checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOOKING_RUNNER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("booking_runner")

def safe_id(username: str) -> str:
    """Generate a filesystem-safe unique identifier from a username/email."""
    return re.sub(r'[^a-zA-Z0-9]', '_', str(username))

# ─── State file helpers ───────────────────────────────────────────────────────

def _read_state(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _set_flag(state_file: Path, **flags):
    """Atomically update one or more top-level keys in the state file."""
    state = _read_state(state_file)
    state.update(flags)
    _write_state(state_file, state)

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
        
    # 2. Trigger redirect by simply reloading the page
    try:
        log.info("Reloading the page to trigger session validation...")
        await page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error(f"Failed during page reload: {e}")
    
    # 3. Handle waiting room
    try:
        await wait_for_waiting_room(page, log, timeout_minutes=120)
    except Exception as e:
        log.error(f"Error waiting for waiting room during recovery: {e}")
        return False
        
    # 4. Check if we reached login
    cur_url = page.url.lower()
    if not any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
        if "usvisascheduling.com" in cur_url and any(k in cur_url for k in ["/schedule", "/en-us"]):
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
    existing_state = _read_state(state_file)
    existing_state.update({
        "extension_running": False,
        "customer_name": customer,
    })
    _write_state(state_file, existing_state)

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
                    try:
                        if action_type == "SNIPER":
                            log.info("🎯 Action type: SNIPER (OFC+Consular booking)")
                            success = await trigger_extension_booking(page, trigger, log)
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
                            sys.exit(42)
                        if "Session expired" in str(e):
                            log.error("Session expired during action. Triggering recovery...")
                            # Trigger recovery
                            await recover_session(page, customer, username)

                    if success:
                        log.info("=" * 60)
                        log.info(f"✅ ACTION COMPLETED SUCCESSFULLY for '{customer}'! [{action_type}]")
                        log.info("=" * 60)
                        send_slack(f"🎉 *BOOKING SUCCESSFUL* 🎉\n*Customer / ID:* `{customer}`\n*Type:* `{action_type}`\n✅ The appointment has been successfully scheduled!")
                    else:
                        log.error(f"❌ Action failed for '{customer}'. [{action_type}]")

                    # Mark extension as done
                    _set_flag(state_file, extension_running=False)
                    last_keep_alive = time.time()
                    
                    await asyncio.sleep(0.5)
                    continue

                # ── If NO trigger, do maintenance ──────────────────────────────
                await asyncio.sleep(0.2)

                # ── Keep-alive & Content Health Check ──────────────────────
                now = time.time()
                if now - last_keep_alive > 30.0:
                    try:
                        # 0. Check for extension's session expiry broadcast
                        expired_flag = await page.evaluate("window._extensionSessionExpired || false")
                        if expired_flag:
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
                        body_text = (await page.inner_text("body", timeout=2000)).lower()
                        if any(phrase in body_text for phrase in [
                            "session has expired", "please sign in", "sign in to continue", "unauthorized"
                        ]):
                            print("") # visual break
                            log.warning("⚠️ Silent session expiry detected from page content. Triggering recovery...")
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
