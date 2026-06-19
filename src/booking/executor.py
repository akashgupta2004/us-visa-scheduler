import time
import json
import logging
import asyncio
import sys
from pathlib import Path
from datetime import datetime
from playwright.async_api import Page

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from slack import send as send_slack


async def check_for_page_limit(page: Page, customerName: str, log: logging.Logger) -> bool:
    """Check if the page view limit error is present on the page."""
    try:
        url = page.url.lower()
        if "/ofc-schedule" not in url and "/schedule" not in url:
            return False

        limit_hit = await page.evaluate("() => document.body && document.body.innerText.includes('exceeded the limit for viewing this page')")
        if limit_hit:
            msg = f"❌ *Page View Limit Exceeded*: `{customerName}` has exceeded the daily limit for viewing the schedule page. Please wait 24 hours before trying again."
            log.error(msg)
            send_slack(msg)
            return True
    except Exception:
        pass
    return False


# Default city — used only for display purposes.
DEFAULT_OFC_CITY = "HYDERABAD"

# City name mapping: slot monitor format → extension format
CITY_NORMALIZE = {
    "DELHI": "NEW DELHI",
}

DATE_FORMATS = [
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
]

def normalize_city(city: str) -> str:
    """Map city names from slot monitor format to extension format."""
    upper = city.strip().upper()
    return CITY_NORMALIZE.get(upper, upper)


def parse_date_to_iso(value) -> str | None:
    """Parse various date formats and return YYYY-MM-DD (ISO) for the extension."""
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


async def trigger_extension_booking(page: Page, trigger: dict, log: logging.Logger) -> bool:
    """
    Send a window.postMessage to the extension's content script
    with the booking configuration built from the trigger file.
    """
    ofcCities = trigger.get("ofcCities")
    if not ofcCities:
        ofcCities = [normalize_city(trigger.get("ofc_city", DEFAULT_OFC_CITY))]
    else:
        ofcCities = [normalize_city(c) for c in ofcCities]

    consularCities = trigger.get("consularCities")
    if not consularCities:
        consularCities = [normalize_city(trigger.get("consular_city", ofcCities[0] if ofcCities else DEFAULT_OFC_CITY))]
    else:
        consularCities = [normalize_city(c) for c in consularCities]

    customerName = trigger.get("customer_name", "unknown")

    config = {
        "ofcCities": ofcCities,
        "ofcPriorityCity": normalize_city(trigger.get("ofcPriorityCity", ofcCities[0] if ofcCities else "")),
        "ofcStartDate": trigger.get("ofcStartDate", ""),
        "ofcEndDate": trigger.get("ofcEndDate", ""),
        "consularCities": consularCities,
        "consularPriorityCity": normalize_city(trigger.get("consularPriorityCity", consularCities[0] if consularCities else "")),
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
        "preventImmediateBooking": trigger.get("prevent_immediate", False),
    }

    log.info(f"🚀 Triggering extension booking for '{customerName}'")
    log.info(f"   OFC: {', '.join(ofcCities)} between {config['ofcStartDate']} and {config['ofcEndDate']}")
    log.info(f"   Consular: {', '.join(consularCities)} between {config['consularStartDate']} and {config['consularEndDate']}")
    log.info(f"   Config: {json.dumps(config)}")

    await page.evaluate("""
        window.__sniperResult = null;
        window.__sniperResultListener = function(event) {
            if (event.source !== window) return;
            if (event.data && event.data.action === 'SNIPER_BOOKING_RESULT') {
                window.__sniperResult = event.data;
                window.removeEventListener('message', window.__sniperResultListener);
            }
        };
        window.addEventListener('message', window.__sniperResultListener);
    """)

    await page.evaluate("""(config) => {
        window.postMessage({
            action: 'EXECUTE_SNIPER_BOOKING',
            config: config
        }, '*');
    }""", config)

    log.info("📨 Message sent to extension. Waiting for result (up to 120s) …")

    deadline = time.time() + 120
    while time.time() < deadline:
        if await check_for_page_limit(page, customerName, log):
            return False

        try:
            result = await page.evaluate("window.__sniperResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                log.info("Navigation detected while waiting for extension...")
                for _ in range(5):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in page.url:
                        log.info("✅ Booking SUCCESS (verified via URL navigation)")
                        return True
                log.warning(f"Navigated to unexpected URL: {page.url}")
                return False
            raise e

        if result is not None:
            status = result.get("status", "unknown")
            msg = result.get("msg", "No message")

            try:
                await page.evaluate("window.__sniperResult = null;")
            except Exception:
                pass

            if status == "success":
                log.info(f"✅ Booking SUCCESS: {msg}")
                return True
            else:
                log.error(f"❌ Booking FAILURE: {msg}")
                if "429" in msg:
                    raise Exception("429 Too Many Requests")
                if "Session expired" in msg:
                    raise Exception("Session expired")
                return False

        await asyncio.sleep(1)

    log.error("⏱️ Timed out waiting for extension response (120s).")
    await page.evaluate("""
        if (window.__sniperResultListener) {
            window.removeEventListener('message', window.__sniperResultListener);
        }
        window.__sniperResult = null;
    """)
    return False


async def trigger_extension_reschedule(page: Page, trigger: dict, log: logging.Logger) -> bool:
    """
    Send a window.postMessage to the extension's content script
    with the RESCHEDULE_CONSULAR configuration.
    Expects CONSULAR_RESCHEDULE_RESULT back from the content script.
    """
    consularCities = trigger.get("consularCities")
    if not consularCities:
        log.error("❌ No consularCities in trigger — cannot reschedule.")
        return False
    consularCities = [normalize_city(c) for c in consularCities]

    customerName = trigger.get("customer_name", "unknown")

    config = {
        "consularCities": consularCities,
        "consularPriorityCity": normalize_city(trigger.get("consularPriorityCity", consularCities[0] if consularCities else "")),
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
        "preventImmediateBooking": trigger.get("prevent_immediate", False),
    }

    log.info(f"📅 Triggering consular reschedule for '{customerName}'")
    log.info(f"   Cities: {', '.join(consularCities)} between {config['consularStartDate']} and {config['consularEndDate']}")
    log.info(f"   Config: {json.dumps(config)}")

    await page.evaluate("""
        window.__rescheduleResult = null;
        window.__rescheduleResultListener = function(event) {
            if (event.source !== window) return;
            if (event.data && event.data.action === 'CONSULAR_RESCHEDULE_RESULT') {
                window.__rescheduleResult = event.data;
                window.removeEventListener('message', window.__rescheduleResultListener);
            }
        };
        window.addEventListener('message', window.__rescheduleResultListener);
    """)

    await page.evaluate("""(config) => {
        window.postMessage({
            action: 'EXECUTE_CONSULAR_RESCHEDULE',
            config: config
        }, '*');
    }""", config)

    log.info("📨 Reschedule message sent to extension. Waiting for result (up to 120s) …")

    deadline = time.time() + 120
    while time.time() < deadline:
        if await check_for_page_limit(page, customerName, log):
            return False

        try:
            result = await page.evaluate("window.__rescheduleResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                log.info("Navigation detected while waiting for extension...")
                for _ in range(5):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in page.url:
                        log.info("✅ Reschedule SUCCESS (verified via URL navigation)")
                        return True
                log.warning(f"Navigated to unexpected URL: {page.url}")
                return False
            raise e

        if result is not None:
            status = result.get("status", "unknown")
            msg = result.get("msg", "No message")

            try:
                await page.evaluate("window.__rescheduleResult = null;")
            except Exception:
                pass

            if status == "success":
                log.info(f"✅ Reschedule SUCCESS: {msg}")
                return True
            else:
                log.error(f"❌ Reschedule FAILURE: {msg}")
                if "429" in msg:
                    raise Exception("429 Too Many Requests")
                if "Session expired" in msg:
                    raise Exception("Session expired")
                return False

        await asyncio.sleep(1)

    log.error("⏱️ Timed out waiting for reschedule result (120s).")
    await page.evaluate("""
        if (window.__rescheduleResultListener) {
            window.removeEventListener('message', window.__rescheduleResultListener);
        }
        window.__rescheduleResult = null;
    """)
    return False
