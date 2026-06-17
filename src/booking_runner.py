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

from src.auth.browser import connect_to_chrome
from src.booking.cdp_client import ensure_on_portal
from src.booking.executor import trigger_extension_booking, trigger_extension_reschedule
from slack import send_slack

load_dotenv()

POLL_INTERVAL = 0.5   # seconds between state file checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOOKING_RUNNER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("booking_runner")


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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(cdp_port: int, customer: str):
    state_file = Path(__file__).parent / f"state_{customer}.json"

    # Initialise state file — mark extension as not running on startup, but preserve pending triggers
    existing_state = _read_state(state_file)
    existing_state.update({
        "extension_running": False,
        "customer_name": customer,
    })
    _write_state(state_file, existing_state)

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        log.info("Waiting for portal …")
        if not await ensure_on_portal(page, log):
            log.error("Could not reach portal. Exiting.")
            sys.exit(1)

        log.info("=" * 60)
        log.info(f"✅ Booking runner ready — watching {state_file.name}")
        log.info("=" * 60)

        last_keep_alive = time.time()
        last_activity_time = time.time()
        keep_alive_interval = random.uniform(600.0, 900.0)

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)

                # ── Keep-alive & Content Health Check ──────────────────────
                now = time.time()
                if now - last_keep_alive > 60.0:
                    try:
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
                            log.error("⚠️ Silent session expiry detected from page content. Exiting to trigger restart...")
                            sys.exit(1)
                    except Exception as e:
                        log.warning(f"Keep-alive / health check failed: {e}")
                    last_keep_alive = now

                # ── Session expiry check ───────────────────────────────────
                try:
                    cur_url = page.url.lower()
                    if any(k in cur_url for k in ["b2clogin", "logon", "login", "signin", "sign-in"]):
                        log.error("⚠️ Session expired — browser redirected to login page. Exiting to trigger restart...")
                        sys.exit(1)
                except Exception:
                    pass

                # ── 10-15 Minute Inactivity Keep-Alive ────────────────────────
                if now - last_activity_time > keep_alive_interval:
                    log.warning(f"⏱️ No activity/slots for {keep_alive_interval/60:.1f} minutes. Performing keep-alive...")
                    try:
                        cur_url_lower = page.url.lower()
                        is_home = cur_url_lower.endswith("/en-us/") or cur_url_lower.endswith("/en-us")
                        
                        if is_home:
                            log.info("Currently on home page. Clicking a dashboard button and staying there...")
                            loc_reschedule = page.locator("a:has-text('Reschedule Appointment')")
                            loc_schedule = page.locator("a:has-text('Schedule Appointment')")
                            
                            button_clicked = False
                            if await loc_reschedule.count() > 0 and await loc_reschedule.first.is_visible():
                                await loc_reschedule.first.click()
                                log.info("Clicked 'Reschedule Appointment'")
                                button_clicked = True
                            elif await loc_schedule.count() > 0 and await loc_schedule.first.is_visible():
                                await loc_schedule.first.click()
                                log.info("Clicked 'Schedule Appointment'")
                                button_clicked = True
                            else:
                                log.info("Dashboard buttons not found, but page was reloaded.")
                                
                            if button_clicked:
                                await asyncio.sleep(3)  # Wait for page navigation to begin
                                try:
                                    from src.auth.login import wait_for_waiting_room
                                    await wait_for_waiting_room(page, log, timeout_minutes=10)
                                except Exception as e:
                                    log.error(f"Error handling Cloudflare after button click: {e}")
                        else:
                            log.info("Not on home page. Directing back to home page and staying there...")
                            await page.goto("https://www.usvisascheduling.com/en-US/", wait_until="domcontentloaded")
                            try:
                                from src.auth.login import wait_for_waiting_room
                                await wait_for_waiting_room(page, log, timeout_minutes=10)
                            except Exception as e:
                                log.error(f"Error handling Cloudflare after keep-alive: {e}")
                            log.info("Returned to dashboard home page.")
                            
                    except Exception as e:
                        log.warning(f"Failed keep-alive action: {e}")
                        
                    last_activity_time = time.time()
                    keep_alive_interval = random.uniform(600.0, 900.0)

                # ── Check for pending trigger ───────────────────────────────
                state = _read_state(state_file)

                if not state.get("pending"):
                    continue   # nothing to do

                # Mark extension as running before we start
                _set_flag(state_file, extension_running=True, pending=False)
                last_activity_time = time.time()
                log.info(f"📥 Pending trigger detected for '{customer}'.")

                action_type = state.get("action_type")

                trigger = {k: state[k] for k in [
                    "ofcCities", "ofcPriorityCity", "ofcStartDate", "ofcEndDate",
                    "consularCities", "consularPriorityCity", "consularStartDate", "consularEndDate",
                    "customer_name",
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
                        from src.auth.login import wait_for_waiting_room
                        await wait_for_waiting_room(page, log, timeout_minutes=15)
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
                        log.error("Session expired during action. Exiting to trigger restart...")
                        sys.exit(1)

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
    args = parser.parse_args()
    log.info(f"Starting booking runner for customer '{args.customer}' on Chrome port {args.cdp_port}")
    asyncio.run(run(args.cdp_port, args.customer))
