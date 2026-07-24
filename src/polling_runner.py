import asyncio
import json
import os
import sys
import time
import logging
import random
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Ensure project root is on the path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.auth.browser import connect_to_chrome, ensure_chrome_debug_running
from src.booking.cdp_client import ensure_on_portal
from src.auth.login import login, wait_for_waiting_room
from src.auth.security import handle_security_question
from src.common.config import ACCOUNTS_FILE
from src.common.state import read_state, get_state_file

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POLLING] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("polling_runner")

def load_running_accounts():
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        # Assuming enabled means action_mode is SNIPER or similar, or just active in accounts manager
        # In gui.py, accounts are usually just loaded. Let's assume all accounts in accounts.json are eligible unless marked disabled.
        return [acc for acc in accounts if acc.get("enabled", True)]
    except Exception as e:
        log.error(f"Failed to load accounts: {e}")
        return []

async def fetch_dates_via_browser(page, match_config=None):
    """
    Executes JS in the context of the browser to fetch OFC dates directly from the official API.

    The existing five-city order is unchanged. When the first OFC date
    qualifying for this account is found, the function returns immediately
    without polling the remaining cities.
    """
    match_config = match_config or {}
    criteria = {
        "ofcCities": match_config.get("ofcCities", []),
        "ofcStartDate": match_config.get("ofcStartDate", ""),
        "ofcEndDate": match_config.get("ofcEndDate", ""),
        "preventImmediate": match_config.get("prevent_immediate", False),
    }

    js_code = """
    async (criteria) => {
        let primaryId = "";
        let appd = "";
        for (const script of Array.from(document.querySelectorAll("script:not([src])"))) {
            const content = script.textContent || "";
            let pMatch = /['"]?(?:primaryId|applicantUuid|ApplicationID)['"]?\\s*:\\s*['"]([0-9a-f-]{36})['"]/gi.exec(content);
            if (pMatch && pMatch[1]) primaryId = pMatch[1];
            let aMatch = /['"]?(?:contactId|appd|scheduleGroupId|familyId)['"]?\\s*:\\s*['"]([0-9a-f-]{36})['"]/gi.exec(content);
            if (aMatch && aMatch[1]) appd = aMatch[1];
        }
        
        if (!primaryId || !appd) {
            return { error: "Could not find primaryId or appd in page context." };
        }

        const headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest"
        };

        // Fetch Family Members to get all application IDs
        let applicationIds = [primaryId];
        try {
            const familyUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/query-family-members-ofc&appd=${appd}&cacheString=${Date.now()}`;
            const bodyStr = `parameters=${encodeURIComponent(JSON.stringify({ primaryId: primaryId, visaClass: "all" }))}`;
            const res = await fetch(familyUrl, { method: 'POST', headers, body: bodyStr });
            const data = await res.json();
            if (data && data.Members) {
                applicationIds = data.Members.map(m => m.ApplicationID).filter(Boolean);
            }
        } catch (e) {
            console.error(e);
        }
        if (applicationIds.length === 0) applicationIds = [primaryId];

        const OFC_LOCATION_MAP = {
            "CHENNAI": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
            "HYDERABAD": "436bf614-b0db-ec11-a7b4-001dd80234f6",
            "KOLKATA": "466bf614-b0db-ec11-a7b4-001dd80234f6",
            "MUMBAI": "486bf614-b0db-ec11-a7b4-001dd80234f6",
            "NEW DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6"
        };

        const isRescheduleUrl = window.location.href.toLowerCase().includes("reschedule");
        const results = {};

        const normalizeCity = (value) => {
            const city = String(value || "").trim().toUpperCase();
            return city === "DELHI" ? "NEW DELHI" : city;
        };

        const targetCities = new Set(
            (criteria.ofcCities || []).map(normalizeCity)
        );

        let effectiveStartDate = criteria.ofcStartDate || "";
        const effectiveEndDate = criteria.ofcEndDate || "";

        if (criteria.preventImmediate) {
            const dynamicStart = new Date();
            dynamicStart.setDate(dynamicStart.getDate() + 3);
            const dynamicStartDate =
                `${dynamicStart.getFullYear()}-` +
                `${String(dynamicStart.getMonth() + 1).padStart(2, "0")}-` +
                `${String(dynamicStart.getDate()).padStart(2, "0")}`;

            if (!effectiveStartDate || effectiveStartDate < dynamicStartDate) {
                effectiveStartDate = dynamicStartDate;
            }
        }

        for (const [city, postId] of Object.entries(OFC_LOCATION_MAP)) {
            try {
                const dateUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/get-family-ofc-schedule-days&appd=${appd}&cacheString=${Date.now()}`;
                const payload = {
                    primaryId: primaryId,
                    applications: applicationIds,
                    scheduleDayId: "",
                    scheduleEntryId: "",
                    postId: postId,
                    isReschedule: isRescheduleUrl ? "true" : "false"
                };
                const bodyStr = `parameters=${encodeURIComponent(JSON.stringify(payload))}`;
                const res = await fetch(dateUrl, { method: 'POST', headers, body: bodyStr });
                const text = await res.text();
                
                try {
                    const data = JSON.parse(text);
                    if (data && data.ScheduleDays) {
                        results[city] = data.ScheduleDays;

                        const cityIsSelected = targetCities.has(
                            normalizeCity(city)
                        );

                        const qualifyingDate = cityIsSelected
                            ? data.ScheduleDays.find((item) => {
                                  const dateValue = String(
                                      item.Date || item
                                  ).slice(0, 10);

                                  return (
                                      effectiveStartDate &&
                                      effectiveEndDate &&
                                      dateValue >= effectiveStartDate &&
                                      dateValue <= effectiveEndDate
                                  );
                              })
                            : null;

                        if (qualifyingDate) {
                            const matchedDate = String(
                                qualifyingDate.Date || qualifyingDate
                            ).slice(0, 10);

                            console.log(
                                `[Polling] Qualifying OFC date found: ` +
                                `${city} ${matchedDate}. ` +
                                `Stopping remaining city polling.`
                            );

                            return {
                                success: true,
                                results: results,
                                earlyMatch: {
                                    city: city,
                                    date: matchedDate
                                }
                            };
                        }
                    } else {
                        results[city] = [];
                    }
                } catch (e) {
                    const snippet = text.substring(0, 150).replace(/\\n/g, " ").replace(/\\r/g, "");
                    results[city] = { error: `Not JSON. HTML Snippet: ${snippet}` };
                }
            } catch (e) {
                results[city] = { error: e.message };
            }
            await new Promise(r => setTimeout(r, 1500));
        }
        
        return { success: true, results: results };
    }
    """
    return await page.evaluate(js_code, criteria)

async def poll_account(account, p):
    username = account.get("username")
    password = account.get("password")
    customer_name = account.get("customer_name", username)

    log.info(f"🚀 Starting polling cycle for account: {customer_name} ({username})")

    cdp_port = 9500 + random.randint(1, 999)
    profile_dir = str((Path(_project_root) / f"chrome_profile_{username}_polling").resolve())
    
    login_script = Path(_project_root) / "src" / "login_runner.py"
    cmd = [
        sys.executable, str(login_script),
        "--username", username,
        "--password", password,
        "--cdp-port", str(cdp_port),
        "--customer", customer_name,
        "--profile-dir", profile_dir,
    ]
    
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    
    login_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(_project_root),
        env=env
    )
    
    ready = False
    
    def read_output():
        nonlocal ready
        for line in iter(login_proc.stdout.readline, ''):
            if line:
                log.info(f"[LOGIN] {line.rstrip()}")
                if "[READY]" in line:
                    ready = True
                    break
                    
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, read_output)
    
    if not ready:
        log.error("Login runner failed to reach [READY] state.")
        login_proc.kill()
        _kill_chrome_by_port(cdp_port)
        return False
        
    log.info(f"✅ Login complete! Connecting to Chrome on port {cdp_port}...")
    
    browser = None
    try:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0]
        page = context.pages[0]

        log.info("Checking for dashboard buttons to navigate to Schedule page...")
        if await page.locator("text='Reschedule Appointment'").is_visible():
            log.info("Clicking Reschedule Appointment...")
            await page.locator("text='Reschedule Appointment'").first.click()
        elif await page.locator("text='Schedule Appointment'").is_visible():
            log.info("Clicking Schedule Appointment...")
            await page.locator("text='Schedule Appointment'").first.click()
        elif await page.locator("text='Continue Application'").is_visible():
            log.info("Clicking Continue Application...")
            await page.locator("text='Continue Application'").first.click()
        
        await asyncio.sleep(5)

        log.info("Executing API fetch directly via browser context...")
        data = await fetch_dates_via_browser(page, account)
        
        if data.get("error"):
            log.error(f"Failed to fetch data: {data['error']}")
            return False

        if data.get("success"):
            # Detect a silently-expired session: every city came back as a
            # login HTML page instead of JSON. Treat as a failed poll (return
            # False) so the caller does NOT put the account into a long
            # "successful" cooldown — it should re-login on the next cycle.
            results = data.get("results", {}) or {}
            html_errors = [
                city for city, dates in results.items()
                if isinstance(dates, dict)
                and ("Not JSON" in str(dates.get("error", "")) or "HTML" in str(dates.get("error", "")))
            ]
            if results and len(html_errors) == len(results):
                log.error(
                    f"❌ Polling returned login HTML for ALL cities for {customer_name} "
                    f"({', '.join(html_errors)}). Session is expired — treating as failed poll."
                )
                return False

            log.info(f"✅ Successfully fetched dates for {customer_name}:")
            for city, dates in results.items():
                if isinstance(dates, list) and len(dates) > 0:
                    log.info(f"  📍 {city}: {len(dates)} dates available (Earliest: {dates[0].get('Date')})")
                elif isinstance(dates, dict):
                    log.warning(f"  📍 {city}: session/API error — {str(dates.get('error', ''))[:120]}")
                else:
                    log.info(f"  📍 {city}: No dates available.")
            return True
            
    except Exception as e:
        log.error(f"Error during polling for {username}: {e}")
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        
        try:
            login_proc.kill()
        except Exception:
            pass
            
        _kill_chrome_by_port(cdp_port)
        log.info("Closed browser session and killed login runner.")

def _kill_chrome_by_port(cdp_port: int):
    import subprocess
    try:
        output = subprocess.check_output(f"netstat -ano | findstr :{cdp_port}", shell=True, text=True)
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[1].endswith(f":{cdp_port}"):
                pid = parts[-1]
                if pid != "0":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    except Exception:
        pass

async def run_polling_loop(cooldown_minutes: int, gap_minutes: int):
    cooldown_map = {}
    
    async with async_playwright() as p:
        while True:
            accounts = load_running_accounts()
            if not accounts:
                log.info("No running accounts found. Waiting...")
                await asyncio.sleep(60)
                continue
                
            now = datetime.now()
            account_polled = False
            
            for account in accounts:
                username = account.get("username")
                
                # Check cooldown
                if username in cooldown_map:
                    cooldown_end = cooldown_map[username]
                    if now < cooldown_end:
                        log.debug(f"Skipping {username}, in cooldown until {cooldown_end.strftime('%H:%M:%S')}")
                        continue
                
                # Check state guard (skip if booking is active)
                state_file = get_state_file(username)
                if state_file.exists():
                    state = read_state(state_file)
                    if state.get("extension_running") or state.get("pending"):
                        log.info(f"Skipping {username}, account is currently busy with a booking.")
                        continue

                # We have an eligible account
                success = await poll_account(account, p)
                account_polled = True

                if success:
                    # Only place a long cooldown on a successful poll.
                    cooldown_map[username] = datetime.now() + timedelta(minutes=cooldown_minutes)
                    log.info(f"Placed {username} in cooldown for {cooldown_minutes} minutes.")
                else:
                    # A failed poll (e.g. expired session, login failure) should
                    # retry soon, not wait a full cooldown. Use a short backoff
                    # so the account re-attempts login on the next cycle.
                    short_backoff = 5
                    cooldown_map[username] = datetime.now() + timedelta(minutes=short_backoff)
                    log.info(f"Poll failed for {username}. Short retry cooldown of {short_backoff} minutes.")
                
                # Wait for gap before next account
                log.info(f"Waiting for gap period: {gap_minutes} minutes...")
                await asyncio.sleep(gap_minutes * 60)
                break # Only process one account per loop iteration to re-check states
                
            if not account_polled:
                # All accounts are in cooldown, just sleep for a bit and re-eval
                await asyncio.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cooldown", type=int, default=60, help="Cooldown per account in minutes")
    parser.add_argument("--gap", type=int, default=15, help="Gap between accounts in minutes")
    args = parser.parse_args()
    
    try:
        asyncio.run(run_polling_loop(args.cooldown, args.gap))
    except KeyboardInterrupt:
        log.info("Polling runner stopped.")