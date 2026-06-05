import time
import json
import logging
import asyncio
from datetime import datetime
from playwright.async_api import Page

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
        "ofcStartDate": trigger.get("ofcStartDate", ""),
        "ofcEndDate": trigger.get("ofcEndDate", ""),
        "consularCities": consularCities,
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
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
        result = await page.evaluate("window.__sniperResult")
        if result is not None:
            status = result.get("status", "unknown")
            msg = result.get("msg", "No message")

            await page.evaluate("window.__sniperResult = null;")

            if status == "success":
                log.info(f"✅ Extension reports SUCCESS: {msg}")
                return True
            else:
                log.error(f"❌ Extension reports FAILURE: {msg}")
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
