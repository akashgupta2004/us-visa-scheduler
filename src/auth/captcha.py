import os
import base64
import logging
from playwright.async_api import Page
from src.auth.utils import human_delay, human_type

# ── FastCaptcha ──────────────────────────────────────────────
try:
    from fastcaptcha import FastCaptcha
    _FC_AVAILABLE = True
except ImportError:
    _FC_AVAILABLE = False

def _fastcaptcha_solve(image_bytes: bytes, api_key: str, log: logging.Logger) -> str:
    if not _FC_AVAILABLE:
        raise RuntimeError("fastcaptcha-api not installed.")
    if not api_key:
        raise RuntimeError("FASTCAPTCHA_API_KEY missing in .env")
    client = FastCaptcha(api_key=api_key)
    b64    = base64.b64encode(image_bytes).decode("utf-8")
    text   = client.solve_base64(b64)
    log.info(f"FastCaptcha → '{text}'")
    return text

async def solve_captcha_on_page(page: Page, api_key: str, log: logging.Logger) -> bool:
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
        solution = _fastcaptcha_solve(img_bytes, api_key, log)
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
