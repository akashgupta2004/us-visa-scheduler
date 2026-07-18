import os
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path
from playwright.async_api import Page, BrowserContext

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def ensure_chrome_debug_running(cdp_port: int, profile_dir: str, log: logging.Logger) -> None:
    """
    Start Chrome with remote debugging on the given port if not already running.
    Each account gets its own profile directory and port so sessions are isolated.
    """
    def port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    if port_open(cdp_port):
        log.info(f"Chrome debug port {cdp_port} already active — connecting.")
        return

    log.info(f"Starting Chrome with --remote-debugging-port={cdp_port} …")

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    
    # Delete the Sessions directory to prevent Chrome from restoring previous tabs.
    # We want a fresh 'about:blank' window each time.
    import shutil
    import json
    
    default_dir = Path(profile_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    
    sessions_dir = default_dir / "Sessions"
    if sessions_dir.exists():
        try:
            shutil.rmtree(sessions_dir)
        except Exception as e:
            log.warning(f"Failed to clear Sessions dir: {e}")

    # Disable password saving prompt via Preferences file
    prefs_file = default_dir / "Preferences"
    try:
        if prefs_file.exists():
            with open(prefs_file, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        else:
            prefs = {}
            
        prefs["credentials_enable_service"] = False
        if "profile" not in prefs:
            prefs["profile"] = {}
        prefs["profile"]["password_manager_enabled"] = False
        
        with open(prefs_file, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception as e:
        log.warning(f"Could not disable password manager in preferences: {e}")

    chrome_exe = CHROME_EXE
    # First, try to find Playwright's bundled Chromium because standard Chrome
    # no longer supports the --load-extension command line flag.
    import glob
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        pattern = os.path.join(local_app_data, "ms-playwright", "chromium-*", "chrome-win*", "chrome.exe")
        matches = glob.glob(pattern)
        if matches:
            matches.sort(reverse=True)
            chrome_exe = matches[0]

    if not os.path.isfile(chrome_exe) or "ms-playwright" not in chrome_exe:
        log.error(
            "Playwright Chromium not found. The standard Google Chrome browser no longer supports "
            "side-loading extensions via command line.\n"
            "Please install Playwright's bundled Chromium by running:\n"
            "  playwright install chromium\n"
            "Then run this bot again."
        )
        sys.exit(1)

    # Load the production build of the extension from the local extension-build folder.
    extension_path = str((Path(__file__).parent.parent.parent / "extension-build" / "chrome-mv3-prod").resolve())
    log.info(f"Using extension from: {extension_path}")

    subprocess.Popen([
        chrome_exe,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
        "about:blank",
    ])

    # Wait up to 15 s for port to open
    for _ in range(30):
        time.sleep(0.5)
        if port_open(cdp_port):
            log.info("Chrome debug port ready.")
            return

    log.error(f"Chrome debug port {cdp_port} did not open in time.")
    sys.exit(1)


async def connect_to_chrome(playwright, cdp_port: int, log: logging.Logger, handle_dialogs: bool = False):
    """Connect Playwright to a running Chrome via CDP."""
    log.info(f"Connecting to Chrome on ws://127.0.0.1:{cdp_port} …")

    browser = await playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{cdp_port}"
    )

    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Use existing page or open new one
    page = None
    if context.pages:
        for p in context.pages:
            try:
                if "usvisascheduling.com" in p.url.lower():
                    page = p
                    break
            except Exception:
                pass
        if not page:
            page = context.pages[-1]
            
        # Surgical Fix: Close all other about:blank tabs
        for p in context.pages:
            try:
                if p != page and p.url == "about:blank":
                    await p.close()
            except Exception:
                pass
    else:
        page = await context.new_page()

    if handle_dialogs:
        async def handle_dialog(dialog):
            try:
                await dialog.accept()
            except Exception:
                pass
        page.on("dialog", handle_dialog)

    log.info(f"Connected — current page: {page.url}")
    return browser, context, page
