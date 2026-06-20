# 🇺🇸 US Visa Appointment Auto-Booker

An automated system that monitors US Visa appointment slots and books OFC (Offsite Facilitation Center) appointments instantly when availability is detected.

## 🏗️ Architecture

The system is a **multi-process pipeline** managed by an orchestrator:

```
                        ┌───────────────────────┐
                        │    orchestrator.py     │
                        │  (Process Manager)     │
                        └───┬───────┬───────┬───┘
                            │       │       │
             ┌──────────────┘       │       └──────────────┐
             ▼                      ▼                      ▼
  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
  │  login_runner.py │   │ booking_runner.py │   │ monitor_runner.py│
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
| `orchestrator.py` | Manages all child processes, assigns CDP ports, handles auto-restart on crashes |
| `login_runner.py` | Launches Chrome with remote debugging, logs in, handles CAPTCHA & security questions, keeps session alive |
| `booking_runner.py` | Connects to authenticated Chrome, watches state files for triggers, delegates booking to browser extension |
| `monitor_runner.py` | Polls the CheckVisaSlots API, matches slots against customer criteria, writes triggers and sends Slack alerts |

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

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```env
FASTCAPTCHA_API_KEY=your_api_key
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

### 3. Configure Customer Criteria

Edit `accounts.json` or use `gui.py` to manage your customers and booking criteria.
A customer record should include:

```json
{
  "customer_name": "your_name",
  "username": "your_email_or_username",
  "password": "your_password",
  "ofcCities": ["HYDERABAD"],
  "ofcStartDate": "2026-01-01",
  "ofcEndDate": "2026-12-31",
  "consularCities": ["HYDERABAD"],
  "consularStartDate": "2026-01-01",
  "consularEndDate": "2026-12-31",
  "security_questions": {
    "food": "YourAnswer"
  }
}
```

**Supported cities:** `CHENNAI`, `HYDERABAD`, `MUMBAI`, `DELHI`, `KOLKATA` (or `ANY`)

### 5. Run the System

**Option A — Single command (recommended):**

```bash
python main.py
```

The orchestrator will launch Chrome, log in, start booking runners, and monitor slots for **all accounts** in `accounts.json` automatically.

**Option B — Use the GUI dashboard:**

```bash
python gui.py
```

This provides a unified dashboard with start/stop controls, per-account management, and live log streaming.

## ⚙️ How It Works

1. **`orchestrator.py`** reads `accounts.json` and for each account assigns a unique Chrome CDP port (9222, 9223, …). It spawns `login_runner.py` and, once login succeeds, `booking_runner.py`. It also starts a single `monitor_runner.py` instance.

2. **`login_runner.py`** opens Chrome with `--remote-debugging-port`, navigates to the visa scheduling site, handles Cloudflare waiting rooms, logs in with your credentials, solves CAPTCHAs, and answers security questions. It then keeps the browser session open.

3. **`booking_runner.py`** connects to the same Chrome via CDP, navigates to the portal, and polls `state_<customer>.json` every 0.5 seconds while keeping the session alive with mouse movements. When a trigger is detected, it delegates booking to the browser extension via `postMessage`.

4. **`monitor_runner.py`** polls the CheckVisaSlots API every 15–20 seconds, looking for OFC + Consular date pairs that match your criteria (city and date range). When a match is found, it:
   - Sends a Slack notification
   - Writes a trigger to `state_<customer>.json`

5. **`booking_runner.py` detects the trigger** and immediately sends a message to the browser extension, which:
   - Selects the city from the dropdown
   - Navigates the calendar to the target month
   - Clicks the available date
   - Selects a time slot and submits

## 🔧 Configuration

### Slot Monitor Timing (in `src/monitor_runner.py`)

```python
POLL_MIN_SECONDS = 15       # Min seconds between API polls
POLL_MAX_SECONDS = 20       # Max seconds between API polls
ALERT_COOLDOWN_SECONDS = 900  # 15 min cooldown between duplicate alerts
```

## ⚠️ Disclaimer

This tool is for educational purposes only. Use it responsibly and in accordance with the terms of service of the visa scheduling platform.
