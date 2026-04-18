"""
utils/ai_navigator.py — Gemini vision navigator for Playwright.
Fixes:
  - uses page.locator() API correctly for both CSS and xpath
  - filters to first visible element properly
  - tells Gemini to use short CSS selectors (not full xpaths)
  - raises token limit to avoid truncated JSON
"""
import base64, json, re, time, os, ast

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

_cache: dict[str, str] = {}


def ai_do(page, goal: str, max_steps: int = 8):
    """
    Loop: screenshot → ask Gemini what to do → execute → repeat.
    Stops when Gemini says "done" or max_steps reached.
    """
    for step in range(max_steps):
        screenshot_b64 = _screenshot(page)
        html = page.evaluate("() => document.body.innerHTML")[:5000]

        prompt = f"""You are automating a browser with Playwright (Python).
Current URL: {page.url}
GOAL: {goal}

HTML snippet (first 5000 chars):
{html}

What is the single next action to reach the goal?

Return ONLY a JSON object — no markdown, no explanation, no extra text.
Use SHORT CSS selectors (id, class, name attribute). Never use full xpath paths.

Formats:
  {{"action": "click", "selector": "CSS_SELECTOR", "reason": "short reason"}}
  {{"action": "goto", "url": "FULL_URL", "reason": "short reason"}}
  {{"action": "done", "reason": "goal reached, form is visible"}}

IMPORTANT: selector must be a valid CSS selector under 100 characters. No xpath."""

        raw = _call_gemini(prompt, screenshot_b64)
        step_data = _parse_json(raw)
        if step_data is None:
            print(f"[ai_nav] Could not parse Gemini response, skipping step")
            continue

        action = step_data.get("action")
        reason = step_data.get("reason", "")
        print(f"[ai_nav] step {step+1}: {action} — {reason}")

        if action == "done":
            return

        elif action == "click":
            selector = step_data["selector"]
            _safe_click(page, selector)
            page.wait_for_load_state("networkidle", timeout=10_000)

        elif action == "goto":
            page.goto(step_data["url"], wait_until="networkidle")

    print("[ai_nav] ⚠ Reached max steps — proceeding anyway")


def _safe_click(page, selector: str):
    """Click the first VISIBLE element matching selector. Handles xpath= prefix."""
    try:
        loc = page.locator(selector)
        count = loc.count()
        if count == 0:
            print(f"[ai_nav] ⚠ No elements found for: {selector}")
            return
        # Find first visible one
        for i in range(min(count, 5)):
            nth = loc.nth(i)
            if nth.is_visible():
                nth.click(timeout=8_000)
                return
        # Fallback: force-click first
        print(f"[ai_nav] ⚠ No visible match — force clicking first element")
        loc.first.click(force=True, timeout=5_000)
    except Exception as e:
        print(f"[ai_nav] ⚠ Click failed for '{selector}': {e}")


def find_selector(page, task: str, retries: int = 2) -> str:
    """Ask Gemini to find the CSS selector for an element described in plain English."""
    cache_key = f"{_url_stem(page.url)}::{task}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        if page.query_selector(cached):
            print(f"[ai_nav] cache hit: {cached}")
            return cached

    for attempt in range(retries + 1):
        screenshot_b64 = _screenshot(page)
        html = page.evaluate("() => document.body.innerHTML")[:5000]

        prompt = f"""You are helping automate a web browser with Playwright.

TASK: Find the CSS selector for: {task}

HTML snippet:
{html}

Rules:
- Return ONLY a CSS selector string, nothing else, no markdown
- Prefer: #id > input[name='x'] > .specific-class > button[type='submit']
- For TinyMCE/rich text editors: return div.mce-content-body or [contenteditable]
- Keep selector SHORT (under 80 chars)
- If not found: return exactly NOT_FOUND"""

        result = _call_gemini(prompt, screenshot_b64).strip()
        # strip any accidental markdown
        result = re.sub(r"```.*?```", "", result, flags=re.DOTALL).strip()
        result = result.strip("`").strip()

        if result and result != "NOT_FOUND":
            if page.query_selector(result):
                _cache[cache_key] = result
                print(f"[ai_nav] '{task}' → {result}")
                return result
            else:
                print(f"[ai_nav] attempt {attempt+1}: '{result}' matched nothing on page")
        else:
            print(f"[ai_nav] attempt {attempt+1}: NOT_FOUND for '{task}'")

        time.sleep(1)

    raise RuntimeError(f"[ai_nav] Could not locate: '{task}' on {page.url}")


def ai_fill(page, task: str, value: str):
    """Find a field by description and fill it."""
    sel = find_selector(page, task)
    el = page.query_selector(sel)
    if not el:
        raise RuntimeError(f"[ai_nav] Element gone after selector found: {sel}")
    if el.get_attribute("contenteditable"):
        el.click()
        page.keyboard.press("Meta+a")
        page.keyboard.type(value)
    else:
        page.fill(sel, value)
    print(f"[ai_nav] filled '{task}'")


def ai_click(page, task: str):
    """Find a button/link by description and click it."""
    sel = find_selector(page, task)
    page.click(sel)
    page.wait_for_load_state("networkidle")
    print(f"[ai_nav] clicked '{task}'")


def ai_pick_link(page, task: str) -> str:
    """Ask Gemini to return the best href on the page for a given goal."""
    screenshot_b64 = _screenshot(page)
    html = page.evaluate("() => document.body.innerHTML")[:5000]
    prompt = f"""You are helping automate a browser.
TASK: {task}
HTML snippet:
{html}
Find the most relevant link (href) for the task. Return ONLY the full URL or path."""
    return _call_gemini(prompt, screenshot_b64).strip()


# ── internals ─────────────────────────────────────────────────────────────────

def _screenshot(page) -> str:
    return base64.standard_b64encode(page.screenshot()).decode()


def _url_stem(url: str) -> str:
    return re.sub(r"\?.*", "", url)


def _parse_json(raw: str) -> dict | None:
    """Robustly parse JSON from Gemini, handling markdown fences and truncation."""
    clean = re.sub(r"```json|```", "", raw).strip()
    # Try standard parse first
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    # Try ast for single-quoted dicts
    try:
        return ast.literal_eval(clean)
    except Exception:
        pass
    # Try extracting just the JSON object if there's surrounding text
    match = re.search(r'\{[^{}]*\}', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    print(f"[ai_nav] Could not parse: {clean[:200]}")
    return None


def _call_gemini(prompt: str, screenshot_b64: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable not set")
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
                "generationConfig": {
                    "maxOutputTokens": 256,   # selectors are short — don't need more
                    "temperature": 0,
                },
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
