import time
import json
import logging
import asyncio
import sys
from pathlib import Path
from playwright.async_api import Page

# Ensure project root is on the path for top-level imports (slack.py)
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from slack import send as send_slack
from src.common.state import read_state as _read_state



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

# City name mapping: internal canonical form → extension/portal dropdown form.
CITY_NORMALIZE = {
    "DELHI": "NEW DELHI",
}

def normalize_city(city: str) -> str:
    """Map city names from internal canonical form to extension/portal form."""
    upper = city.strip().upper()
    return CITY_NORMALIZE.get(upper, upper)


async def trigger_extension_booking(
    page: Page,
    trigger: dict,
    log: logging.Logger,
    on_message_sent=None,
) -> tuple[bool, dict]:
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
        "action_type": trigger.get("action_type", "SNIPER"),
        "isReschedule": trigger.get("action_type") == "RESCHEDULE_FULL",
        "ofcCities": ofcCities,
        "ofcPriorityCity": normalize_city(trigger.get("ofcPriorityCity", ofcCities[0] if ofcCities else "")),
        "ofcPriorityDate": trigger.get("ofcPriorityDate", ""),
        "ofcStartDate": trigger.get("ofcStartDate", ""),
        "ofcEndDate": trigger.get("ofcEndDate", ""),
        "consularCities": consularCities,
        "consularPriorityCity": normalize_city(trigger.get("consularPriorityCity", consularCities[0] if consularCities else "")),
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
        "preventImmediateBooking": trigger.get("prevent_immediate", False),
        "multiPerson": trigger.get("multiPerson", False),
                "scoutOfcToken": trigger.get(
            "scoutOfcToken", ""
        ),
        "scoutOfcAppd": trigger.get(
            "scoutOfcAppd", ""
        ),
        "scoutOfcTokenCity": normalize_city(
            trigger.get(
                "scoutOfcTokenCity",
                "",
            )
        ),
        "scoutOfcTokenDate": trigger.get(
            "scoutOfcTokenDate", ""
        ),
        "scoutOfcTokenIsReschedule": trigger.get(
            "scoutOfcTokenIsReschedule",
            False,
        ),
        "scoutOfcTokenCapturedAt": trigger.get(
            "scoutOfcTokenCapturedAt",
            0,
        ),
    }

    log.info(f"🚀 Triggering extension booking for '{customerName}'")
    log.info(f"   OFC: {', '.join(ofcCities)} between {config['ofcStartDate']} and {config['ofcEndDate']}")
    log.info(f"   Consular: {', '.join(consularCities)} between {config['consularStartDate']} and {config['consularEndDate']}")
    log_config = dict(config)

    if log_config.get("scoutOfcToken"):
        log_config["scoutOfcToken"] = "<preserved>"

    log.info(f"   Config: {json.dumps(log_config)}")

    await page.evaluate("""
        window.__sniperResult = null;
        window.__sniperResultListener = function(event) {
            if (event.source !== window) return;
            if (event.data && event.data.action === 'SNIPER_RESULT') {
                window.__sniperResult = event.data;
                window.removeEventListener('message', window.__sniperResultListener);
            }
        };
        window.addEventListener('message', window.__sniperResultListener);
    """)

    await page.evaluate("""(config) => {
        window.postMessage({
            action: 'EXECUTE_SNIPER',
            config: config
        }, '*');
    }""", config)

    log.info("📨 Message sent to extension. Waiting for result (up to 300s) …")

    # Optional hook used only by PRE-CVS detector-first booking.
    # At this point the detector's EXECUTE_SNIPER message has already
    # been posted to the page/extension, so the rest of the fleet may
    # safely be released without getting ahead of the detector.
    if on_message_sent is not None:
        try:
            await on_message_sent()
        except Exception as callback_error:
            # Never allow fleet-broadcast bookkeeping to interfere
            # with the detector's own booking attempt.
            log.warning(
                f"[SCOUT] Could not release detector-first fleet "
                f"broadcast: {callback_error}"
            )

    deadline = time.time() + 300
    while time.time() < deadline:
        if await check_for_page_limit(page, customerName, log):
            return False, {}

        try:
            result = await page.evaluate("window.__sniperResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                log.info("Navigation detected while waiting for extension...")
                for _ in range(5):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in page.url:
                        log.info("✅ Booking SUCCESS (verified via URL navigation)")
                        return True, {}
                log.warning(f"Navigated to unexpected URL: {page.url}")
                return False, {}
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
                return True, {"waitingForConsular": result.get("waitingForConsular", False), "bookedOfcDate": result.get("bookedOfcDate")}
            else:
                context = {"waitingForConsular": result.get("waitingForConsular", False), "bookedOfcDate": result.get("bookedOfcDate")}
                if context.get("waitingForConsular"):
                    log.warning(f"⚠️ Booking PARTIAL: {msg}")
                    return False, context
                log.error(f"❌ Booking FAILURE: {msg}")
                if "429" in msg:
                    raise Exception("429 Too Many Requests")
                if "Session expired" in msg:
                    raise Exception("Session expired")
                return False, {}

        await asyncio.sleep(1)

    log.error("⏱️ Timed out waiting for extension response (300s).")
    await page.evaluate("""
        if (window.__sniperResultListener) {
            window.removeEventListener('message', window.__sniperResultListener);
        }
        window.__sniperResult = null;
    """)
    return False, {}


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
        "isReschedule": True,
        "consularCities": consularCities,
        "consularPriorityCity": normalize_city(trigger.get("consularPriorityCity", consularCities[0] if consularCities else "")),
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
        "preventImmediateBooking": trigger.get("prevent_immediate", False),
        "multiPerson": trigger.get("multiPerson", False),
    }

    log.info(f"📅 Triggering consular reschedule for '{customerName}'")
    log.info(f"   Cities: {', '.join(consularCities)} between {config['consularStartDate']} and {config['consularEndDate']}")
    log.info(f"   Config: {json.dumps(config)}")

    await page.evaluate("""
        window.__rescheduleResult = null;
        window.__rescheduleResultListener = function(event) {
            if (event.source !== window) return;
            if (event.data && event.data.action === 'CONSULAR_STANDALONE_RESULT') {
                window.__rescheduleResult = event.data;
                window.removeEventListener('message', window.__rescheduleResultListener);
            }
        };
        window.addEventListener('message', window.__rescheduleResultListener);
    """)

    await page.evaluate("""(config) => {
        window.postMessage({
            action: 'EXECUTE_CONSULAR_STANDALONE',
            config: config
        }, '*');
    }""", config)

    log.info("📨 Reschedule message sent to extension. Waiting for result (up to 300s) …")

    deadline = time.time() + 300
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

    log.error("⏱️ Timed out waiting for reschedule result (300s).")
    await page.evaluate("""
        if (window.__rescheduleResultListener) {
            window.removeEventListener('message', window.__rescheduleResultListener);
        }
        window.__rescheduleResult = null;
    """)
    return False


async def trigger_extension_sniper_consular_only(
    page: Page,
    trigger: dict,
    bookedOfcDate: str,
    log: logging.Logger,
    state_file: Path | None = None,
) -> tuple[bool, dict]:
    """
    Send a window.postMessage to the extension's content script
    with the SNIPER_CONSULAR_ONLY configuration.
    Expects SNIPER_CONSULAR_ONLY_RESULT back from the content script.
    """
    consularCities = trigger.get("consularCities")
    if not consularCities:
        log.error("❌ No consularCities in trigger — cannot book consular only.")
        return False, {}
    consularCities = [normalize_city(c) for c in consularCities]

    customerName = trigger.get("customer_name", "unknown")

    config = {
        "isReschedule": trigger.get("action_type") == "RESCHEDULE_FULL_CONSULAR_ONLY",
        "consularCities": consularCities,
        "consularPriorityCity": normalize_city(trigger.get("consularPriorityCity", consularCities[0] if consularCities else "")),
        "consularStartDate": trigger.get("consularStartDate", ""),
        "consularEndDate": trigger.get("consularEndDate", ""),
        "preventImmediateBooking": trigger.get("prevent_immediate", False),
        "multiPerson": trigger.get("multiPerson", False),
    }

    log.info(f"🎯 Triggering Consular-Only Sniper for '{customerName}'")
    log.info(f"   Cities: {', '.join(consularCities)} between {config['consularStartDate']} and {config['consularEndDate']}")
    log.info(f"   bookedOfcDate: {bookedOfcDate}")

    last_priority_version = 0

    if state_file:
        initial_state = _read_state(state_file)

        try:
            last_priority_version = int(
                initial_state.get(
                    "consularPriorityVersion",
                    0,
                )
                or 0
            )
        except (TypeError, ValueError):
            last_priority_version = 0

    await page.evaluate("""
        window.__consularOnlyResult = null;
        window.__consularOnlyResultListener = function(event) {
            if (event.source !== window) return;
            if (event.data && event.data.action === 'SNIPER_CONSULAR_ONLY_RESULT') {
                window.__consularOnlyResult = event.data;
                window.removeEventListener('message', window.__consularOnlyResultListener);
            }
        };
        window.addEventListener('message', window.__consularOnlyResultListener);
    """)

    await page.evaluate("""(data) => {
        window.postMessage({
            action: 'EXECUTE_SNIPER_CONSULAR_ONLY',
            config: data.config,
            bookedOfcDate: data.bookedOfcDate
        }, '*');
    }""", {"config": config, "bookedOfcDate": bookedOfcDate})

    log.info("📨 Consular-Only message sent to extension. Waiting for result (up to 600s) …")

    deadline = time.time() + 600
    while time.time() < deadline:
        if await check_for_page_limit(page, customerName, log):
            return False, {}

        # Check whether a newer CVS Consular alert arrived while
        # the extension is already scanning other cities.
        if state_file:
            latest_state = _read_state(state_file)

            try:
                latest_priority_version = int(
                    latest_state.get(
                        "consularPriorityVersion",
                        0,
                    )
                    or 0
                )
            except (TypeError, ValueError):
                latest_priority_version = 0

            if latest_priority_version > last_priority_version:
                latest_priority_city = normalize_city(
                    str(
                        latest_state.get("targetConsularCity")
                        or latest_state.get("consularPriorityCity")
                        or ""
                    )
                )

                latest_priority_date = str(
                    latest_state.get("targetConsularDate")
                    or ""
                )

                latest_detected_at = latest_state.get(
                    "targetConsularDetectedAt"
                )

                if latest_priority_city:
                    try:
                        await page.evaluate(
                            """(priority) => {
                                window.postMessage({
                                    action: 'CONSULAR_PRIORITY_UPDATE',
                                    city: priority.city,
                                    date: priority.date,
                                    detectedAt: priority.detectedAt,
                                    version: priority.version
                                }, '*');
                            }""",
                            {
                                "city": latest_priority_city,
                                "date": latest_priority_date,
                                "detectedAt": latest_detected_at,
                                "version": latest_priority_version,
                            },
                        )

                        log.info(
                            f"⚡ Forwarded newer CVS Consular priority "
                            f"to active scan: {latest_priority_city} "
                            f"{latest_priority_date}"
                        )

                    except Exception as priority_error:
                        log.warning(
                            f"Could not forward new Consular priority "
                            f"to extension: {priority_error}"
                        )

                last_priority_version = latest_priority_version

        try:
            result = await page.evaluate("window.__consularOnlyResult")
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                log.info("Navigation detected while waiting for extension...")
                for _ in range(5):
                    await asyncio.sleep(1)
                    if "appointment-confirmation" in page.url:
                        log.info("✅ Consular-Only SUCCESS (verified via URL navigation)")
                        return True, {}
                log.warning(f"Navigated to unexpected URL: {page.url}")
                return False, {}
            raise e

        if result is not None:
            status = result.get("status", "unknown")
            msg = result.get("msg", "No message")

            try:
                await page.evaluate("window.__consularOnlyResult = null;")
            except Exception:
                pass

            if status == "success":
                log.info(f"✅ Consular-Only SUCCESS: {msg}")
                return True, {"waitingForConsular": result.get("waitingForConsular", False), "bookedOfcDate": result.get("bookedOfcDate")}
            else:
                context = {"waitingForConsular": result.get("waitingForConsular", False), "bookedOfcDate": result.get("bookedOfcDate")}
                if context.get("waitingForConsular"):
                    log.warning(f"⚠️ Consular-Only STILL WAITING: {msg}")
                    return False, context
                log.error(f"❌ Consular-Only FAILURE: {msg}")
                if "429" in msg:
                    raise Exception("429 Too Many Requests")
                if "Session expired" in msg:
                    raise Exception("Session expired")
                return False, {}

        await asyncio.sleep(1)

    log.error("⏱️ Timed out waiting for Consular-Only result (600s).")
    await page.evaluate("""
        if (window.__consularOnlyResultListener) {
            window.removeEventListener('message', window.__consularOnlyResultListener);
        }
        window.__consularOnlyResult = null;
    """)
    return False, {}
