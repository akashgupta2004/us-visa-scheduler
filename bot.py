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
#  Security question keyword → CSV column name
#  The bot reads the label text of each question, finds a keyword match here,
#  then looks up the answer from the per-customer answers dict.
# ─────────────────────────────────────────────────────────────────────────────
QUESTION_KEYWORD_MAP = {
    # Exact phrases seen on the US Visa Scheduling portal
    "first car":        "ans_car",
    "childhood hero":   "ans_hero",
    "favourite food":   "ans_food",
    "favorite food":    "ans_food",
    "favourite car":    "ans_car",
    "favorite car":     "ans_car",
    "childhood pet":    "ans_pet",
    "favourite pet":    "ans_pet",
    "favorite pet":     "ans_pet",
    "first job":        "ans_job",
    "born":             "ans_city",
    "hometown":         "ans_city",
    "childhood friend": "ans_teacher",
    "favourite movie":  "ans_movie",
    "favorite movie":   "ans_movie",
    # Fallback single keywords (less specific, but catches edge cases)
    "car":     "ans_car",
    "food":    "ans_food",
    "hero":    "ans_hero",
    "pet":     "ans_pet",
    "job":     "ans_job",
    "city":    "ans_city",
    "sport":   "ans_sport",
    "color":   "ans_color",
    "colour":  "ans_color",
    "teacher": "ans_teacher",
    "movie":   "ans_movie",
    "film":    "ans_movie",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry-point  –  called once per customer from main.py
# ─────────────────────────────────────────────────────────────────────────────

async def login_customer(
    browser: Browser,
    customer_id: str,
    username: str,
    password: str,
    answers: dict,          # Per-customer security-question answers from CSV
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
        # ── Step 1 · Create a fully isolated browser context ─────────────────
        proxy_settings = {"server": PROXY_SERVER} if USE_PROXY and PROXY_SERVER else None

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

        # ── Step 3 · Fill in credentials ─────────────────────────────────────
        logger.info(f"[{customer_id}] Filling credentials for {username}")

        # Azure B2C (which powers this portal) uses a specific ID for the username field: signInName
        # We list it first, then fall back to generic selectors for other portal types.
        user_input = page.locator(
            'input#signInName, '
            'input[type="email"], '
            'input[name*="user" i], input[id*="user" i], input[placeholder*="user" i], '
            'input[name*="email" i], input[id*="email" i], input[placeholder*="email" i], '
            'input:not([type="hidden"]):not([type="password"]):not([type="submit"])'
            ':not([type="checkbox"]):not([type="radio"])'
        ).first
        pwd_input  = page.locator('input#password, input[type="password"]').first
        submit_btn = page.locator(
            'button#next, '
            'button[type="submit"], input[type="submit"], '
            'button:has-text("Log in"), button:has-text("Login"), '
            'button:has-text("Sign in"), button:has-text("Signin"), '
            'button:has-text("Submit"), button:has-text("Continue")'
        ).first

        # Wait for the login form (handles waiting rooms / queues)
        logger.info(f"[{customer_id}] Waiting for login form (bypassing waiting rooms if present)...")
        await user_input.wait_for(state="visible", timeout=WAITING_ROOM_TIMEOUT_MS)

        await user_input.fill(username)
        await pwd_input.fill(password)

        # ── Step 4 · Solve CAPTCHA if present ───────────────────────────────
        captcha_img = page.locator('#captchaImage')
        try:
            await captcha_img.wait_for(state="visible", timeout=3000)
        except PlaywrightTimeout:
            pass  # No CAPTCHA on this page load

        if await captcha_img.is_visible():
            logger.info(f"[{customer_id}] CAPTCHA detected. Solving with FastCaptcha...")
            await page.wait_for_timeout(1500)  # Let image fully render

            try:
                img_bytes = await captcha_img.screenshot(type="jpeg")
                logger.info(f"[{customer_id}] Extracted CAPTCHA image bytes.")

                # Up to 3 retries for network drops (ECONNRESET)
                for attempt in range(3):
                    try:
                        resp = await context.request.post(
                            "https://fastcaptcha.org/api/v1/ocr/",
                            headers={"X-API-Key": FAST_CAPTCHA_API_KEY},
                            multipart={
                                "image": {
                                    "name": "captcha.jpg",
                                    "mimeType": "image/jpeg",
                                    "buffer": img_bytes,
                                }
                            },
                        )
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise e
                        logger.warning(f"[{customer_id}] FastCaptcha dropped, retrying... ({e})")
                        await asyncio.sleep(1)

                if resp.ok:
                    data = await resp.json()
                    captcha_text = data.get("text", "").strip()
                    if captcha_text:
                        logger.info(f"[{customer_id}] CAPTCHA solved: {captcha_text}")
                        await page.fill('#extension_atlasCaptchaResponse', captcha_text)
                        await page.wait_for_timeout(500)
                    else:
                        logger.error(f"[{customer_id}] FastCaptcha returned empty text! Response: {data}")
                else:
                    logger.error(f"[{customer_id}] FastCaptcha API error {resp.status}: {await resp.text()}")

            except Exception as e:
                logger.error(f"[{customer_id}] CAPTCHA solving failed: {e}")

        # ── Step 5 · Submit the login form ───────────────────────────────────
        await submit_btn.click()
        logger.info(f"[{customer_id}] Login form submitted. Waiting for redirect away from Microsoft B2C...")

        # Wait for B2C to redirect us somewhere (either the dashboard OR the security questions page)
        try:
            await page.wait_for_url(
                lambda u: "b2clogin.com" not in u,
                timeout=TIMEOUT_MS,
            )
        except PlaywrightTimeout:
            # B2C did not redirect — CAPTCHA was likely wrong
            error_img = os.path.join(SESSIONS_DIR, f"{customer_id}_error.png")
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            await page.screenshot(path=error_img, full_page=True)
            logger.error(f"[{customer_id}] Still on B2C after submit — CAPTCHA likely wrong. Screenshot: {error_img}")
            raise Exception("Login failed: B2C did not redirect. CAPTCHA was likely incorrect.")

        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        logger.info(f"[{customer_id}] Redirected to: {page.url}")

        # ── Step 6 · Handle Security Questions on US Visa portal ─────────────
        # After the B2C redirect, usvisascheduling.com may show a "User Details"
        # page with 2 random security questions before granting dashboard access.
        await _answer_security_questions(page, customer_id, answers)

        # ── Step 7 · Verify we actually landed on the dashboard ───────────────
        # We should now be on the US Visa Scheduling dashboard, NOT a login/security page.
        if "b2clogin.com" in page.url or "/Account/Login" in page.url or "UserDetails" in page.url:
            error_img = os.path.join(SESSIONS_DIR, f"{customer_id}_error.png")
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            await page.screenshot(path=error_img, full_page=True)
            logger.error(f"[{customer_id}] Still on auth/security page! URL: {page.url}")
            logger.error(f"[{customer_id}] Error screenshot saved → {error_img}")
            raise Exception(
                f"Login failed: security questions may not have been answered correctly. "
                f"Check {error_img} for a screenshot."
            )

        logger.info(f"[{customer_id}] Login successful ✅")

        # ── Step 8 · Save the authenticated session ───────────────────────────
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(SESSIONS_DIR, f"{customer_id}_session.json")
        await context.storage_state(path=session_path)
        logger.info(f"[{customer_id}] Session saved → {session_path}")

        # Keep window open for inspection if configured
        if POST_LOGIN_STAY_OPEN_MS > 0:
            logger.info(f"[{customer_id}] Keeping window open for {POST_LOGIN_STAY_OPEN_MS / 1000:.0f}s...")
            try:
                await page.wait_for_timeout(POST_LOGIN_STAY_OPEN_MS)
            except Exception:
                pass  # Browser was closed manually by the user – that is fine

        return {
            "customer_id": customer_id,
            "status": "success",
            "session_file": session_path,
            "error": None,
        }

    except PlaywrightTimeout:
        msg = "Timed-out waiting for a page element. Check the site, credentials, or CAPTCHA."
        logger.error(f"[{customer_id}] ❌ {msg}")
        return {"customer_id": customer_id, "status": "failed", "session_file": None, "error": msg}

    except Exception as exc:
        logger.error(f"[{customer_id}] ❌ Unexpected error: {exc}")
        return {"customer_id": customer_id, "status": "failed", "session_file": None, "error": str(exc)}

    finally:
        if context:
            await context.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helper: dynamically answer all security questions on the page
# ─────────────────────────────────────────────────────────────────────────────

async def _answer_security_questions(page: Page, customer_id: str, answers: dict) -> None:
    """
    Reads every <label> on the current page.
    If a label contains a keyword we know (car, food, hero, pet, job…),
    we find the <input> associated with that label and fill in the
    per-customer answer from the `answers` dict.
    If no security question inputs are found, returns immediately.
    """
    try:
        # Collect all label elements on the page
        labels = page.locator("label")
        count  = await labels.count()

        if count == 0:
            logger.debug(f"[{customer_id}] No <label> elements found on page — skipping security questions.")
            return

        # Log ALL visible label texts so we can see what the page is showing
        visible_labels = []
        for i in range(count):
            lbl = labels.nth(i)
            if await lbl.is_visible():
                txt = (await lbl.text_content() or "").strip()
                if txt:
                    visible_labels.append(txt)
        if visible_labels:
            logger.info(f"[{customer_id}] Visible labels on current page: {visible_labels}")

        answered = 0
        for i in range(count):
            label = labels.nth(i)

            # Only look at visible labels
            if not await label.is_visible():
                continue

            label_text = (await label.text_content() or "").lower().strip()

            # Find which keyword (if any) this label contains
            matched_key = None
            for keyword, csv_col in QUESTION_KEYWORD_MAP.items():
                if keyword in label_text:
                    matched_key = csv_col
                    break

            if not matched_key:
                continue

            answer = answers.get(matched_key, "").strip()
            if not answer or answer.lower() == "na":
                logger.warning(
                    f"[{customer_id}] Security question about '{label_text}' found "
                    f"but no answer in CSV column '{matched_key}'. Skipping."
                )
                continue

            # Find the input that belongs to this label.
            # Priority: label[for] → input[id], then nearest sibling/descendant input.
            for_attr = await label.get_attribute("for")
            if for_attr:
                target_input = page.locator(f"input#{for_attr}").first
            else:
                target_input = label.locator("xpath=following-sibling::input | following-sibling::div//input").first

            if not await target_input.is_visible():
                logger.warning(f"[{customer_id}] Label '{label_text}' found but input is not visible.")
                continue

            logger.info(f"[{customer_id}] Answering security question: '{label_text}' → '{answer}'")
            await target_input.fill(answer)
            await page.wait_for_timeout(300)   # Tiny human-like pause between answers
            answered += 1

        if answered > 0:
            logger.info(f"[{customer_id}] Answered {answered} security question(s). Clicking Continue...")
            continue_btn = page.locator(
                "#continue, "
                "button:has-text('Continue'), "
                "input[type='submit'][value*='Continue' i], "
                "button[type='submit']"
            ).first
            await continue_btn.click()

            # Wait for the final redirect away from the B2C auth domain
            try:
                await page.wait_for_url(
                    lambda u: "b2clogin.com" not in u and "signin" not in u.lower(),
                    timeout=TIMEOUT_MS,
                )
            except PlaywrightTimeout:
                pass  # Will be caught by the Step 7 check

            await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

    except Exception as e:
        # Non-fatal – Step 7 will confirm if login actually worked
        logger.debug(f"[{customer_id}] Security questions helper error (non-fatal): {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Helper – reuse a saved session without logging in again
# ─────────────────────────────────────────────────────────────────────────────

async def open_existing_session(browser: Browser, customer_id: str) -> BrowserContext | None:
    """
    Loads a previously saved session file and returns an active BrowserContext.
    No login is performed – the context is already authenticated.
    """
    session_path = os.path.join(SESSIONS_DIR, f"{customer_id}_session.json")
    if not os.path.exists(session_path):
        logger.warning(f"[{customer_id}] No saved session found at {session_path}")
        return None
    context = await browser.new_context(storage_state=session_path)
    logger.info(f"[{customer_id}] Loaded saved session from {session_path}")
    return context
