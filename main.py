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
#  Load credentials from CSV
# ─────────────────────────────────────────────────────────────────────────────

def load_credentials(filepath: str) -> list[dict]:
    """Returns a list of dicts with keys: customer_id, username, password."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Credentials file not found: {filepath}")

    customers = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Strip whitespace from all fields
            customers.append({k: v.strip() for k, v in row.items()})

    logger.info(f"Loaded {len(customers)} customer(s) from {filepath}")
    return customers


# ─────────────────────────────────────────────────────────────────────────────
#  Semaphore-controlled worker  (respects MAX_WORKERS)
# ─────────────────────────────────────────────────────────────────────────────

async def bounded_login(semaphore: asyncio.Semaphore, browser, customer: dict, delay: float):
    """Acquires a slot in the semaphore, waits `delay` seconds, then logs in."""
    async with semaphore:
        await asyncio.sleep(delay)
        return await login_customer(
            browser=browser,
            customer_id=customer["customer_id"],
            username=customer["username"],
            password=customer["password"],
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Main async runner
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    customers = load_credentials(CREDENTIALS_FILE)

    if not customers:
        logger.warning("No customers found in credentials file. Exiting.")
        return

    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async with async_playwright() as playwright:
        # Launch a single shared browser instance
        browser = await playwright.chromium.launch(headless=HEADLESS)
        logger.info(f"Browser launched | headless={HEADLESS} | max_workers={MAX_WORKERS}")

        # Build all login tasks, staggered by DELAY_BETWEEN_LOGINS
        tasks = [
            bounded_login(semaphore, browser, customer, idx * DELAY_BETWEEN_LOGINS)
            for idx, customer in enumerate(customers)
        ]

        start = time.perf_counter()
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        await browser.close()

    # ── Print a summary report ────────────────────────────────────────────────
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
