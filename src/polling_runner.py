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
from src.common.platform_utils import kill_process_by_port

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

async def fetch_dates_via_browser(
    page,
    match_config=None,
    city_gap_ms=1500,
    scout_slots=False,
):
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
        "cityGapMs": int(city_gap_ms),
        "prepareScoutSlots": bool(scout_slots),
        "multiPerson": bool(
            match_config.get("multiPerson", False)
        ),
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

        // Fetch Family Members to get all application IDs.
        //
        // In Scout-Slots mode we must preserve the SAME applicant
        // context that the later direct Book request will use.
        let applicationIds = [primaryId];
        let familyQuerySucceeded = false;

        try {
            const familyUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/query-family-members-ofc&appd=${appd}&cacheString=${Date.now()}`;
            const bodyStr = `parameters=${encodeURIComponent(JSON.stringify({
                primaryId: primaryId,
                visaClass: "all"
            }))}`;

            const res = await fetch(
                familyUrl,
                {
                    method: "POST",
                    headers,
                    body: bodyStr
                }
            );

            const data = await res.json();

            if (data && Array.isArray(data.Members)) {
                familyQuerySucceeded = true;

                applicationIds = data.Members
                    .map(m => m.ApplicationID)
                    .filter(Boolean);
            }
        } catch (e) {
            console.error(e);
        }

        if (
            criteria.prepareScoutSlots &&
            criteria.multiPerson &&
            (
                !familyQuerySucceeded ||
                applicationIds.length === 0
            )
        ) {
            return {
                error:
                    "OFC Scout could not safely resolve multiperson " +
                    "application IDs before fetching Slots."
            };
        }

        if (applicationIds.length === 0) {
            applicationIds = [primaryId];
        }

        // For a normal single-person Scout, mint the Dates/Slots
        // token only for the primary applicant.
        //
        // For multiperson Scout, preserve the complete family set.
        const scoutApplicationIds =
            criteria.prepareScoutSlots
                ? (
                    criteria.multiPerson
                        ? [...applicationIds]
                        : [primaryId]
                )
                : [...applicationIds];

        const scoutNumberOfPeople =
            criteria.prepareScoutSlots &&
            criteria.multiPerson
                ? Math.max(1, scoutApplicationIds.length)
                : 1;

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

        // Ask the content-script Scout action to perform the
        // Slots request immediately while we are still inside
        // this same OFC Scout browser evaluation.
        //
        // There is deliberately no short artificial timeout here.
        // The outer Python Scout task remains cancellable/pre-emptible.
        const requestOfcScoutSlots = (config) => {
            return new Promise((resolve) => {
                const handler = (event) => {
                    if (event.source !== window) {
                        return;
                    }

                    const message = event.data || {};

                    if (
                        message.action !==
                        "OFC_SCOUT_SLOTS_RESULT"
                    ) {
                        return;
                    }

                    window.removeEventListener(
                        "message",
                        handler
                    );

                    resolve(message);
                };

                window.addEventListener(
                    "message",
                    handler
                );

                window.postMessage(
                    {
                        action: "EXECUTE_OFC_SCOUT_SLOTS",
                        config: config
                    },
                    "*"
                );
            });
        };

        for (const [city, postId] of Object.entries(OFC_LOCATION_MAP)) {
            try {
                const dateUrl = `/en-US/custom-actions/?route=/api/v1/schedule-group/get-family-ofc-schedule-days&appd=${appd}&cacheString=${Date.now()}`;
                const payload = {
                    primaryId: primaryId,
                    applications: criteria.prepareScoutSlots
                        ? scoutApplicationIds
                        : applicationIds,
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

                        const qualifyingDates = cityIsSelected
                            ? data.ScheduleDays.filter((item) => {
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
                            : [];

                        if (qualifyingDates.length > 0) {
                            // -------------------------------------------------
                            // ORDINARY POLLING
                            //
                            // Preserve existing behaviour exactly:
                            // use only the first qualifying date and return.
                            // -------------------------------------------------
                            if (!criteria.prepareScoutSlots) {
                                const qualifyingDate =
                                    qualifyingDates[0];

                                const matchedDate = String(
                                    qualifyingDate.Date ||
                                    qualifyingDate
                                ).slice(0, 10);

                                const datesToken = String(
                                    data.Token || ""
                                );

                                const datesCapturedAt =
                                    Date.now();

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
                                        date: matchedDate,
                                        token: datesToken,
                                        appd: appd,
                                        isReschedule:
                                            isRescheduleUrl,
                                        capturedAt:
                                            datesCapturedAt
                                    }
                                };
                            }

                            // -------------------------------------------------
                            // OFC SCOUT SLOT FAST-PATH
                            //
                            // Scout must check Slots for EVERY qualifying
                            // date in this city before moving to the next city.
                            //
                            // IMPORTANT:
                            // The portal rotates/chains tokens through Slots
                            // responses, so carry the newest returned token
                            // into the next date's Slots request.
                            // -------------------------------------------------

                            let rollingToken = String(
                                data.Token || ""
                            );

                            let rollingTokenCapturedAt =
                                Date.now();

                            console.log(
                                `[Polling] OFC Scout found ` +
                                `${qualifyingDates.length} qualifying ` +
                                `date(s) in ${city}. ` +
                                `Checking Slots for each date.`
                            );

                            for (
                                let dateIndex = 0;
                                dateIndex < qualifyingDates.length;
                                dateIndex++
                            ) {
                                const qualifyingDate =
                                    qualifyingDates[dateIndex];

                                const matchedDate = String(
                                    qualifyingDate.Date ||
                                    qualifyingDate
                                ).slice(0, 10);

                                // Preserve the exact token/context that
                                // is about to be used for THIS date.
                                const tokenForThisDate =
                                    rollingToken;

                                const tokenForThisDateCapturedAt =
                                    rollingTokenCapturedAt;

                                console.log(
                                    `[Polling] Checking OFC Scout Slots: ` +
                                    `${city} ${matchedDate} ` +
                                    `(${dateIndex + 1}/` +
                                    `${qualifyingDates.length} ` +
                                    `qualifying dates).`
                                );

                                const slotResult =
                                    await requestOfcScoutSlots({
                                        date: matchedDate,
                                        token: tokenForThisDate,
                                        appd: appd,
                                        numberOfPeople:
                                            scoutNumberOfPeople,
                                        isReschedule:
                                            isRescheduleUrl
                                    });

                                if (
                                    !slotResult ||
                                    slotResult.status !== "success"
                                ) {
                                    const slotError = String(
                                        slotResult?.msg ||
                                        "Unknown OFC Scout Slots error."
                                    );

                                    console.warn(
                                        `[Polling] OFC Scout Slots failed for ` +
                                        `${city} ${matchedDate}: ` +
                                        `${slotError}`
                                    );

                                    // Session / 429 must stop this Scout
                                    // attempt. Never keep using a token from
                                    // an unusable authenticated session.
                                    if (
                                        slotResult?.rateLimited ||
                                        slotResult?.sessionExpired
                                    ) {
                                        return {
                                            success: false,
                                            results: results,
                                            error: slotError,
                                            rateLimited:
                                                !!slotResult.rateLimited,
                                            sessionExpired:
                                                !!slotResult.sessionExpired
                                        };
                                    }

                                    // 500 / 524 / timeout / other non-fatal
                                    // failure:
                                    //
                                    // Try the next qualifying date using the
                                    // latest token we still safely possess.
                                    console.log(
                                        `[Polling] Continuing to next ` +
                                        `qualifying OFC date in ${city}.`
                                    );

                                    continue;
                                }

                                const viableSlots =
                                    Array.isArray(
                                        slotResult.slots
                                    )
                                        ? slotResult.slots
                                        : [];

                                const returnedSlotsToken =
                                    String(
                                        slotResult.slotsToken ||
                                        ""
                                    );

                                const returnedSlotsCapturedAt =
                                    Number(
                                        slotResult.slotsCapturedAt ||
                                        Date.now()
                                    );

                                // The Slots response becomes the token
                                // context for the next Slots request.
                                if (returnedSlotsToken) {
                                    rollingToken =
                                        returnedSlotsToken;

                                    rollingTokenCapturedAt =
                                        returnedSlotsCapturedAt;
                                }

                                // -----------------------------------------
                                // CONFIRMED OFC SLOT HIT
                                // -----------------------------------------
                                if (
                                    viableSlots.length > 0 &&
                                    returnedSlotsToken
                                ) {
                                    console.log(
                                        `[Polling] 🎯 OFC Scout SLOT HIT: ` +
                                        `${city} ${matchedDate} | ` +
                                        `${viableSlots.length} viable ` +
                                        `slot(s). Top: ` +
                                        `Time=${viableSlots[0].Time}, ` +
                                        `Available=` +
                                        `${viableSlots[0].EntriesAvailable}, ` +
                                        `Num=${viableSlots[0].Num}. ` +
                                        `Stopping remaining date/city polling.`
                                    );

                                    return {
                                        success: true,
                                        results: results,

                                        earlyMatch: {
                                            city: city,
                                            date: matchedDate,

                                            // Token/context used to make
                                            // THIS successful Slots request.
                                            token:
                                                tokenForThisDate,
                                            primaryId:
                                                primaryId,
                                            appd:
                                                appd,
                                            applications:
                                                scoutApplicationIds,
                                            isReschedule:
                                                isRescheduleUrl,
                                            capturedAt:
                                                tokenForThisDateCapturedAt,

                                            // ALL viable slots + the new
                                            // token returned by Slots.
                                            slots:
                                                viableSlots,
                                            slotsToken:
                                                returnedSlotsToken,
                                            slotsCapturedAt:
                                                returnedSlotsCapturedAt
                                        }
                                    };
                                }

                                console.log(
                                    `[Polling] OFC Scout date ` +
                                    `${city} ${matchedDate} returned ` +
                                    `zero viable slots. ` +
                                    `Trying next qualifying date.`
                                );
                            }

                            // Every qualifying date in this city was
                            // checked and none had a usable slot.
                            console.log(
                                `[Polling] OFC Scout exhausted all ` +
                                `${qualifyingDates.length} qualifying ` +
                                `date(s) in ${city} without a viable slot. ` +
                                `Continuing to next OFC city.`
                            );
                        }                        }
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
            await new Promise(
    r => setTimeout(r, Number(criteria.cityGapMs ?? 1500))
);
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
    try:
        killed_pid = kill_process_by_port(cdp_port)

        if killed_pid:
            log.info(
                f"Killed Chrome PID {killed_pid} "
                f"on CDP port {cdp_port}."
            )
    except Exception as exc:
        log.warning(
            f"Could not kill Chrome on port {cdp_port}: {exc}"
        )

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