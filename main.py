"""
Entry point for the US Visa Appointment Auto-Booker.

Usage:
    python main.py              — Start the orchestrator (all accounts)
    python main.py --no-monitor — Start without the slot monitor
    python gui.py               — Start the GUI dashboard instead
"""

import sys
from pathlib import Path

# Ensure the project root is on the Python path so that
# `from src.xxx import ...` works regardless of cwd.
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.orchestrator import main

if __name__ == "__main__":
    main()
