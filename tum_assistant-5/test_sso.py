from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    page = b.new_page()

    print("Bypassing Moodle UI and going straight to TUM Identity Provider...")

    # This is the exact URL Moodle generates when you click "TUM Login".
    # It contains the 'providerId' so it skips the Organisation Selection.
    bypass_url = (
        "https://www.moodle.tum.de/Shibboleth.sso/Login?"
        "providerId=https%3A%2F%2Ftumidp.lrz.de%2Fidp%2Fshibboleth&"
        "target=https%3A%2F%2Fwww.moodle.tum.de%2Fauth%2Fshibboleth%2Findex.php"
    )

    page.goto(bypass_url, wait_until="domcontentloaded")

    print("Waiting for TUM SSO redirect...")
    page.wait_for_url("**/login.tum.de/**", timeout=60_000)

    print("Taking screenshot...")
    page.screenshot(path='sso_debug.png')
    print("Page Title:", page.title())
    print("HTML Snippet:", page.content()[:500])
    print("Done!")