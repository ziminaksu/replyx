"""
utils/ai_navigator.py — Gemini vision to find elements on any page.
No hardcoded selectors — works even when TUM updates their UI.
"""
import base64, json, re, time, os

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

_cache: dict[str, str] = {}

def ai_do(page, goal: str, max_steps: int = 5):
    """
    Keep taking screenshots and asking Gemini what to do next
    until the goal is reached (a form is visible) or max_steps hit.
    """
    for step in range(max_steps):
        screenshot_b64 = _screenshot(page)
        html = page.evaluate("() => document.body.innerHTML")[:6000]

        prompt = f"""You are automating a browser with Playwright.
Current URL: {page.url}
GOAL: {goal}

HTML snippet:
{html}

What is the single next action to take to reach the goal?
Return JSON:
  {{"action": "click", "selector": "CSS", "reason": "why"}}
  {{"action": "goto", "url": "URL", "reason": "why"}}
  {{"action": "done", "reason": "form is now visible, ready to fill"}}

Return ONLY the JSON object."""

        raw = _call_gemini(prompt, screenshot_b64)
        clean = re.sub(r"```json|```", "", raw).strip()
        step_data = json.loads(clean)

        print(f"[ai_nav] step {step+1}: {step_data.get('action')} — {step_data.get('reason', '')}")

        if step_data["action"] == "done":
            return
        elif step_data["action"] == "click":
            page.click(step_data["selector"])
            page.wait_for_load_state("networkidle")
        elif step_data["action"] == "goto":
            page.goto(step_data["url"], wait_until="networkidle")

    print("[ai_nav] ⚠ Reached max steps — proceeding anyway")

def find_selector(page, task: str, retries: int = 2) -> str:
    cache_key = f"{_url_stem(page.url)}::{task}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        if page.query_selector(cached):
            print(f"[ai_nav] cache hit: {cached}")
            return cached

    for attempt in range(retries + 1):
        screenshot_b64 = _screenshot(page)
        html = page.evaluate("() => document.body.innerHTML")[:6000]

        prompt = f"""You are helping automate a web browser with Playwright.

TASK: Find the CSS selector for: {task}

HTML snippet:
{html}

Rules:
- Return ONLY a CSS selector, nothing else
- Prefer: #id > input[name='x'] > .specific-class
- For TinyMCE/rich text editors: return the [contenteditable] div
- If not found: return exactly NOT_FOUND"""

        result = _call_gemini(prompt, screenshot_b64).strip()

        if result and result != "NOT_FOUND":
            if page.query_selector(result):
                _cache[cache_key] = result
                print(f"[ai_nav] '{task}' → {result}")
                return result
            else:
                print(f"[ai_nav] attempt {attempt+1}: '{result}' matched nothing")
        else:
            print(f"[ai_nav] attempt {attempt+1}: NOT_FOUND for '{task}'")

        time.sleep(1)

    raise RuntimeError(f"[ai_nav] Could not locate: '{task}' on {page.url}")


def ai_fill(page, task: str, value: str):
    sel = find_selector(page, task)
    el = page.query_selector(sel)
    if el.get_attribute("contenteditable"):
        el.click()
        page.keyboard.press("Meta+a")
        page.keyboard.type(value)
    else:
        page.fill(sel, value)
    print(f"[ai_nav] filled '{task}'")


def ai_click(page, task: str):
    sel = find_selector(page, task)
    page.click(sel)
    page.wait_for_load_state("networkidle")
    print(f"[ai_nav] clicked '{task}'")


def ai_pick_link(page, task: str) -> str:
    """Ask Gemini to pick the best href on the page for a given goal."""
    screenshot_b64 = _screenshot(page)
    html = page.evaluate("() => document.body.innerHTML")[:6000]

    prompt = f"""You are helping automate a web browser.

TASK: {task}

HTML snippet:
{html}

Find the most relevant link (href) on this page for the task above.
Return ONLY the full URL or path, nothing else."""

    return _call_gemini(prompt, screenshot_b64).strip()


# ── internals ─────────────────────────────────────────────────────────────────

def _screenshot(page) -> str:
    return base64.standard_b64encode(page.screenshot()).decode()

def _url_stem(url: str) -> str:
    return re.sub(r"\?.*", "", url)

def _call_gemini(prompt: str, screenshot_b64: str) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    for attempt in range(3):
        resp = __import__("requests").post(
            f"{GEMINI_API}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": screenshot_b64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0},
            }
        )
        if resp.status_code in (429, 503):
            wait = 15 * (attempt + 1)
            print(f"[ai_nav] rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError("[ai_nav] Gemini unavailable after 3 attempts")
