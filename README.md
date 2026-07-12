# 🇺🇸 US Visa Appointment Auto-Booker

An automated system that monitors US Visa appointment slots and books OFC (Offsite Facilitation Center) and Consular appointments instantly when availability is detected.

## 🏗️ Architecture

The system is a **multi-process pipeline** managed by an orchestrator, working alongside a side-loaded Chrome extension to bypass bot detection and automate the booking process securely.

```
                        ┌───────────────────────┐
                        │    orchestrator.py    │
                        │  (Process Manager)    │
                        └───┬───────┬───────┬───┘
                            │       │       │
             ┌──────────────┘       │       └──────────────┐
             ▼                      ▼                      ▼
  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
  │  login_runner.py │   │ booking_runner.py│   │ monitor_runner.py│
  │  (Chrome Login)  │   │  (Auto-Booker)   │   │  (Slot Watcher)  │
  └──────────────────┘   └──────────────────┘   └──────────────────┘
         │                       ▲                      │
         │ Chrome CDP            │ state_*.json         │ API poll
         ▼                       │                      ▼
  ┌──────────────┐        ┌──────┴─────────┐    ┌──────────────────┐
  │   Chrome     │        │ Browser        │    │ CheckVisaSlots   │
  │  (per acct)  │───────►│ Extension      │    │ API              │
  └──────────────┘        └────────────────┘    └──────────────────┘
```

| Component | Purpose |
|---|---|
| `orchestrator.py` | Manages all child processes, assigns unique CDP ports to Chrome instances, and handles auto-restarts on crashes. |
| `login_runner.py` | Launches Playwright's Chromium browser with a custom side-loaded extension, logs in, handles CAPTCHA (FastCaptcha) & security questions, and keeps the session alive. |
| `booking_runner.py` | Connects to the authenticated Chrome instance, watches local state files for triggers, and delegates booking to the browser extension. |
| `monitor_runner.py` | Polls the CheckVisaSlots API, matches available slots against customer criteria, writes triggers to state files, and sends Slack alerts. |
| `Browser Extension` | Built-in Chrome MV3 extension (`extension-build/`) injected automatically by Playwright to safely execute DOM interactions inside the page context without being flagged as an automated bot. |

## 📁 File Structure

```
bot/
├── main.py                         # Entry point (starts orchestrator)
├── gui.py                          # Tkinter dashboard (optional)
├── slack.py                        # Slack notification integration
├── accounts.json                   # Customer booking criteria & credentials (not committed)
├── .env                            # API keys & secrets (not committed)
├── .env.example                    # Template for .env
├── requirements.txt                # Python dependencies
├── extension-build/                # Pre-built Chrome MV3 Extension
│   └── chrome-mv3-prod/            # Production extension loaded by browser.py
├── src/
│   ├── orchestrator.py             # Multi-account process manager
│   ├── login_runner.py             # Chrome login & session keeper
│   ├── booking_runner.py           # Trigger watcher & booking executor
│   ├── monitor_runner.py           # Slot polling & analytics
│   ├── common/                     # Shared utilities
│   │   ├── utils.py                # safe_id() and other helpers
│   │   ├── config.py               # Paths, constants, account loading
│   │   └── state.py                # Thread-safe state file I/O
│   ├── auth/                       # Authentication modules
│   │   ├── browser.py              # Chrome launch & CDP connection
│   │   ├── login.py                # Login flow & waiting room
│   │   ├── captcha.py              # CAPTCHA solving (FastCaptcha)
│   │   ├── security.py             # Security question handling
│   │   └── utils.py                # Human-like delay/click/type
│   ├── booking/                    # Booking modules
│   │   ├── cdp_client.py           # Portal navigation
│   │   └── executor.py             # Extension trigger & result handling
│   └── monitor/                    # Monitoring modules
│       ├── api.py                  # CheckVisaSlots API client
│       ├── matcher.py              # Slot matching & date logic
│       └── notifier.py             # CSV/JSONL analytics logging
└── chrome_profile_*/               # Chrome user data (auto-created, not committed)
```

## 🚀 Setup & Installation

Follow these steps to get the system running on your local machine:

### 1. Prerequisites
- **Python 3.10+**: Make sure Python is installed and added to your system PATH.
- **Git** (optional, to clone the repository).

### 2. Set up a Virtual Environment (Recommended)
It is highly recommended to use a virtual environment to manage dependencies.
```bash
# Create a virtual environment
python -m venv .venv

# Activate the virtual environment (Windows)
.venv\Scripts\activate

# Activate the virtual environment (macOS/Linux)
source .venv/bin/activate
```

### 3. Install Dependencies
Install all required Python packages and the Playwright Chromium browser. Note: Standard Google Chrome is not used as it restricts side-loading extensions; Playwright's bundled Chromium is required.
```bash
# Install Python packages
pip install -r requirements.txt

# Install Playwright's custom Chromium browser
playwright install chromium
```

### 4. Configure Environment Variables
Copy the template environment file and fill in your secrets:
```bash
# Copy the template
cp .env.example .env
```
Open `.env` and configure:
- `FASTCAPTCHA_API_KEY`: Get a free API key at [FastCaptcha](https://fastcaptcha.org/accounts/signup/) to automatically solve CAPTCHAs during login.
- `SLACK_WEBHOOK_URL` (Optional): Setup an incoming webhook on Slack to get notified when slots are found.

### 5. Configure Customer Accounts
The easiest way to configure accounts and criteria is to use the provided GUI dashboard.
```bash
python gui.py
```
From the GUI:
1. Go to the **Accounts Manager** tab.
2. Click **Add New Account**.
3. Fill in the credentials, target cities, date ranges, and security questions.
4. Click **Save Changes**. This will automatically generate or update your `accounts.json` file.

Alternatively, you can manually create an `accounts.json` file in the root directory (refer to the "Manual Configuration" section below).

## 🎮 Running the System

You have two options to run the orchestrator:

### Option A: Using the GUI Dashboard (Recommended)
The GUI provides a unified dashboard with start/stop controls, per-account management, and a live log streaming interface.
```bash
python gui.py
```

**Step-by-Step GUI Guide:**

1. **Adding an Account (Accounts Manager Tab)**
   - Open the **Accounts Manager** tab.
   - Click the **Add New Account** button on the left sidebar.
   - Fill in your **Customer Name**, **Username/Email**, and **Password**.
   - Select your **Account Mode** (e.g., `Full Booking (OFC + Consular)`, `Full Reschedule`, etc.).
   - Choose your **Target Cities** for both OFC and Consular by clicking the pill buttons. You can use the "Keep Consular Location & Dates identical to OFC" checkbox for convenience.
   - Set the **Start Date** and **End Date** for acceptable slots using the calendar dropdowns.
   - Add your **Security Questions** (keyword and answer pairs).
   - Click **Save Changes** at the bottom. Your account will appear in the left listbox.

2. **Managing Accounts**
   - Click on any account in the left sidebar to load its configuration into the form.
   - Make edits and click **Save Changes**, or click **Delete** to remove the account permanently.

3. **Running the Bots (Orchestrator Control Tab)**
   - Switch to the **Orchestrator Control** tab.
   - Use the checkboxes to optionally disable the **Slot Monitor** or **MongoDB Logging**.
   - Click the green **▶ Start All Bots** button to launch the orchestrator.
   - The bots will launch in the background. The **Active Accounts** list will populate, allowing you to selectively **START** or **STOP** individual accounts on the fly without stopping the entire system.
   - Watch the **Live Output** console at the bottom for real-time logs. Check the **Auto-scroll** box to follow the latest updates.
   - When finished, click the red **⏹ Stop All** button to gracefully shut down Chrome and all background processes.

### Option B: Using the CLI
If you prefer running headlessly or deploying on a server, run the orchestrator directly from the command line:
```bash
# Run for all accounts in accounts.json
python main.py

# Run without the slot monitor (if you want to trigger bookings manually)
python main.py --no-monitor
```

### Option C: Running on Two Different PCs (Advanced Setup)

To optimize performance and avoid rate limits, you can split the bot's workload across two different computers:
1. **Polling Laptop:** Dedicated to polling for available slots and running `POLLING_ONLY` accounts.
2. **Booking Laptop:** Dedicated to receiving slot triggers and executing the booking for `RESERVED_BOOKING` accounts.

**Prerequisites:**
- Both computers must have the same Python environment and codebase.
- Both computers need access to the same synchronized folder (e.g., via OneDrive, Dropbox, or a shared network drive) so that `state_{uid}.json` files can be updated by the Polling Laptop and instantly read by the Booking Laptop.

**Step 1: Configure the Polling Laptop (PC 1)**
1. Open the `.env` file on PC 1.
2. Set the laptop role to `POLLING`:
   ```env
   LAPTOP_ROLE=POLLING
   ```
3. Run the orchestrator: `python main.py`
   *(It will run `POLLING_ONLY` accounts and the slot monitor.)*

**Step 2: Configure the Booking Laptop (PC 2)**
1. Open the `.env` file on PC 2.
2. Set the laptop role to `BOOKING`:
   ```env
   LAPTOP_ROLE=BOOKING
   ```
3. Run the orchestrator: `python main.py`
   *(It will run `RESERVED_BOOKING` accounts and continuously watch state files for triggers.)*


## ⚙️ How It Works

1. **Initialization:** The orchestrator reads `accounts.json` and assigns a unique Chrome CDP port (e.g., 9222, 9223) to each account.
2. **Login & Session Keeper:** It spawns `login_runner.py` which opens Playwright's Chromium browser, side-loads the custom Chrome extension, navigates the Cloudflare waiting room, logs in, solves CAPTCHAs via FastCaptcha, and answers security questions. The browser remains open to keep the session alive.
3. **Standby Booking Watcher:** Once logged in, `booking_runner.py` connects to the exact same Chrome instance via CDP. It parks on the scheduling portal and polls `state_<customer>.json` every 0.5 seconds for action triggers.
4. **Slot Monitoring:** Concurrently, `monitor_runner.py` polls the CheckVisaSlots API every 15-20 seconds in the background. It looks for OFC + Consular date pairs that match the customer's strict criteria.
5. **Execution:** When the monitor finds a matching slot, it:
   - Sends a Slack notification.
   - Writes a trigger payload into `state_<customer>.json`.
6. **Auto-Booking:** `booking_runner.py` detects the file change and delegates the booking instruction to the side-loaded browser extension via `postMessage`. The extension then securely takes over and completes the booking process.

## 🔄 Booking Flows

The system supports several dynamic action modes depending on your current appointment status. The orchestrator triggers these flows based on the `action_mode` set in your account configuration:

1. **Full Booking (`SNIPER`)** 
   - **Use Case:** For users who do not have any existing appointments.
   - **Behavior:** The bot automatically books the OFC (biometrics) appointment and then seamlessly proceeds to book the Consular interview back-to-back.
2. **Full Reschedule (`RESCHEDULE_FULL`)**
   - **Use Case:** For users who already have both OFC and Consular appointments booked but want to find earlier/better dates.
   - **Behavior:** Identical to the `SNIPER` flow, but explicitly targets rescheduling to overwrite your existing appointments.
3. **Consular Reschedule Only (`RESCHEDULE_CONSULAR`)**
   - **Use Case:** For users who have already attended their OFC appointment (or want to keep it) and strictly want to reschedule their Consular interview to an earlier date.
   - **Behavior:** Bypasses the OFC selection entirely and only monitors/books the Consular calendar.
4. **Consular Wait Mode (Fallback Flow)**
   - **Use Case:** Handled automatically if the system successfully secures an OFC slot but the corresponding Consular slot gets snatched by someone else before the bot can secure it.
   - **Behavior:** Instead of failing, the bot enters a persistent "Wait Mode". It keeps the browser session alive by performing mouse movements every 30 seconds to prevent idle timeouts. 
   - **Timings:** While in Wait Mode, the bot triggers a direct Consular booking attempt (`SNIPER_CONSULAR_ONLY` or `RESCHEDULE_FULL_CONSULAR_ONLY`) at randomized intervals between **180 and 240 seconds (3 to 4 minutes)**. It continuously polls the calendar this way to strictly find a Consular date that matches the secured OFC date until it successfully finishes the booking. If the server drops the held OFC appointment due to a hard session expiry, it abandons Wait Mode and restarts the full flow.

## ⏱️ System Timings & Thresholds

The system is configured with several delays, timeouts, and polling intervals to mimic human behavior and comply with API limits:

- **CheckVisaSlots API Polling (`monitor_runner.py`)**: Randomly polls between **5 to 15 seconds** (`POLL_MIN_SECONDS` / `POLL_MAX_SECONDS`).
- **Slack Alert Cooldown**: Identical duplicate alerts are suppressed for **15 minutes** (`ALERT_COOLDOWN_SECONDS`) to avoid spamming.
- **API Error Backoff**: If the slot polling API throws an error, the monitor backs off for **40 seconds** (`ERROR_BACKOFF_SECONDS`) before retrying.
- **State File Polling (`booking_runner.py`)**: The booking runner checks the local `state_<customer>.json` file every **0.5 seconds** (`POLL_INTERVAL`) for near-instant trigger response.
- **Waiting Room Timeout (`auth/login.py`)**: The bot will wait up to **120 minutes** if placed in a Cloudflare waiting room queue.
- **Login Timeout (`orchestrator.py`)**: The orchestrator allows up to **10 minutes** for the login runner to successfully authenticate before giving up.
- **Rate Limit (429) Handling**: If the booking portal throws a "429 Too Many Requests" error, the orchestrator automatically puts that account on cooldown and restarts it after **45 minutes**.
- **Crash Loop Prevention**: If any bot crashes more than 3 times within **5 minutes** (300 seconds), the orchestrator pauses for **60 seconds** before attempting another restart.

## 📝 Manual Configuration (Advanced)


If you prefer not to use the GUI, create an `accounts.json` file manually in the root folder with the following schema:
```json
[
  {
    "customer_name": "John Doe",
    "username": "your_email@example.com",
    "password": "your_password",
    "action_mode": "SNIPER",
    "ofcCities": ["HYDERABAD", "CHENNAI"],
    "ofcStartDate": "2026-01-01",
    "ofcEndDate": "2026-12-31",
    "consularCities": ["HYDERABAD", "CHENNAI"],
    "consularStartDate": "2026-01-01",
    "consularEndDate": "2026-12-31",
    "security_questions": {
      "food": "Pizza",
      "city": "New York"
    },
    "prevent_immediate": true,
    "multiPerson": false
  }
]
```

### Field Explanations:
- `action_mode`: Defines the booking flow (`SNIPER`, `RESCHEDULE_FULL`, or `RESCHEDULE_CONSULAR`).
- `ofcCities` & `consularCities`: Arrays of cities you want to target (e.g., `"HYDERABAD"`, `"CHENNAI"`, `"MUMBAI"`, `"DELHI"`, `"KOLKATA"`).
- `ofcStartDate` / `ofcEndDate`: The date range for acceptable OFC (biometrics) slots (Format: `YYYY-MM-DD`).
- `consularStartDate` / `consularEndDate`: The date range for acceptable Consular interview slots (Format: `YYYY-MM-DD`).
- `security_questions`: Key-value pairs matching a substring of the question and its answer. For example, if the question asks for your favorite food, use `"food": "Pizza"`.
- `prevent_immediate`: `true` or `false`. If true, the bot dynamically ignores any slots available within the next 3 days from the current date (useful if you cannot arrange travel on short notice).
- `multiPerson`: `true` or `false`. Set to true if the account has dependents/family members attached so the bot books for the whole group.

## ⚠️ Disclaimer

This tool is for educational and research purposes only. Use it responsibly and in accordance with the terms of service of the visa scheduling platform. The developers are not responsible for any bans or issues arising from the use of this software.
