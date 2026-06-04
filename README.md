# 🇺🇸 US Visa Appointment Auto-Booker

An automated system that monitors US Visa appointment slots and books OFC (Offsite Facilitation Center) appointments instantly when availability is detected.

## 🏗️ Architecture

The system is a **three-process pipeline**:

```
┌──────────────┐     trigger.json     ┌──────────────────┐
│  bot.py      │                      │  bot2_ofc_       │
│  (Login &    │◄─── Chrome CDP ─────►│  booking.py      │
│   Session)   │                      │  (Auto-Booker)   │
└──────────────┘                      └──────────────────┘
                                            ▲
                                            │ trigger.json
                                            │
                                      ┌─────┴────────────┐
                                      │  slot_monitor_    │
                                      │  qualified.py     │
                                      │  (Slot Watcher)   │
                                      └──────────────────┘
```

| Component | Purpose |
|---|---|
| `bot.py` | Launches Chrome with remote debugging, logs in, handles CAPTCHA & security questions, keeps session alive |
| `bot2_ofc_booking.py` | Connects to the same Chrome session, parks on the OFC scheduling page, and books instantly when triggered |
| `slot_monitor_qualified.py` | Polls a slot-checking API, matches slots against customer criteria, writes `trigger.json` when a valid slot is found, and sends Slack alerts |

## 📁 File Structure

```
bot/
├── bot.py                          # Login bot (run first)
├── bot2_ofc_booking.py             # OFC booking bot (run second)
├── slot_monitor_qualified (1).py   # Slot monitor (run third)
├── gui.py                          # Tkinter dashboard (optional)
├── show_slots.py                   # Quick slot viewer utility
├── test_trigger.py                 # Test trigger.json generation
├── test_connection.py              # Test Chrome CDP connection
├── .env                            # Credentials (not committed)
├── .env.example                    # Template for .env
├── security_questions.json         # Security question answers
├── slot_notification.csv           # Customer booking criteria
├── requirements.txt                # Python dependencies
└── chrome_profile/                 # Chrome user data (auto-created)
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
VISA_USERNAME=your_username
VISA_PASSWORD=your_password
FASTCAPTCHA_API_KEY=your_api_key
HEADLESS=false
```

### 3. Configure Security Questions

Edit `security_questions.json` with your security question answers:

```json
{
  "favourite food": "YourAnswer1",
  "born": "YourAnswer2",
  "pet": "YourAnswer3"
}
```

### 4. Configure Customer Criteria

Edit `slot_notification.csv` with your booking requirements:

```csv
customer_name,ofc_location,consular_location,need_before,min_slots
your_name,HYDERABAD,HYDERABAD,2026-12-31,1
```

**Supported cities:** `CHENNAI`, `HYDERABAD`, `MUMBAI`, `DELHI`, `KOLKATA` (or `ANY`)

### 5. Run the System

Run these in **three separate terminals**, in order:

```bash
# Terminal 1 — Login & keep session alive
python bot.py

# Terminal 2 — Park on OFC page & wait for trigger
python bot2_ofc_booking.py

# Terminal 3 — Monitor slots & trigger booking
python "slot_monitor_qualified (1).py"
```

### Alternative: Use the GUI

```bash
python gui.py
```

This provides a unified dashboard with start/stop controls and live log streaming.

## ⚙️ How It Works

1. **`bot.py`** opens Chrome with `--remote-debugging-port=9222`, navigates to the visa scheduling site, handles Cloudflare waiting rooms, logs in with your credentials, solves CAPTCHAs, and answers security questions. It then keeps the browser session open.

2. **`bot2_ofc_booking.py`** connects to the same Chrome via CDP, navigates to the OFC scheduling page, and enters a polling loop — checking for `trigger.json` every 0.5 seconds while moving the mouse every 60 seconds to prevent session timeout.

3. **`slot_monitor_qualified.py`** polls the CheckVisaSlots API every 15–20 seconds, looking for OFC + Consular date pairs that match your criteria (city, date range, minimum slots). When a match is found, it:
   - Sends a Slack notification
   - Writes `trigger.json` with the target date and city

4. **Bot2 detects `trigger.json`** and immediately:
   - Selects the city from the dropdown
   - Navigates the calendar to the target month
   - Clicks the available (green) date
   - Waits for time slots to load (up to 60s)
   - Selects the first available time slot
   - Force-enables and clicks Submit
   - Waits for booking confirmation redirect

## 🔧 Configuration

### Bot2 City (in `bot2_ofc_booking.py`)

```python
BOOKING_OFC_CITY = "HYDERABAD"  # Change to your target city
```

### Slot Monitor Timing (in `slot_monitor_qualified.py`)

```python
POLL_MIN_SECONDS = 15       # Min seconds between API polls
POLL_MAX_SECONDS = 20       # Max seconds between API polls
ALERT_COOLDOWN_SECONDS = 900  # 15 min cooldown between duplicate alerts
```

## ⚠️ Disclaimer

This tool is for educational purposes only. Use it responsibly and in accordance with the terms of service of the visa scheduling platform.
