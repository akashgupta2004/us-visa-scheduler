import sys
from pathlib import Path
import time
import json
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Add parent dir to path so src imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.monitor_runner import _write_trigger_if_idle
from src.common.state import read_state, write_state

def test_trigger_gap():
    print("Testing RESERVED_BOOKING trigger gap...")
    state_dir = Path(__file__).resolve().parent
    
    # Load real accounts from accounts.json to pass state.py validation
    try:
        from src.common.config import ACCOUNTS_FILE
        all_accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        # Grab up to 3 RESERVED_BOOKING accounts for testing
        customers = [
            {"username": acc["username"], "role": "RESERVED_BOOKING"}
            for acc in all_accounts if acc.get("role") == "RESERVED_BOOKING"
        ][:3]
        if not customers:
            print("❌ No RESERVED_BOOKING accounts found in accounts.json to test!")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to load accounts.json: {e}")
        sys.exit(1)
    
    # Initialize empty state files
    for c in customers:
        state_file = state_dir / f"state_{c['username']}.json"
        write_state(state_file, {"extension_running": False, "pending": False})
    
    trigger_times = []
    
    # Simulate monitor loop finding slots for all 3 customers
    for c in customers:
        state_file = state_dir / f"state_{c['username']}.json"
        bot_state = read_state(state_file)
        
        start_t = time.time()
        _write_trigger_if_idle(
            state_file=state_file,
            bot_state=bot_state,
            customer_name=c["username"],
            trigger_updates={"pending": True, "action_type": "SNIPER"},
            current_triggers=0,
            max_triggers=3,
            role=c["role"]
        )
        end_t = time.time()
        trigger_times.append(end_t)
        print(f"Triggered {c['username']} at {end_t:.3f}")
        
    # Verify gap
    gaps = []
    for i in range(1, len(trigger_times)):
        gap = trigger_times[i] - trigger_times[i-1]
        gaps.append(gap)
        print(f"Gap between {customers[i-1]['username']} and {customers[i]['username']}: {gap:.3f} seconds")
        
    for gap in gaps:
        if gap < 1.0:
            print(f"❌ TEST FAILED: Gap {gap:.3f} is less than 1.0 second!")
            sys.exit(1)
            
    print("✅ TEST PASSED: All RESERVED_BOOKING accounts triggered with >= 1.0 second gap.")

    # Cleanup
    for c in customers:
        state_file = state_dir / f"state_{c['username']}.json"
        try:
            os.remove(state_file)
            lock_file = state_file.with_suffix(".lock")
            if lock_file.exists():
                os.remove(lock_file)
        except:
            pass

if __name__ == "__main__":
    test_trigger_gap()
