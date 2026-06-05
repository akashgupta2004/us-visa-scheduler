import logging
import asyncio
from playwright.async_api import Page

PORTAL_HOST = "www.usvisascheduling.com"
PORTAL_URL  = "https://www.usvisascheduling.com/en-US/"


async def ensure_on_portal(page: Page, log: logging.Logger, timeout_seconds: int = 120) -> bool:
    """Wait until the browser naturally lands on usvisascheduling.com.

    After login_runner completes, the Azure B2C OAuth redirect chain finishes
    automatically and the browser arrives on the portal on its own.
    We just need to wait for it — no manual navigation needed.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            if page.url.startswith("https://www.usvisascheduling.com"):
                log.info(f"On portal: {page.url}")
                return True
        except Exception:
            pass
        await asyncio.sleep(1)

    # Fallback: still not there, try a direct navigation
    log.info("Redirect didn't complete naturally — navigating directly …")
    try:
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
        # Wait again for any further redirect to settle
        deadline2 = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline2:
            try:
                if page.url.startswith("https://www.usvisascheduling.com"):
                    log.info(f"On portal: {page.url}")
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
    except Exception as e:
        log.error(f"Failed to navigate to portal: {e}")

    log.error(f"Timed out waiting for portal. Last URL: {page.url}")
    return False
