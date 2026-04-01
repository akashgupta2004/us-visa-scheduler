# ─────────────────────────────────────────────────────────────────────────────
# config.py  –  All settings for the multi-login bot
# ─────────────────────────────────────────────────────────────────────────────

# ── 1. TARGET WEBSITE ─────────────────────────────────────────────────────────
# Replace with the actual login page URL your customers use.
LOGIN_URL = "https://www.usvisascheduling.com/en-US/"


# ── 3. CAPTCHA SOLVER ─────────────────────────────────────────────────────────
FAST_CAPTCHA_API_KEY = "QN92k7Xq5BFjN6_lBPa1zGDPXnw3Rw0Jyt5Vf6-n6hqjDNwVgu3xT97xmb2QMMB5"

# ── 4. FILES & FOLDERS ────────────────────────────────────────────────────────
CREDENTIALS_FILE = "credentials.csv"   # CSV with customer login info
SESSIONS_DIR     = "sessions"          # Where session JSON files are saved
LOG_FILE         = "bot.log"           # Log file

# ── 5. BOT BEHAVIOUR ──────────────────────────────────────────────────────────
HEADLESS    = False   # False = show the browser windows (easier to debug)
MAX_WORKERS = 5       # How many logins run at the same time (keep low to avoid bans)
TIMEOUT_MS  = 30_000  # Max ms to wait for normal page elements (30 seconds)
WAITING_ROOM_TIMEOUT_MS = 300_000  # Max ms to wait in a queue/waiting room (5 minutes)
POST_LOGIN_STAY_OPEN_MS = 60_000   # How long to keep the window open after success (60 seconds)
DELAY_BETWEEN_LOGINS = 1  # Seconds to stagger login starts (reduces bot detection)

# ── 6. PROXY SETTINGS ─────────────────────────────────────────────────────────
# Use rotating residential proxies to completely prevent IP bans.
# Most proxy providers give you a single address that automatically changes your IP.
# Format: "http://<username>:<password>@<proxy-domain>:<port>"
USE_PROXY = False
PROXY_SERVER = "http://username:password@gate.smartproxy.com:7000"
