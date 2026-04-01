# Multi-Login Session Bot

A Python bot using [Playwright](https://playwright.dev/python/) to log into multiple customer accounts concurrently and save each session independently.

---

## 📁 Project Structure

```
bot/
├── main.py            # Run this to start the bot
├── bot.py             # Core login + session-save logic
├── config.py          # ← Edit your URL, selectors & settings here
├── credentials.csv    # ← Put customer usernames & passwords here
├── requirements.txt   # Python dependencies
├── sessions/          # Auto-created – stores session JSON files
└── bot.log            # Auto-created – log of all runs
```

---

## ⚙️ Setup (Run Once)

```bash
# 1. Install Python libraries
pip install -r requirements.txt

# 2. Install Playwright's browser binaries
python -m playwright install chromium
```

---

## 🛠️ Configuration (Before First Run)

### 1. Set your login URL
Open `config.py` and replace the placeholder URL:
```python
LOGIN_URL = "https://YOUR_ACTUAL_LOGIN_URL_HERE"
```

### 2. Set your CSS selectors
Inspect the login page (Right-click → Inspect in browser) and update:
```python
SELECTORS = {
    "username_field":     "input[name='username']",   # username input
    "password_field":     "input[name='password']",   # password input
    "submit_button":      "button[type='submit']",     # login button
    "post_login_element": ".dashboard",                # element visible AFTER login
}
```

### 3. Add customer credentials
Edit `credentials.csv`:
```
customer_id,username,password
customer_001,alice@site.com,password123
customer_002,bob@site.com,securepass!
```

---

## ▶️ Running the Bot

```bash
python main.py
```

The bot will:
1. Read all customers from `credentials.csv`
2. Log in concurrently (up to `MAX_WORKERS` at a time)
3. Save each session to `sessions/<customer_id>_session.json`
4. Print a summary report

---

## ♻️ Reusing a Saved Session (No Re-Login)

```python
from playwright.async_api import async_playwright
from bot import open_existing_session

async def demo():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx  = await open_existing_session(browser, "customer_001")
        page = await ctx.new_page()
        await page.goto("https://example.com/dashboard")
        # ... do whatever you need ...
        await ctx.close()
        await browser.close()
```

---

## ⚙️ Tuning Options (in `config.py`)

| Setting | Default | Description |
|---|---|---|
| `HEADLESS` | `False` | `True` = invisible browser (faster) |
| `MAX_WORKERS` | `5` | Logins running simultaneously |
| `DELAY_BETWEEN_LOGINS` | `1` sec | Stagger logins to avoid bans |
| `TIMEOUT_MS` | `30000` ms | Max wait for any page element |
