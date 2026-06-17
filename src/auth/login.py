import time
import asyncio
import logging
from playwright.async_api import Page
from src.auth.utils import human_delay, human_type, human_click
from src.auth.captcha import solve_captcha_on_page

async def wait_for_waiting_room(page: Page, log: logging.Logger, timeout_minutes: int = 60) -> None:
    deadline = time.time() + timeout_minutes * 60
    log.info("Checking for Cloudflare Waiting Room …")

    while time.time() < deadline:
        try:
            html = await page.content()
            title = await page.title()
        except Exception:
            # Page is mid-navigation — wait and retry
            await asyncio.sleep(2)
            continue
            
        html_lower = html.lower()
        if any(kw in html_lower for kw in ["schedule appointment", "reschedule appointment", "cancel appointment"]):
            log.info("Dashboard keywords detected (e.g. Schedule Appointment). We are already logged in!")
            return
        
        title_lower = title.lower()
        
        # Check title for common waiting room/challenge indicators
        in_queue = any(kw in title_lower for kw in [
            "moment", "waiting room", "queue", "verify you are human", "attention required"
        ])
        
        # If title doesn't match, check for specific visible Cloudflare elements
        if not in_queue:
            cf_elements = [
                "#cf-please-wait", 
                "[data-translate='challenge_headline']",
                ".queue-position",
                "#queuePosition",
                "#waitTime"
            ]
            for sel in cf_elements:
                if await page.locator(sel).is_visible():
                    in_queue = True
                    break

        if not in_queue:
            log.info(f"Waiting room cleared — URL: {page.url}")
            return
            
        # Attempt to click Cloudflare challenge if present
        try:
            cf_iframe = page.frame_locator("iframe[src*='challenge']")
            cf_input = page.locator('input[name="cf-turnstile-response"]')
            
            if await cf_iframe.locator("input[type='checkbox']").count() > 0:
                await cf_iframe.locator("input[type='checkbox']").first.click(timeout=1000)
                log.info("Clicked Cloudflare challenge checkbox.")
            elif await cf_iframe.locator(".ctp-checkbox-label").count() > 0:
                await cf_iframe.locator(".ctp-checkbox-label").first.click(timeout=1000)
                log.info("Clicked Cloudflare Turnstile label.")
            elif await page.locator("iframe[src*='challenge']").count() > 0:
                await page.locator("iframe[src*='challenge']").first.click(timeout=1000)
                log.info("Clicked Cloudflare challenge iframe directly.")
            elif await cf_input.count() > 0:
                # Often hidden behind closed shadow DOM, click the wrapper div
                await cf_input.first.locator("..").click(position={"x": 30, "y": 30}, timeout=1000)
                log.info("Clicked Cloudflare Turnstile widget wrapper.")
        except Exception:
            pass

        try:
            loc = page.locator("[data-queue-position], .queue-position, #queuePosition, #waitTime")
            pos = (await loc.first.inner_text()).strip() if await loc.count() > 0 else "unknown"
            if pos != "unknown":
                log.info(f"In waiting room — {pos}")
            else:
                log.debug("In waiting room / Cloudflare challenge — waiting for auto-redirect …")
        except Exception:
            log.debug("In waiting room / Cloudflare challenge — waiting for auto-redirect …")

        await asyncio.sleep(5)

    raise TimeoutError("Timed out waiting for waiting room to clear.")


async def login(page: Page, username: str, password: str, api_key: str, log: logging.Logger) -> bool:
    log.info("Starting login …")

    u_selectors = [
        "#signInName", "#email", "input[name='signInName']",
        "input[name='UserName']", "input[name='username']",
        "input[name='email']",    "input[type='email']",
        "input[id='UserName']",   "input[id='username']",
        "input[placeholder*='username' i]", "input[placeholder*='email' i]",
        "input[type='text']",
    ]
    p_selectors = [
        "#password", "input[name='Password']", "input[name='password']",
        "input[type='password']", "input[id='Password']",
        "input[placeholder*='password' i]",
    ]

    u_combined = ", ".join(u_selectors)
    p_combined = ", ".join(p_selectors)

    try:
        await page.wait_for_selector(u_combined, state="visible", timeout=30000)
    except Exception:
        log.error("Username field did not appear within 30s.")
        return False

    u_field = None
    for s in u_selectors:
        if await page.locator(s).count() > 0:
            u_field = s
            break

    p_field = None
    for s in p_selectors:
        if await page.locator(s).count() > 0:
            p_field = s
            break

    if not u_field:
        log.error("Username field not found.")
        return False
    if not p_field:
        log.error("Password field not found.")
        return False

    await human_type(page, u_field, username)
    log.debug(f"Typed username: {username}")
    await human_type(page, p_field, password)
    log.debug("Typed password.")

    if not await solve_captcha_on_page(page, api_key, log):
        log.error("CAPTCHA failed.")
        return False

    await human_delay(300, 600)

    signin_selectors = [
        "input[type='submit'][value*='Sign' i]",
        "input[type='submit'][value*='Log' i]",
        "button[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Login')",
        "input[value='Login']",
        "#loginButton", ".login-btn",
    ]

    clicked = False
    for sel in signin_selectors:
        if await page.locator(sel).count() > 0:
            await human_click(page, sel)
            log.info(f"Clicked Sign In: {sel}")
            clicked = True
            break

    if not clicked:
        log.error("Sign In button not found.")
        return False

    log.info("Waiting for login redirect ...")
    try:
        for _ in range(45):
            u = page.url.lower()
            if "selfasserted" in u or not any(k in u for k in ["b2clogin", "login", "logon", "signin", "sign-in"]):
                break
            await asyncio.sleep(1)
    except Exception:
        pass

    try:
        url  = page.url
        html = await page.inner_text("body")  # MUST use inner_text
    except Exception as e:
        if page.is_closed() or "closed" in str(e).lower():
            log.error("Page was unexpectedly closed!")
            return False
        log.info(f"Page navigating, couldn't read content ({e}) — assuming successful redirect.")
        return True

    if any(p in html.lower() for p in [
        "invalid credentials", "incorrect password", "login failed",
        "the user name or password", "invalid username",
        "character", "match the image", "try again", "does not match", "captcha failed", "image captcha"
    ]):
        log.error("Login error / invalid CAPTCHA message on page. Aborting attempt and forcing refresh...")
        return False

    url_lower = url.lower()
    if "selfasserted" not in url_lower and any(k in url_lower for k in ["logon", "login", "signin", "sign-in", "b2clogin"]):
        log.error("Login still failed after initial submit. Aborting attempt.")
        return False

    try:
        log.info(f"Login successful — URL: {page.url}")
    except Exception:
        log.info("Login successful — navigating ...")
    return True
