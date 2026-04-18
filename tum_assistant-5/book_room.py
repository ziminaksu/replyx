"""
actions/book_room.py — library room reservation via TUM Online.
"""
from utils.browser import new_page
from config import TUM_ONLINE_BASE


def book_room(date: str, duration_hours: int = 2, building: str | None = None):
    """
    date: 'YYYY-MM-DD'
    duration_hours: 1, 2, 3, 4
    building: optional filter e.g. 'Stammgelände', 'Garching'
    """
    page = new_page()

    # TUM library room booking — adjust URL to your TUM instance
    page.goto(f"{TUM_ONLINE_BASE}/wbRaum.raumListe")
    page.wait_for_load_state("networkidle")

    # Fill date filter
    date_input = page.query_selector("input[name*='date'], input[type='date']")
    if date_input:
        date_input.fill(date)

    # Select duration if dropdown exists
    dur_select = page.query_selector("select[name*='duration'], select[name*='dauer']")
    if dur_select:
        dur_select.select_option(str(duration_hours))

    # Building filter
    if building:
        bldg_select = page.query_selector("select[name*='building'], select[name*='gebaeude']")
        if bldg_select:
            bldg_select.select_option(label=building)

    # Search
    page.click("button[type='submit'], input[type='submit']")
    page.wait_for_load_state("networkidle")

    # Pick first available slot
    slot = page.query_selector(".available, .frei, [data-status='available']")
    if not slot:
        print("[book_room] No available slots found for the given criteria.")
        print(f"[book_room] Page left open: {page.url}")
        page.wait_for_timeout(120_000)
        page.close()
        return

    slot.click()
    page.wait_for_load_state("networkidle")

    # Confirm booking
    confirm = page.query_selector("button[name*='confirm'], input[value*='Buchen'], input[value*='Reserv']")
    if confirm:
        confirm.click()
        page.wait_for_load_state("networkidle")
        print(f"[book_room] Room booked for {date}, {duration_hours}h.")
    else:
        print("[book_room] Confirmation button not found — check the page manually.")

    page.close()
