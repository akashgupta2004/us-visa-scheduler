"""
main.py  –  Orchestrates concurrent logins for all customers in credentials.csv
Run:  python main.py
"""

import asyncio
import csv
import logging
import os
import time

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from bot import login_customer
from config import (
    CREDENTIALS_FILE,
    HEADLESS,
    LOG_FILE,
    MAX_WORKERS,
    DELAY_BETWEEN_LOGINS,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Logging setup  (console + file)
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Load credentials from CSV  (all columns are passed through)
# ─────────────────────────────────────────────────────────────────────────────

def load_credentials(filepath: str) -> list[dict]:
    """
    Returns a list of dicts with keys matching every CSV column header.
    Required columns: customer_id, username, password
    Optional columns: ans_car, ans_food, ans_hero, ans_pet, ans_job,
                      ans_city, ans_sport, ans_color, ans_teacher, ans_movie
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Credentials file not found: {filepath}")

    customers = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            customers.append({k.strip(): v.strip() for k, v in row.items()})

    logger.info(f"Loaded {len(customers)} customer(s) from {filepath}")
    return customers


# ─────────────────────────────────────────────────────────────────────────────
#  Semaphore-controlled worker  (respects MAX_WORKERS)
# ─────────────────────────────────────────────────────────────────────────────

async def bounded_login(semaphore: asyncio.Semaphore, browser, customer: dict, delay: float):
    """Acquires a semaphore slot, waits `delay` seconds, then logs in with automatic CAPTCHA retries."""
    async with semaphore:
        await asyncio.sleep(delay)
        customer_id = customer["customer_id"]

        # Retry up to 3 times automatically if CAPTCHA is wrong
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                logger.info(f"[{customer_id}] Retrying login (attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(3)   # Brief pause before re-attempting

            result = await login_customer(
                browser=browser,
                customer_id=customer["customer_id"],
                username=customer["username"],
                password=customer["password"],
                answers=customer,
            )

            if result["status"] == "success":
                return result

            # Only retry on CAPTCHA-related failures — don't retry credential errors
            error = result.get("error", "")
            if "CAPTCHA" in error or "B2C did not redirect" in error:
                if attempt < max_attempts:
                    logger.warning(f"[{customer_id}] CAPTCHA failure on attempt {attempt}. Will retry...")
                    continue

            # Non-retryable error or exhausted retries
            return result

        return result  # Return last result after all retries exhausted



# ─────────────────────────────────────────────────────────────────────────────
#  Main async runner
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    customers = load_credentials(CREDENTIALS_FILE)

    if not customers:
        logger.warning("No customers found in credentials file. Exiting.")
        return

    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async with Stealth().use_async(async_playwright()) as playwright:
        browser = await playwright.chromium.launch(headless=HEADLESS)
        logger.info(f"Browser launched | headless={HEADLESS} | max_workers={MAX_WORKERS}")

        tasks = [
            bounded_login(semaphore, browser, customer, idx * DELAY_BETWEEN_LOGINS)
            for idx, customer in enumerate(customers)
        ]

        start   = time.perf_counter()
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        await browser.close()

    # ── Print summary report ──────────────────────────────────────────────────
    success = [r for r in results if r["status"] == "success"]
    failed  = [r for r in results if r["status"] == "failed"]

    print("\n" + "=" * 60)
    print(f"  RESULTS  –  {len(success)} succeeded  |  {len(failed)} failed  |  {elapsed:.1f}s")
    print("=" * 60)

    if success:
        print("\n✅  Saved sessions:")
        for r in success:
            print(f"    [{r['customer_id']}]  →  {r['session_file']}")

    if failed:
        print("\n❌  Failed logins:")
        for r in failed:
            print(f"    [{r['customer_id']}]  →  {r['error']}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run())
