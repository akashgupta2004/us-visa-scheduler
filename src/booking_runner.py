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
from src.booking.executor import trigger_extension_booking

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

    # Initialise state file — mark extension as not running on startup
    _write_state(state_file, {
        "extension_running": False,
        "pending": False,
        "customer_name": customer,
    })

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

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)

                # ── Keep-alive ─────────────────────────────────────────────
                now = time.time()
                if now - last_keep_alive > 60.0:
                    try:
                        await page.mouse.move(
                            random.randint(100, 800),
                            random.randint(100, 600),
                        )
                    except Exception as e:
                        log.warning(f"Keep-alive failed: {e}")
                    last_keep_alive = now

                # ── Check for pending trigger ───────────────────────────────
                state = _read_state(state_file)

                if not state.get("pending"):
                    continue   # nothing to do

                # Mark extension as running before we start
                _set_flag(state_file, extension_running=True, pending=False)
                log.info(f"📥 Pending trigger detected for '{customer}' — starting booking.")

                trigger = {k: state[k] for k in [
                    "ofcCities", "ofcStartDate", "ofcEndDate",
                    "consularCities", "consularStartDate", "consularEndDate",
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

                # ── Execute booking ────────────────────────────────────────
                try:
                    success = await trigger_extension_booking(page, trigger, log)
                except Exception as e:
                    log.error(f"Booking error: {e}", exc_info=True)
                    success = False

                if success:
                    log.info("=" * 60)
                    log.info(f"✅ BOOKING DELEGATED SUCCESSFULLY for '{customer}'!")
                    log.info("=" * 60)
                    try:
                        await page.screenshot(path="ofc_booked.png")
                    except Exception:
                        pass
                else:
                    log.error(f"❌ Booking failed for '{customer}'.")
                    try:
                        await page.screenshot(path="ofc_error.png")
                    except Exception:
                        pass

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
