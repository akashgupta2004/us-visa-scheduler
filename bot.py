"""
=============================================================
  Visa Scheduling Bot — usvisascheduling.com/en-US/
  ─────────────────────────────────────────────────
  HOW TO USE:
  1. Close ALL Chrome windows
  2. Run this in a terminal FIRST:
       "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
  3. In the Chrome window that opens, go to ANY page (e.g. google.com)
  4. Then run:  python bot.py

  The bot connects to YOUR real Chrome (bypasses Cloudflare completely).
=============================================================
"""

import asyncio
import base64
import json
import os
import random
import sys
import time
import logging
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

# ── playwright-stealth ───────────────────────────────────────
try:
    from playwright_stealth import Stealth as _Stealth
    async def _apply_stealth(page: Page):
        await _Stealth().apply_stealth_async(page)
except ImportError:
    try:
        from playwright_stealth import stealth_async as _sa
        async def _apply_stealth(page: Page):
            await _sa(page)
    except ImportError:
        async def _apply_stealth(page: Page):
            pass

# ── FastCaptcha ──────────────────────────────────────────────
try:
    from fastcaptcha import FastCaptcha, FastCaptchaException
    _FC_AVAILABLE = True
except ImportError:
    _FC_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
load_dotenv()

FASTCAPTCHA_API_KEY = os.getenv("FASTCAPTCHA_API_KEY", "")
VISA_USERNAME       = os.getenv("VISA_USERNAME", "")
VISA_PASSWORD       = os.getenv("VISA_PASSWORD", "")

BASE_URL            = "https://www.usvisascheduling.com/en-US/"
LOGIN_URL           = "https://www.usvisascheduling.com/en-US/Account/LogOn"
SECURITY_Q_FILE     = Path(__file__).parent / "security_questions.json"
CHROME_EXE          = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT            = 9222

# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("visa-bot")

# ─────────────────────────────────────────────────────────────
# Human-like helpers
# ─────────────────────────────────────────────────────────────

async def human_delay(min_ms: int = 60, max_ms: int = 180) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def human_type(page: Page, selector: str, text: str) -> None:
    loc = page.locator(selector).first
    await loc.click()
    await loc.fill("")  # Ensure the field is cleared to avoid double typing
    await human_delay(100, 300)
    for char in text:
        await loc.type(char, delay=random.uniform(50, 150))
    await human_delay(80, 200)


async def human_click(page: Page, selector: str) -> None:
    element = page.locator(selector).first
    box = await element.bounding_box()
    if box:
        x = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y)
        await human_delay(50, 150)
        await page.mouse.click(x, y)
    else:
        await element.click()
    await human_delay(100, 250)


# ─────────────────────────────────────────────────────────────
# Launch Chrome with remote debugging (if not already running)
# ─────────────────────────────────────────────────────────────

def ensure_chrome_debug_running() -> None:
    """
    Start Chrome with remote debugging on port 9222 if it isn't already.
    Waits for the debug port to become available.
    """
    import socket, time

    def port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    if port_open(CDP_PORT):
        log.info(f"Chrome debug port {CDP_PORT} already active — connecting.")
        return

    log.info(f"Starting Chrome with --remote-debugging-port={CDP_PORT} …")

    profile_dir = Path(__file__).parent / "chrome_profile"
    profile_dir.mkdir(exist_ok=True)

    chrome_exe = CHROME_EXE
    if not os.path.isfile(chrome_exe):
        # Try LOCALAPPDATA path
        alt = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
        if os.path.isfile(alt):
            chrome_exe = alt
        else:
            log.error(
                "Chrome not found. Please start Chrome manually with:\n"
                f'  "{CHROME_EXE}" --remote-debugging-port={CDP_PORT}\n'
                "Then run this bot again."
            )
            sys.exit(1)

    subprocess.Popen([
        chrome_exe,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ])

    # Wait up to 15 s for port to open
    for i in range(30):
        time.sleep(0.5)
        if port_open(CDP_PORT):
            log.info("Chrome debug port ready.")
            return

    log.error(f"Chrome debug port {CDP_PORT} did not open in time.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# Connect to running Chrome via CDP
# ─────────────────────────────────────────────────────────────

async def connect_to_chrome(playwright) -> tuple[BrowserContext, Page]:
    """Connect Playwright to a running Chrome via CDP."""
    log.info(f"Connecting to Chrome on ws://127.0.0.1:{CDP_PORT} …")

    browser = await playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{CDP_PORT}"
    )

    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Use existing page or open new one
    if context.pages:
        page = context.pages[-1]
    else:
        page = await context.new_page()

    log.info(f"Connected — current page: {page.url}")
    return browser, context, page


# ─────────────────────────────────────────────────────────────
# Waiting room
# ─────────────────────────────────────────────────────────────

async def wait_for_waiting_room(page: Page, timeout_minutes: int = 60) -> None:
    deadline = time.time() + timeout_minutes * 60
    log.info("Checking for Cloudflare Waiting Room …")

    while time.time() < deadline:
        html = await page.content()
        in_queue = any(kw in html.lower() for kw in [
            "waiting room", "you are in the queue", "queue position",
            "estimated wait", "waitingroom", "cfwaitingroom", "waiting-room",
        ])

        if not in_queue:
            log.info(f"Waiting room cleared — URL: {page.url}")
            return

        try:
            loc = page.locator("[data-queue-position], .queue-position, #queuePosition")
            pos = (await loc.first.inner_text()).strip() if await loc.count() > 0 else "unknown"
            log.info(f"In waiting room — position: {pos}")
        except Exception:
            log.info("In waiting room — waiting for auto-redirect …")

        await asyncio.sleep(15)

    raise TimeoutError("Timed out waiting for waiting room to clear.")


# ─────────────────────────────────────────────────────────────
# CAPTCHA
# ─────────────────────────────────────────────────────────────

def _fastcaptcha_solve(image_bytes: bytes) -> str:
    if not _FC_AVAILABLE:
        raise RuntimeError("fastcaptcha-api not installed.")
    if not FASTCAPTCHA_API_KEY:
        raise RuntimeError("FASTCAPTCHA_API_KEY missing in .env")
    client = FastCaptcha(api_key=FASTCAPTCHA_API_KEY)
    b64    = base64.b64encode(image_bytes).decode("utf-8")
    text   = client.solve_base64(b64)
    log.info(f"FastCaptcha → '{text}'")
    return text


async def solve_captcha_on_page(page: Page) -> bool:
    captcha_selectors = [
        "img[src*='captcha' i]", "img[src*='Captcha' i]",
        "img[src*='VerifyImage' i]", "img[src*='CaptchaImage' i]",
        "img[alt*='captcha' i]",  "img[class*='captcha' i]",
        "img[id*='captcha' i]",   ".captcha img", "#captcha img",
    ]

    captcha_loc = None
    for sel in captcha_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            captcha_loc = loc.first
            log.info(f"CAPTCHA image detected: {sel}")
            break

    if not captcha_loc:
        log.info("No image CAPTCHA — skipping.")
        return True

    try:
        img_bytes = await captcha_loc.screenshot()
    except Exception as e:
        log.warning(f"CAPTCHA screenshot failed: {e}")
        return False

    try:
        solution = _fastcaptcha_solve(img_bytes)
    except Exception as e:
        log.error(f"FastCaptcha error: {e}")
        return False

    if not solution:
        log.error("Empty CAPTCHA solution.")
        return False

    input_selectors = [
        "#CaptchaInputText", "#captchaText",
        "input[name*='captcha' i]", "input[id*='captcha' i]",
        "input[class*='captcha' i]", "input[placeholder*='captcha' i]",
        "input[placeholder*='code' i]",
    ]

    for sel in input_selectors:
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.fill("")
            await human_delay(100, 200)
            await human_type(page, sel, solution)
            log.info(f"CAPTCHA typed: '{solution}'")
            return True

    log.error("CAPTCHA input not found.")
    return False


# ─────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────

async def login(page: Page, username: str, password: str) -> bool:
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
    log.info(f"Typed username: {username}")
    await human_type(page, p_field, password)
    log.info("Typed password.")

    if not await solve_captcha_on_page(page):
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
        # Playwright Python's wait_for_url does not support lambda predicates.
        # We manually poll for the URL to change away from the B2C login state.
        for _ in range(45):
            u = page.url.lower()
            if "selfasserted" in u or not any(k in u for k in ["b2clogin", "login", "logon", "signin", "sign-in"]):
                break
            await asyncio.sleep(1)
    except Exception:
        pass

    try:
        url  = page.url
        html = await page.inner_text("body")  # MUST use inner_text to avoid reading hidden DOM elements!
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

    log.info(f"Login successful — URL: {page.url}")
    return True


# ─────────────────────────────────────────────────────────────
# Security question
# ─────────────────────────────────────────────────────────────

def load_security_answers() -> dict:
    if not SECURITY_Q_FILE.exists():
        return {}
    with open(SECURITY_Q_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.pop("_comment", None)
    return data


def match_answer(question_text: str, answers: dict) -> str | None:
    q = question_text.lower()
    return next((v for k, v in answers.items() if k.lower() in q), None)


async def handle_security_question(page: Page) -> bool:
    await human_delay(1000, 2000)

    q_selectors = [
        "label[for*='SecurityAnswer' i]", "label[for*='security' i]",
        ".security-question", ".question-text", "#securityQuestion", "legend",
        "p:has-text('favourite')", "p:has-text('favorite')",
        "p:has-text('maiden')",    "p:has-text('born')",
        "p:has-text('pet')",       "p:has-text('school')",
        "span:has-text('favourite')", "div.question",
    ]

    log.info("Waiting for security question to appear (up to 15s) …")
    try:
        combined_q = ", ".join(q_selectors)
        await page.wait_for_selector(combined_q, state="visible", timeout=15000)
    except Exception:
        pass

    answers = load_security_answers()
    filled_any = False
    
    try:
        log.info("Waiting for security question inputs (up to 15s) …")
        await page.wait_for_selector("input:not([type='hidden']):not([type='submit']):not([type='button']):not([readonly]):not([disabled])", state="visible", timeout=15000)
    except Exception:
        pass
        
    try:
        # Find all inputs that are editable (not hidden, disabled, or readonly) and are not buttons/checkboxes
        inputs = page.locator("input:not([type='hidden']):not([type='submit']):not([type='button']):not([readonly]):not([disabled])")
        count = await inputs.count()
        
        # Pull the entire text of the document in visual order
        body_text = await page.inner_text("body")
        questions = []
        
        for line in body_text.split('\n'):
            line = line.strip()
            if len(line) < 5: continue
            
            is_question = "?" in line and any(k in line.lower() for k in [
                "favourite", "favorite", "born", "pet", "school", "maiden", "childhood", "car", "hero", "food"
            ])
            
            if is_question and line not in questions:
                questions.append(line)
        
        for i in range(count):
            input_loc = inputs.nth(i)
            if not await input_loc.is_visible():
                continue
                
            if i < len(questions):
                textToMatch = questions[i]
                answer = match_answer(textToMatch, answers)
                if not answer:
                    log.error(f"No answer found mapped for question: '{textToMatch}'")
                    continue
                    
                await input_loc.fill("")
                await human_delay(100, 200)
                await input_loc.type(answer, delay=random.randint(50, 150))
                log.info(f"Typed answer for: '{textToMatch}'")
                filled_any = True

    except Exception as e:
        log.error(f"Error handling security inputs: {e}")

    if not filled_any:
        log.info("No security questions filled. Either none present or couldn't parse.")
        return True

    await human_delay(300, 600)

    for sel in ["button[type='submit']", "input[type='submit']",
                "button:has-text('Continue')", "button:has-text('Submit')",
                "input[value='Continue']"]:
        if await page.locator(sel).count() > 0:
            await human_click(page, sel)
            log.info("Security question submitted.")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            return True

    log.error("Submit button not found.")
    return False


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

async def run() -> None:
    missing = [k for k, v in {
        "VISA_USERNAME": VISA_USERNAME,
        "VISA_PASSWORD": VISA_PASSWORD,
        "FASTCAPTCHA_API_KEY": FASTCAPTCHA_API_KEY,
    }.items() if not v]
    if missing:
        log.error(f"Missing .env vars: {', '.join(missing)}")
        sys.exit(1)

    # Start Chrome in debug mode (or connect to existing)
    ensure_chrome_debug_running()
    await asyncio.sleep(2)  # give Chrome a moment to initialise

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw)

        try:
            # ── 1. & 2. Open site & wait ──────────────
            cur_url = page.url.lower()
            if any(k in cur_url for k in ["logon", "login", "signin", "b2clogin"]):
                log.info("Already on login page — skipping initial navigation.")
            else:
                if "usvisascheduling.com" not in cur_url:
                    log.info(f"→ Navigating to {BASE_URL}")
                    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
                    await human_delay(2000, 4000)

                await wait_for_waiting_room(page, timeout_minutes=60)
                await human_delay(1000, 2000)

                # ── 3. Wait for login page ──────────────
                log.info("Waiting for automatic redirect to login page (up to 5 minutes) …")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if any(k in page.url.lower() for k in ["logon", "login", "signin"]):
                        log.info("Arrived at login page.")
                        break
                    await asyncio.sleep(5)
                else:
                    log.warning("Did not auto-redirect in 5 minutes. Trying manual navigation …")
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
                    await wait_for_waiting_room(page, timeout_minutes=30)
                
            await human_delay(1000, 2000)

            # ── 4. Login ──────────────────────────────
            success = False
            for attempt in range(1, 4):
                log.info(f"Login attempt {attempt}/3")
                success = await login(page, VISA_USERNAME, VISA_PASSWORD)
                if success:
                    break
                if attempt < 3:
                    log.info("Retrying in 5 s …")
                    await asyncio.sleep(5)
                    await page.reload(wait_until="domcontentloaded")
                    await human_delay(1500, 3000)

            if not success:
                log.error("All login attempts failed.")
                await page.screenshot(path="login_failed.png")
                return

            # ── 5. Security question ──────────────────
            await human_delay(1500, 3000)
            if not await handle_security_question(page):
                log.error("Security question failed.")
                await page.screenshot(path="security_question_failed.png")
                return

            # ── Done ──────────────────────────────────
            log.info("=" * 60)
            log.info("✅  All steps complete!")
            log.info(f"   URL: {page.url}")
            log.info("=" * 60)
            log.info("Browser staying open — press Ctrl+C to exit.")
            await asyncio.Event().wait()

        except KeyboardInterrupt:
            log.info("Stopped by user.")
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            try:
                await page.screenshot(path="error_screenshot.png")
                log.info("Screenshot → error_screenshot.png")
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run())
