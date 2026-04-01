"""
bot.py  –  Core login + session-save logic for a single customer.
Each customer gets their own isolated Playwright BrowserContext.
"""

import asyncio
import logging
import os

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout

from config import (
    LOGIN_URL,
    SESSIONS_DIR,
    TIMEOUT_MS,
    FAST_CAPTCHA_API_KEY,
    WAITING_ROOM_TIMEOUT_MS,
    POST_LOGIN_STAY_OPEN_MS,
    USE_PROXY,
    PROXY_SERVER,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry-point  –  called once per customer from main.py
# ─────────────────────────────────────────────────────────────────────────────

async def login_customer(
    browser: Browser,
    customer_id: str,
    username: str,
    password: str,
) -> dict:
    """
    Opens an isolated browser context, logs in the customer, saves the session,
    then closes the context.

    Returns a result dict:
        {
            "customer_id": str,
            "status":      "success" | "failed",
            "session_file": str | None,
            "error":       str | None,
        }
    """
    context: BrowserContext | None = None

    try:
        # Construct proxy settings if enabled
        proxy_settings = None
        if USE_PROXY and PROXY_SERVER:
            proxy_settings = {"server": PROXY_SERVER}

        # ── Step 1 · Create a fully isolated browser context ─────────────────
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            proxy=proxy_settings,
        )
        context.set_default_timeout(TIMEOUT_MS)
        page: Page = await context.new_page()

        # ── Step 2 · Navigate to the login page ──────────────────────────────
        logger.info(f"[{customer_id}] Navigating to {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # ── Step 3 · Smart auto-detect and fill credentials ─────────────────
        logger.info(f"[{customer_id}] Filling credentials for {username}")
        
        # Smart Locator 1: Username/Email
        # Looks for email types, or inputs with "user" / "email" in their name/id/placeholder,
        # or just the first generic text-like input on the page.
        user_input = page.locator(
            'input[type="email"], '
            'input[name*="user" i], input[id*="user" i], input[placeholder*="user" i], '
            'input[name*="email" i], input[id*="email" i], input[placeholder*="email" i], '
            'input:not([type="hidden"]):not([type="password"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"])'
        ).first
        
        # Smart Locator 2: Password
        pwd_input = page.locator('input[type="password"]').first
        
        # Smart Locator 3: Submit Button
        # Looks for submit inputs/buttons, or buttons containing login-related text.
        submit_btn = page.locator(
            'button[type="submit"], input[type="submit"], '
            'button:has-text("Log in"), button:has-text("Login"), '
            'button:has-text("Sign in"), button:has-text("Signin"), '
            'button:has-text("Submit"), button:has-text("Continue")'
        ).first

        # Wait for the login form to appear (Handles Waiting Rooms / Queues!)
        logger.info(f"[{customer_id}] Waiting for the login form to appear (Bypassing Waiting Rooms if present)...")
        await user_input.wait_for(state="visible", timeout=WAITING_ROOM_TIMEOUT_MS)

        await user_input.fill(username)
        await pwd_input.fill(password)

        # ── Step 3.5 · Handle CAPTCHA if present ─────────────────────────────
        # specific to the US Visa Scheduling portal (and similar generic Azure B2C)
        captcha_img = page.locator('#captchaImage')
        
        # Give the page up to 3 seconds to load the CAPTCHA image
        try:
            await captcha_img.wait_for(state="visible", timeout=3000)
        except PlaywrightTimeout:
            pass # No CAPTCHA appeared within 3 seconds

        if await captcha_img.is_visible():
            logger.info(f"[{customer_id}] CAPTCHA detected. Solving with FastCaptcha...")
            
            # Wait briefly to ensure the image has finished rendering
            await page.wait_for_timeout(1500)

            try:
                # The easiest and most reliable way to extract ANY image regardless of if it's 
                # base64, a blob: URL, or an external link, is to just ask Playwright to take 
                # a screenshot of that specific HTML element and give us the raw bytes.
                img_bytes = await captcha_img.screenshot(type="jpeg")
                
                logger.info(f"[{customer_id}] Successfully extracted CAPTCHA image bytes.")
                
                # Send to FastCaptcha OCR API
                resp = await context.request.post(
                    "https://fastcaptcha.org/api/v1/ocr/",
                    headers={"X-API-Key": FAST_CAPTCHA_API_KEY},
                    multipart={
                        "image": {
                            "name": "captcha.jpg",
                            "mimeType": "image/jpeg",
                            "buffer": img_bytes,
                        }
                    }
                )
                
                if resp.ok:
                    data = await resp.json()
                    captcha_text = data.get("text")
                    if captcha_text:
                        logger.info(f"[{customer_id}] Solved CAPTCHA: {captcha_text}")
                        await page.fill('#extension_atlasCaptchaResponse', captcha_text)
                        await page.wait_for_timeout(500) # Quick pause to mimic human input
                    else:
                        logger.error(f"[{customer_id}] FastCaptcha returned no text! Resp: {data}")
                else:
                    logger.error(f"[{customer_id}] FastCaptcha API error: {resp.status} - {await resp.text()}")
            except Exception as e:
                logger.error(f"[{customer_id}] Failed to extract or solve CAPTCHA: {e}")

        # ── Step 4 · Click the login button ──────────────────────────────────
        await submit_btn.click()

        # ── Step 4 · Confirm login was successful ────────────────────────────
        # Wait for the URL to change from the initial login page
        try:
            await page.wait_for_url(
                lambda u: u.split('?')[0] != LOGIN_URL.split('?')[0], 
                timeout=TIMEOUT_MS
            )
        except PlaywrightTimeout:
            # If URL didn't change (e.g. Single Page Apps), wait for network requests to settle
            await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            
            # Simple heuristic: If the password field is still usable, login likely failed (e.g. validation error)
            if await pwd_input.is_visible() and await pwd_input.is_editable():
                raise Exception("Login form still active after submission. Login likely failed.")
                
        logger.info(f"[{customer_id}] Login successful ✅")

        # ── Step 6 · Save the session (cookies + localStorage) ───────────────
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(SESSIONS_DIR, f"{customer_id}_session.json")
        await context.storage_state(path=session_path)
        logger.info(f"[{customer_id}] Session saved → {session_path}")

        # Keep window open so the user can see the results before it closes
        if POST_LOGIN_STAY_OPEN_MS > 0:
            logger.info(f"[{customer_id}] Keeping window open for {POST_LOGIN_STAY_OPEN_MS/1000}s to inspect results...")
            await page.wait_for_timeout(POST_LOGIN_STAY_OPEN_MS)

        return {
            "customer_id": customer_id,
            "status": "success",
            "session_file": session_path,
            "error": None,
        }

    except PlaywrightTimeout:
        msg = f"Timed-out waiting for post-login element. Wrong credentials or CAPTCHA?"
        logger.error(f"[{customer_id}] ❌ {msg}")
        return {"customer_id": customer_id, "status": "failed", "session_file": None, "error": msg}

    except Exception as exc:
        logger.error(f"[{customer_id}] ❌ Unexpected error: {exc}")
        return {"customer_id": customer_id, "status": "failed", "session_file": None, "error": str(exc)}

    finally:
        if context:
            await context.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Helper – reuse a saved session without logging in again
# ─────────────────────────────────────────────────────────────────────────────

async def open_existing_session(browser: Browser, customer_id: str) -> BrowserContext | None:
    """
    Loads a previously saved session file and returns an active BrowserContext.
    No login is performed – the context is already authenticated.

    Usage:
        ctx  = await open_existing_session(browser, "customer_001")
        page = await ctx.new_page()
        await page.goto("https://example.com/dashboard")
    """
    session_path = os.path.join(SESSIONS_DIR, f"{customer_id}_session.json")

    if not os.path.exists(session_path):
        logger.warning(f"[{customer_id}] No saved session found at {session_path}")
        return None

    context = await browser.new_context(storage_state=session_path)
    logger.info(f"[{customer_id}] Loaded saved session from {session_path}")
    return context
