import json
import random
import logging
from pathlib import Path
from playwright.async_api import Page
from src.auth.utils import human_delay, human_click
from src.common.config import ACCOUNTS_FILE

def load_security_answers(username: str, log: logging.Logger) -> dict:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        for acc in accounts:
            if acc.get("username") == username:
                return acc.get("security_questions", {})
    except Exception as e:
        log.error(f"Failed to load security questions for username '{username}': {e}")
    return {}


def match_answer(question_text: str, answers: dict) -> str | None:
    q = question_text.lower()
    return next((v for k, v in answers.items() if k.lower() in q), None)


async def handle_security_question(page: Page, username: str, log: logging.Logger) -> bool:
    await human_delay(1000, 2000)

    log.info("Waiting for security question inputs (up to 15s) …")

    answers = load_security_answers(username, log)
    filled_any = False
    
    try:
        await page.wait_for_selector("input:not([type='hidden']):not([type='submit']):not([type='button']):not([readonly]):not([disabled])", state="visible", timeout=15000)
    except Exception:
        pass
        
    try:
        # Find all inputs that are editable (not hidden, disabled, or readonly) and are not buttons/checkboxes
        inputs = page.locator("input:not([type='hidden']):not([type='submit']):not([type='button']):not([readonly]):not([disabled])")
        count = await inputs.count()
        
        # Pull the entire text of the document in visual order
        body_text = await page.inner_text("body")
        questions = []
        
        for line in body_text.split('\n'):
            line = line.strip()
            if len(line) < 5: continue
            
            # A line is considered a question if it has a '?' and contains any key from our provided answers
            is_question = "?" in line and any(k.lower() in line.lower() for k in answers.keys())
            
            if is_question and line not in questions:
                questions.append(line)
        
        # Get all visible inputs
        visible_inputs = []
        for i in range(count):
            loc = inputs.nth(i)
            if await loc.is_visible():
                visible_inputs.append(loc)
                
        # If there are extra visible inputs (like a search bar), align from the end
        if len(visible_inputs) > len(questions) and len(questions) > 0:
            visible_inputs = visible_inputs[-len(questions):]
            
        for i, input_loc in enumerate(visible_inputs):
            if i < len(questions):
                textToMatch = questions[i]
                answer = match_answer(textToMatch, answers)
                if not answer:
                    log.error(f"No answer found mapped for question: '{textToMatch}'")
                    continue
                    
                await input_loc.fill("")
                await human_delay(100, 200)
                await input_loc.type(answer, delay=random.randint(50, 150))
                log.info(f"Typed answer for: '{textToMatch}'")
                filled_any = True

    except Exception as e:
        log.error(f"Error handling security inputs: {e}")

    if not filled_any:
        log.info("No security questions filled. Either none present or couldn't parse.")
        return True

    await human_delay(300, 600)

    for sel in ["button[type='submit']", "input[type='submit']",
                "button:has-text('Continue')", "button:has-text('Submit')",
                "input[value='Continue']"]:
        if await page.locator(sel).count() > 0:
            await human_click(page, sel)
            log.info("Security question submitted.")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            return True

    log.error("Submit button not found.")
    return False
