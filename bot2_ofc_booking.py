"""
=============================================================
  Bot2 — OFC Appointment Booker
  ─────────────────────────────────────────────────────────
  HOW TO USE:
  1. Run bot.py first and let it fully log in.
  2. Then run this script:  python bot2_ofc_booking.py
  3. Then run:  python slot_monitor_qualified.py

  Bot2 connects to the already-authenticated Chrome,
  navigates to the OFC schedule page, selects your city,
  and parks there waiting for trigger.json.

  When slot_monitor writes trigger.json, Bot2 books
  the appointment immediately and then re-parks.
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
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, expect

# ─────────────────────────────────────────────────────────────
load_dotenv()

# Default city — used only for initial warm-up parking.
# Actual booking city is read dynamically from trigger.json.
DEFAULT_OFC_CITY = "HYDERABAD"

OFC_POST_MAP = {
    "CHENNAI":   "CHENNAI VAC",
    "MUMBAI":    "MUMBAI VAC",
    "HYDERABAD": "HYDERABAD VAC",
    "KOLKATA":   "KOLKATA VAC",
    "DELHI":     "NEW DELHI VAC",
}

OFC_URL        = "https://www.usvisascheduling.com/en-US/ofc-schedule"
POLL_INTERVAL  = 0.5   # seconds between trigger.json checks

DATE_FORMATS = [
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
]

# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BOT2] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot2")

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

async def fast_click(page: Page, selector: str) -> None:
    """Click without any artificial delay."""
    await page.locator(selector).first.click()


async def human_delay(min_ms: int = 60, max_ms: int = 180) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def parse_date(value) -> datetime | None:
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────
# Connect to existing Chrome session
# ─────────────────────────────────────────────────────────────

async def connect_to_chrome(playwright, cdp_port: int):
    log.info(f"Connecting to Chrome on ws://127.0.0.1:{cdp_port} …")
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[-1] if context.pages else await context.new_page()

    async def handle_dialog(dialog):
        try:
            await dialog.accept()
        except Exception:
            pass
    
    page.on("dialog", handle_dialog)
    log.info(f"Connected — current page: {page.url}")
    return browser, page


# ─────────────────────────────────────────────────────────────
# Navigate to OFC Schedule page
# ─────────────────────────────────────────────────────────────

async def navigate_to_ofc(page: Page) -> bool:
    """Navigate to the OFC schedule page. Returns True on success."""
    log.info(f"Navigating to OFC schedule page …")
    try:
        await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("select#post_select", state="visible", timeout=30_000)
        log.info("OFC schedule page loaded.")
        return True
    except Exception as e:
        log.error(f"Failed to navigate to OFC page: {e}")
        try:
            await page.screenshot(path="ofc_nav_error.png")
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────
# Select OFC City Dropdown
# ─────────────────────────────────────────────────────────────

async def select_ofc_post(page: Page, city: str) -> bool:
    post_label = OFC_POST_MAP.get(city.upper())
    if not post_label:
        log.error(f"City '{city}' not found in OFC_POST_MAP. Options: {list(OFC_POST_MAP.keys())}")
        return False

    log.info(f"Selecting OFC post: {post_label}")
    try:
        await page.select_option("select#post_select", label=post_label)
        log.info(f"Selected '{post_label}'.")
        return True
    except Exception as e:
        log.error(f"Failed to select post: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Wait for calendar to load
# ─────────────────────────────────────────────────────────────

async def wait_for_calendar(page: Page, timeout_s: int = 30) -> str:
    """Returns 'loaded', 'no_slots', or 'timeout'."""
    log.info("Waiting for calendar to load (or 'No Slots Available') …")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            # Check if calendar is loaded
            msg_el = page.locator("p#datepicker-message")
            if await msg_el.count() > 0:
                txt = (await msg_el.inner_text()).strip().lower()
                if txt == "select date":
                    log.info("Calendar loaded.")
                    return "loaded"

            # Check if "No Slots Available" is displayed
            body_text = await page.locator("body").inner_text()
            if "No Slots Available" in body_text:
                log.info("Page displays 'No Slots Available'.")
                return "no_slots"
                
        except Exception:
            pass
        await asyncio.sleep(0.5)
        
    log.error("Calendar did not load in time.")
    return "timeout"


# ─────────────────────────────────────────────────────────────
# Navigate Calendar to Target Month
# ─────────────────────────────────────────────────────────────

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


async def get_displayed_month_year(page: Page) -> tuple[int, int] | None:
    """Returns (month_1indexed, year) of the FIRST datepicker panel."""
    try:
        # The left panel title — first .ui-datepicker-title
        title_loc = page.locator(".ui-datepicker-group-first .ui-datepicker-title")
        month_sel = title_loc.locator("select.ui-datepicker-month")
        year_sel  = title_loc.locator("select.ui-datepicker-year")
        month_val = int(await month_sel.input_value())   # 0-indexed
        year_val  = int(await year_sel.input_value())
        return (month_val + 1, year_val)   # return 1-indexed month
    except Exception as e:
        log.warning(f"Could not read calendar month/year: {e}")
        return None


async def navigate_to_month(page: Page, target_dt: datetime) -> bool:
    """Click Next on the calendar until we reach the target month/year."""
    target_month = target_dt.month
    target_year  = target_dt.year

    for i in range(24):   # max 24 months forward
        mv = await get_displayed_month_year(page)
        if mv is None:
            await asyncio.sleep(0.5)
            continue

        current_month, current_year = mv

        if current_year == target_year and current_month == target_month:
            log.info(f"Calendar is on correct month: {MONTHS[target_month-1]} {target_year}")
            return True

        # Need to advance
        if (current_year * 12 + current_month) > (target_year * 12 + target_month):
            log.error(f"Calendar is PAST the target date. Cannot go back.")
            return False

        log.info(f"Advancing calendar from {MONTHS[current_month-1]} {current_year} …")
        try:
            next_btn = page.locator(".ui-datepicker-next").first
            await next_btn.click()
            await asyncio.sleep(0.3)
        except Exception as e:
            log.error(f"Could not click Next on calendar: {e}")
            return False

    log.error("Could not reach target month within 24 clicks.")
    return False


# ─────────────────────────────────────────────────────────────
# Click Target Date
# ─────────────────────────────────────────────────────────────

async def click_target_date(page: Page, target_dt: datetime) -> bool:
    """Find and click the target day on the calendar (must be greenday)."""
    day = target_dt.day
    log.info(f"Looking for greenday: day {day}")

    try:
        # All non-disabled cells in the FIRST panel (left calendar)
        # that have class greenday and text matching our day
        green_cells = page.locator("td.greenday:not(.ui-state-disabled)")
        count = await green_cells.count()

        for i in range(count):
            cell = green_cells.nth(i)
            span = cell.locator("a.ui-state-default, span.ui-state-default")
            if await span.count() == 0:
                continue
            text = (await span.first.inner_text()).strip()
            if text == str(day):
                log.info(f"Found green day {day}, clicking …")
                await cell.click()
                return True

        log.warning(f"Day {day} is not available (not green) on the calendar.")
        return False

    except Exception as e:
        log.error(f"Error clicking target date: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Select First Available Time Slot
# ─────────────────────────────────────────────────────────────

async def select_time_slot(page: Page) -> bool:
    log.info("Waiting for time slots …")
    try:
        await page.wait_for_selector(
            "table#time_select input[name='schedule-entries']",
            state="visible",
            timeout=15_000
        )
        radio_buttons = page.locator("input[name='schedule-entries']")
        count = await radio_buttons.count()
        if count == 0:
            log.error("No time slot radio buttons found.")
            return False

        # Pick first slot with available slots > 0
        for i in range(count):
            rb = radio_buttons.nth(i)
            slots = await rb.get_attribute("data-slots")
            try:
                if int(slots or 0) >= 1:
                    await asyncio.sleep(random.uniform(0.3, 0.8)) # Human jitter
                    await rb.click()
                    log.info(f"Selected time slot {i+1} (slots available: {slots})")
                    return True
            except (ValueError, TypeError):
                continue

        log.error("All visible time slots have 0 availability.")
        return False

    except Exception as e:
        log.error(f"Failed selecting time slot: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Submit Booking
# ─────────────────────────────────────────────────────────────

async def submit_booking(page: Page) -> bool:
    """Organically wait for the submit button to become active, then naturally click it."""
    log.info("Waiting for Submit button to become active naturally…")
    try:
        btn = page.locator("input#submitbtn")

        # Wait until the page's event listener enables the button natively
        # (This avoids bot detection via JavaScript injection)
        await expect(btn).to_be_enabled(timeout=10_000)

        log.info("Waiting 1 second before final submit (human delay)...")
        await asyncio.sleep(1.0)  

        # Natural click
        await btn.click()
        log.info("✅ Submit clicked! Waiting for confirmation …")

        # Wait for redirect OR error message (up to 60s)
        confirm_deadline = time.time() + 60
        while time.time() < confirm_deadline:
            if "ofc-schedule" not in page.url:
                log.info(f"🎉 Redirected to: {page.url}")
                return True

            try:
                err_el = page.locator("#error_row .alert-danger")
                if await err_el.count() > 0 and await err_el.is_visible():
                    err_text = (await err_el.inner_text()).strip()
                    if err_text:
                        log.error(f"Booking error from server: {err_text}")
                        return False
            except Exception:
                pass

            await asyncio.sleep(1)

        log.warning("Submit clicked but no redirect or error within 60s.")
        return False

    except Exception as e:
        log.error(f"Submit error: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Read & delete trigger file
# ─────────────────────────────────────────────────────────────

def read_trigger(trigger_file: Path) -> dict | None:
    if not trigger_file.exists():
        return None
    try:
        data = json.loads(trigger_file.read_text(encoding="utf-8"))
        trigger_file.unlink(missing_ok=True)
        return data
    except Exception as e:
        log.warning(f"Could not read {trigger_file.name}: {e}")
        return None


def delete_trigger(trigger_file: Path):
    try:
        trigger_file.unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Warm-up: park on OFC calendar page (blank state)
# ─────────────────────────────────────────────────────────────

async def warmup(page: Page, trigger_file: Path, city: str | None = None) -> bool:
    """Navigate to OFC page, verify dropdown is there, but sit on blank. Returns True on success."""
    if not await navigate_to_ofc(page):
        return False

    display_city = city or DEFAULT_OFC_CITY
    log.info("=" * 60)
    log.info(f"✅ Bot2 warm-up complete. Parked on blank dropdown.")
    log.info(f"   Default City: {display_city} → {OFC_POST_MAP.get(display_city, '?')}")
    log.info(f"   Watching for {trigger_file.name} every {POLL_INTERVAL}s …")
    log.info("=" * 60)
    return True


# ─────────────────────────────────────────────────────────────
# Booking sequence
# ─────────────────────────────────────────────────────────────

async def book(page: Page, trigger: dict) -> bool:
    ofc_date_str = trigger.get("ofc_date", "")
    ofc_city = trigger.get("ofc_city", DEFAULT_OFC_CITY).upper()
    customer = trigger.get("customer_name", "unknown")
    target_dt = parse_date(ofc_date_str)
    if not target_dt:
        log.error(f"Cannot parse date from trigger.json: '{ofc_date_str}'")
        return False

    if ofc_city not in OFC_POST_MAP:
        log.error(f"City '{ofc_city}' from trigger not in OFC_POST_MAP. Options: {list(OFC_POST_MAP.keys())}")
        return False

    log.info(f"🚀 BOOKING triggered for customer '{customer}' — city: {ofc_city}, date: {target_dt.strftime('%d %b %Y')}")

    # Refresh calendar to get latest state (page might still show old month)
    # We just reload the post selection to re-trigger the AJAX
    if not await select_ofc_post(page, ofc_city):
        return False
        
    cal_status = await wait_for_calendar(page)
    if cal_status == "timeout":
        return False
    if cal_status == "no_slots":
        log.warning("Portal still says 'No Slots Available'. Slot may have been taken.")
        return False

    if not await navigate_to_month(page, target_dt):
        return False

    if not await click_target_date(page, target_dt):
        log.warning("Target date not available (no green day). Slot may have vanished.")
        return False

    if not await select_time_slot(page):
        return False

    if not await submit_booking(page):
        return False

    return True


# ─────────────────────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────────────────────

async def run(cdp_port: int, customer: str):
    if DEFAULT_OFC_CITY not in OFC_POST_MAP:
        log.error(f"DEFAULT_OFC_CITY='{DEFAULT_OFC_CITY}' is not valid. Options: {list(OFC_POST_MAP.keys())}")
        sys.exit(1)

    trigger_file = Path(__file__).parent / f"trigger_{customer}.json"

    # Delete any stale trigger file from previous runs
    delete_trigger(trigger_file)

    async with async_playwright() as pw:
        browser, page = await connect_to_chrome(pw, cdp_port)

        # Initial warm-up — retry up to 3 times
        warmed = False
        for attempt in range(1, 4):
            log.info(f"Warm-up attempt {attempt}/3 …")
            if await warmup(page, trigger_file):
                warmed = True
                break
            await asyncio.sleep(5)

        if not warmed:
            log.error("Warm-up failed after 3 attempts. Exiting.")
            sys.exit(1)

        # Main watch loop variables
        last_keep_alive = time.time()

        while True:
            try:
                # ── KEEP ALIVE LOGIC ──
                # Every 60 seconds, move the mouse slightly to prevent logout
                now = time.time()
                if now - last_keep_alive > 60.0:
                    try:
                        x = random.randint(100, 800)
                        y = random.randint(100, 600)
                        await page.mouse.move(x, y)
                        log.info("🖱️ Keep-alive: mouse moved to prevent timeout.")
                    except Exception as e:
                        log.warning(f"Keep-alive mouse move failed: {e}")
                    last_keep_alive = now

                # ── TRIGGER CHECK ──
                trigger = read_trigger(trigger_file)
                if trigger is None:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                log.info(f"📥 {trigger_file.name} received: {trigger}")
                trigger_customer = trigger.get("customer_name", "unknown")
                trigger_city = trigger.get("ofc_city", DEFAULT_OFC_CITY).upper()
                success = await book(page, trigger)

                if success:
                    log.info("=" * 60)
                    log.info(f"✅ OFC APPOINTMENT BOOKED SUCCESSFULLY for '{trigger_customer}' in {trigger_city}!")
                    log.info("=" * 60)
                    try:
                        await page.screenshot(path="ofc_booked.png")
                    except Exception:
                        pass
                else:
                    log.error(f"❌ Booking failed for '{trigger_customer}'. Re-warming …")
                    try:
                        await page.screenshot(path="ofc_error.png")
                    except Exception:
                        pass

                # Re-warm so we're ready for the next potential trigger
                log.info("Re-warming on OFC page …")
                for attempt in range(1, 4):
                    if await warmup(page, trigger_file):
                        break
                    await asyncio.sleep(5)
                
                # Reset keep alive timer after re-warming
                last_keep_alive = time.time()

            except KeyboardInterrupt:
                log.info("Stopped by user.")
                break
            except Exception as e:
                log.error(f"Unexpected error in watch loop: {e}", exc_info=True)
                delete_trigger(trigger_file)
                await asyncio.sleep(5)


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bot2 OFC Booker (Per-Customer Isolated Loop)")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Chrome debug port (e.g. 9222)")
    parser.add_argument("--customer", type=str, default="default", help="Customer name for the trigger file")
    args = parser.parse_args()

    log.info(f"Starting Bot2 for customer '{args.customer}' on Chrome port {args.cdp_port}")
    asyncio.run(run(args.cdp_port, args.customer))
