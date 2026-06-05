import json
import random
import logging
from pathlib import Path
from playwright.async_api import Page
from src.auth.utils import human_delay, human_click

ACCOUNTS_FILE = Path(__file__).parent.parent.parent / "accounts.json"

def load_security_answers(customer: str, log: logging.Logger) -> dict:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        for acc in accounts:
            if acc.get("customer_name") == customer:
                return acc.get("security_questions", {})
    except Exception as e:
        log.error(f"Failed to load security questions for {customer}: {e}")
    return {}


def match_answer(question_text: str, answers: dict) -> str | None:
    q = question_text.lower()
    return next((v for k, v in answers.items() if k.lower() in q), None)


async def handle_security_question(page: Page, customer: str, log: logging.Logger) -> bool:
    await human_delay(1000, 2000)

    q_selectors = [
        "label[for*='SecurityAnswer' i]", "label[for*='security' i]",
        ".security-question", ".question-text", "#securityQuestion", "legend",
        "p:has-text('favourite')", "p:has-text('favorite')",
        "p:has-text('maiden')",    "p:has-text('born')",
        "p:has-text('pet')",       "p:has-text('school')",
        "span:has-text('favourite')", "div.question",
    ]

    log.info("Waiting for security question to appear (up to 15s) …")
    try:
        combined_q = ", ".join(q_selectors)
        await page.wait_for_selector(combined_q, state="visible", timeout=15000)
    except Exception:
        pass

    answers = load_security_answers(customer, log)
    filled_any = False
    
    try:
        log.info("Waiting for security question inputs (up to 15s) …")
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
        
        for i in range(count):
            input_loc = inputs.nth(i)
            if not await input_loc.is_visible():
                continue
                
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
