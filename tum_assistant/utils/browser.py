"""
utils/browser.py — shared Playwright session with TUM SSO login.
Saves session cookies to disk so login only happens once.
"""
from playwright.sync_api import sync_playwright, BrowserContext, Page
from pathlib import Path
from config import TUM_USERNAME, TUM_PASSWORD, MOODLE_BASE

HEADLESS      = False
SESSION_FILE  = Path(__file__).parent / "session.json"

_playwright = None
_browser    = None
_context: BrowserContext | None = None


def get_context() -> BrowserContext:
    global _playwright, _browser, _context
    if _context is not None:
        return _context

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=HEADLESS,
        args=["--new-window", "--force-app-mode"]
    )

    # Try to restore saved session first
    if SESSION_FILE.exists():
        print("[auth] Restoring saved session...")
        _context = _browser.new_context(storage_state=str(SESSION_FILE))
        # Quick check — are we actually logged in?
        page = _context.new_page()
        page.goto(f"{MOODLE_BASE}/my/", wait_until="networkidle")
        if "moodle.tum.de" in page.url and "login" not in page.url:
            print("[auth] ✓ Session restored, already logged in")
            page.close()
            return _context
        else:
            print("[auth] Saved session expired, logging in again...")
            page.close()
            _context = _browser.new_context()
    else:
        _context = _browser.new_context()

    _login_moodle(_context.new_page())
    return _context

def _login_moodle(page: Page):
    import time
    debug_path = Path(__file__).parent / "login_debug.png"

    print("[auth] Navigating to TUM SSO login...")
    page.goto(f"{MOODLE_BASE}/auth/shibboleth/index.php", wait_until="networkidle")

    # Fill credentials
    user_sel = _find_selector(page, ["input[name='j_username']", "input[name='username']", "input[type='text']"], timeout=20_000)
    pass_sel = _find_selector(page, ["input[name='j_password']", "input[name='password']", "input[type='password']"], timeout=5_000)
    submit_sel = _find_selector(page, ["button[type='submit']", "input[type='submit']"], timeout=5_000)

    print("[auth] Filling credentials...")
    if user_sel: page.fill(user_sel, TUM_USERNAME)
    if pass_sel: page.fill(pass_sel, TUM_PASSWORD)
    if submit_sel: page.click(submit_sel)

    # Handle multi-step SSO — keep clicking "Proceed"/"Weiter" until we reach Moodle
    print("[auth] Waiting for Moodle (approve 2FA / click Proceed if prompted)...")
    for i in range(90):
        current = page.url
        if "moodle.tum.de" in current:
            break

        # Click any "Proceed" or "Weiter" button that appears on intermediate pages
        for proceed_sel in [
            "input[name='_eventId_proceed']",
            "button[name='_eventId_proceed']",
            "input[value='Proceed']",
            "button:has-text('Proceed')",
            "button:has-text('Weiter')",
            "input[type='submit']",
        ]:
            el = page.query_selector(proceed_sel)
            if el:
                print(f"[auth] Clicking intermediate button: {proceed_sel}")
                el.click()
                page.wait_for_load_state("domcontentloaded")
                break

        time.sleep(2)
    else:
        page.screenshot(path=str(debug_path))
        raise RuntimeError(f"Login timed out. Last URL: {page.url}")

    print("[auth] ✓ Logged in")
    _context.storage_state(path=str(SESSION_FILE))
    print(f"[auth] Session saved")
    page.close()


def _find_selector(page: Page, selectors: list[str], timeout: int = 5_000) -> str | None:
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            return sel
        except Exception:
            continue
    return None


def new_page() -> Page:
    return get_context().new_page()


def close():
    global _playwright, _browser, _context
    if _browser:
        _browser.close()
    if _playwright:
        _playwright.stop()
    _context = _browser = _playwright = None
