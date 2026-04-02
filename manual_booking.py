import os
import sys
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from config import LOGIN_URL

def open_manual_session(customer_id):
    session_path = os.path.join("sessions", f"{customer_id}_session.json")
    
    if not os.path.exists(session_path):
        print(f"❌ Error: Could not find a saved session for customer '{customer_id}'")
        print(f"Make sure '{session_path}' exists before running this.")
        sys.exit(1)
        
    print(f"✅ Found saved session for '{customer_id}'")
    print(f"🚀 Launching browser directly into the dashboard...")

    # Use the Stealth wrapper natively, just like we did in main.py
    with Stealth().use_sync(sync_playwright()) as p:
        # Launch browser in non-headless mode so a human can see and interact with it
        browser = p.chromium.launch(headless=False)
        
        # Create a context injected with the saved cookies from yesterday's login
        context = browser.new_context(
            storage_state=session_path,
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
        
        page = context.new_page()
        print("🌐 Navigating to US Visa Scheduling Dashboard...")
        
        # Go directly to the front door, the cookies will bypass the Microsoft login entirely
        page.goto(LOGIN_URL)
        
        print("\n=======================================================")
        print("🎉 You are in! The window will stay open.")
        print("Simply use the browser window normally to book appointments.")
        print("If the Playwright Inspector pops up, just click 'Resume'.")
        print("=======================================================\n")
        
        # Keep the window open indefinitely while the manual team books the appointment
        page.pause()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage: python manual_booking.py <customer_id>")
        print("Example: python manual_booking.py 001\n")
        sys.exit(1)
        
    customer_id = sys.argv[1]
    open_manual_session(customer_id)
