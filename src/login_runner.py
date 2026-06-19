"""
=============================================================
  Visa Scheduling Bot — usvisascheduling.com/en-US/
  ─────────────────────────────────────────────────
  Entrypoint runner for spawning Chrome and logging in.
=============================================================
"""

import argparse
import asyncio
import os
import sys
import time
import logging
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Import modularized components
from src.auth.browser import ensure_chrome_debug_running, connect_to_chrome
from src.auth.login import wait_for_waiting_room, login
from src.auth.security import handle_security_question
from src.auth.utils import human_delay

sys.path.insert(0, str(Path(__file__).parent.parent))
from slack import send_slack_error

load_dotenv()

FASTCAPTCHA_API_KEY = os.getenv("FASTCAPTCHA_API_KEY", "")

BASE_URL  = "https://www.usvisascheduling.com/en-US/"
LOGIN_URL = "https://www.usvisascheduling.com/en-US/Account/LogOn"

_ARGS: argparse.Namespace | None = None

def _is_login_url(url: str) -> bool:
    return any(k in url for k in ["logon", "login", "signin", "b2clogin"])

def _is_portal_url(url: str) -> bool:
    return (
        "usvisascheduling.com" in url
        and not _is_login_url(url)
        and any(k in url for k in ["/schedule", "dashboard", "applicant_details", "/en-us/"])
    )

def safe_id(username: str) -> str:
    """Generate a filesystem-safe unique identifier from a username/email."""
    return re.sub(r'[^a-zA-Z0-9]', '_', str(username))

def _get_state_file(args) -> Path:
    uid = safe_id(args.username)
    return Path(__file__).parent / f"state_{uid}.json"

def _get_args() -> argparse.Namespace:
    """Parse CLI arguments once, caching the result."""
    global _ARGS
    if _ARGS is not None:
        return _ARGS
    parser = argparse.ArgumentParser(description="Visa Scheduling Bot - Login Runner")
    parser.add_argument("--username",    default="",
                        help="Username for the portal")
    parser.add_argument("--password",    default="",
                        help="Visa portal password (default: VISA_PASSWORD env var)")
    parser.add_argument("--cdp-port",   type=int, default=9222,
                        help="Chrome remote-debugging port (default: 9222)")
    parser.add_argument("--customer",    default="",
                        help="Customer label used in log lines (default: username)")
    parser.add_argument("--profile-dir", default="",
                        help="Chrome user-data-dir path (default: chrome_profile_<customer>)")
    _ARGS = parser.parse_args()
    if not _ARGS.customer:
        _ARGS.customer = _ARGS.username
    if not _ARGS.profile_dir:
        _ARGS.profile_dir = str(Path(__file__).parent.parent / f"chrome_profile_{safe_id(_ARGS.username)}")
    return _ARGS

def _make_logger(customer: str) -> logging.Logger:
    name = f"visa-bot[{customer}]"
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s [{customer}] [%(levelname)s] %(message)s"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger

log = logging.getLogger("visa-bot")

async def run() -> None:
    global log
    args = _get_args()

    # Set up a customer-tagged logger for this session
    log = _make_logger(args.customer)

    missing = []
    if not args.username:
        missing.append("--username / VISA_USERNAME")
    if not args.password:
        missing.append("--password / VISA_PASSWORD")

    if missing:
        log.error(f"Missing required values: {', '.join(missing)}")
        sys.exit(1)

    if not FASTCAPTCHA_API_KEY:
        log.warning("FASTCAPTCHA_API_KEY missing; existing sessions can still be reused, but fresh CAPTCHA login will fail.")

    # Start Chrome in debug mode (or connect to existing)
    ensure_chrome_debug_running(args.cdp_port, args.profile_dir, log)
    await asyncio.sleep(2)  # give Chrome a moment to initialise

    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, args.cdp_port, log)

        disconnect_event = asyncio.Event()
        browser.on("disconnected", lambda _: disconnect_event.set())

        try:
            # ── 1. & 2. Open site & wait ──────────────
            already_logged_in = False
            cur_url = page.url.lower()
            if _is_portal_url(cur_url):
                log.info("Existing authenticated portal session detected.")
                already_logged_in = True
            elif _is_login_url(cur_url):
                log.info("Already on login page — skipping initial navigation.")
            else:
                if "usvisascheduling.com" not in cur_url:
                    log.info(f"→ Navigating to {BASE_URL}")
                    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
                    await human_delay(2000, 4000)

                await wait_for_waiting_room(page, log, timeout_minutes=480)
                await human_delay(1000, 2000)

                # ── 3. Wait for login page or Dashboard ──────────────
                log.info("Waiting for automatic redirect (up to 5 minutes) …")
                deadline = time.time() + 300
                while time.time() < deadline:
                    try:
                        cur_url = page.url.lower()
                    except Exception:
                        await asyncio.sleep(2)
                        continue

                    if _is_login_url(cur_url):
                        log.info("Arrived at login page.")
                        break
                        
                    # Check if we bypassed login entirely (already logged in)
                    if _is_portal_url(cur_url):
                        log.info("Already logged in! Reached home page directly.")
                        already_logged_in = True
                        break
                        
                    await asyncio.sleep(5)
                else:
                    log.warning("Did not auto-redirect in 5 minutes. Trying manual navigation …")
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
                    await wait_for_waiting_room(page, log, timeout_minutes=30)
                    try:
                        cur_url = page.url.lower()
                        if _is_portal_url(cur_url):
                            log.info("Existing session redirected to portal after manual navigation.")
                            already_logged_in = True
                    except Exception:
                        pass
                    
            if already_logged_in:
                # Skip to step 5 / READY
                pass
            else:
                await human_delay(1000, 2000)

            if not already_logged_in:
                # ── 4. Login ──────────────────────────────
                if not FASTCAPTCHA_API_KEY:
                    log.error("Fresh login requires FASTCAPTCHA_API_KEY because this session was not already authenticated.")
                    return

                success = False
                for attempt in range(1, 6):
                    log.info(f"Login attempt {attempt}/5")
                    success = await login(page, args.username, args.password, FASTCAPTCHA_API_KEY, log)
                    if success:
                        break
                    
                    if attempt < 5:
                        log.info("Retrying login...")
                        await page.reload()
                        await human_delay(3000, 5000)

                if not success:
                    log.error("Login failed after 5 attempts.")
                    try:
                        send_slack_error(f"❌ *Login Failed*: `{args.customer}` could not log in after 5 consecutive attempts. Bot is restarting.")
                    except Exception as e:
                        log.error(f"Failed to send Slack alert: {e}")
                    await page.screenshot(path=f"login_failed_{args.customer}.png")
                    sys.exit(1)

            if not already_logged_in:
                # ── 5. Security question ──────────────────
                await human_delay(1500, 3000)
                if not await handle_security_question(page, args.username, log):
                    log.error("Security question failed.")
                    await page.screenshot(path=f"security_question_failed_{args.customer}.png")
                    sys.exit(1)
                
                try:
                    log.info("Waiting for portal redirect after login...")
                    await page.wait_for_url("**/*usvisascheduling.com/en-US*", timeout=30_000)
                except Exception as e:
                    log.warning(f"Timeout waiting for portal redirect: {e}")

            # ── Signal orchestrator: login complete ───
            print(f"[READY] {args.customer}", flush=True)
            log.info("=" * 60)
            log.info("✅  Login complete! Keeping browser open …")
            try:
                log.info(f"   URL: {page.url}")
            except Exception:
                pass
            log.info("=" * 60)

            # Keep the browser open so bot2 can use this session
            # If the user manually closes Chrome, disconnect_event will trigger and we will exit.
            await disconnect_event.wait()
            log.error("Browser was disconnected (closed). Exiting login runner...")
            sys.exit(99)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            try:
                await page.screenshot(path=f"error_{args.customer}.png")
                log.info(f"Screenshot → error_{args.customer}.png")
            except Exception:
                pass
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
