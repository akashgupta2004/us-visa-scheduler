import json
import logging
from pathlib import Path

def read_trigger(trigger_file: Path, log: logging.Logger) -> dict | None:
    if not trigger_file.exists():
        return None
    try:
        data = json.loads(trigger_file.read_text(encoding="utf-8"))
        trigger_file.unlink(missing_ok=True)
        return data
    except Exception as e:
        log.warning(f"Could not read {trigger_file.name}: {e}")
        return None


def delete_trigger(trigger_file: Path):
    try:
        trigger_file.unlink(missing_ok=True)
    except Exception:
        pass
