from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    page = b.new_page()

    # 1. Start at the Moodle login portal
    page.goto('https://www.moodle.tum.de/login/index.php')

    # 2. Click the Shibboleth "TUM Login" button
    # This ensures the correct university parameters are passed
    page.click("a[href*='shibboleth']")

    # 3. Wait until we successfully land on the TUM SSO domain
    page.wait_for_url("**/login.tum.de/**", timeout=15000)

    # 4. Take the screenshot and print the HTML
    page.screenshot(path='sso_debug.png')
    print("Page Title:", page.title())
    print("HTML Snippet:", page.content()[:500])