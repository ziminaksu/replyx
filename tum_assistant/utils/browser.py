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


from config import TUM_USERNAME, TUM_PASSWORD


def _login_moodle(page):
    print("[auth] Navigating to Moodle via SSO bypass...")

    # 1. Use the direct bypass URL we just proved works
    bypass_url = (
        "https://www.moodle.tum.de/Shibboleth.sso/Login?"
        "providerId=https%3A%2F%2Ftumidp.lrz.de%2Fidp%2Fshibboleth&"
        "target=https%3A%2F%2Fwww.moodle.tum.de%2Fauth%2Fshibboleth%2Findex.php"
    )
    page.goto(bypass_url, wait_until="domcontentloaded", timeout=60_000)

    # 2. Give the redirect a moment to settle
    page.wait_for_load_state("networkidle")

    # 3. If we are on the login page, fill credentials
    if "login.tum.de" in page.url:
        print("[auth] TUM SSO page detected. Entering credentials...")
        # Use the standard j_username/j_password selectors for Shibboleth
        page.fill("input[name='j_username']", TUM_USERNAME)
        page.fill("input[name='j_password']", TUM_PASSWORD)

        # Click the login button (usually named _eventId_proceed in Shibboleth)
        page.click("button[name='_eventId_proceed'], button[type='submit']")

        # Wait for the post-login redirect back to Moodle
        print("[auth] Waiting for redirect back to Moodle...")
        page.wait_for_load_state("networkidle", timeout=60_000)

    # 4. Verify we actually made it into Moodle
    if "moodle.tum.de" not in page.url:
        page.screenshot(path="login_debug_failed.png")
        raise RuntimeError(f"Login failed. Ended up at: {page.url} (Saved screenshot to login_debug_failed.png)")

    print("[auth] ✓ Successfully logged into Moodle.")

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
