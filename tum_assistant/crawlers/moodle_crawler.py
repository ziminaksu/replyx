"""
crawlers/moodle_crawler.py — Phase 1 discovery for Moodle.

Crawls every enrolled course and records:
  - course forums (discussion boards)  → direct post URL
  - messaging / group chats            → direct chat URL
  - participants (tutors, profs)       → name + profile URL for DM

Run once per semester:  python -m crawlers.moodle_crawler
Results appended into destinations.json.
"""
import json, re
from pathlib import Path
from utils.browser import new_page
from config import MOODLE_BASE, DESTINATIONS_FILE


def crawl() -> dict:
    page = new_page()
    courses = _get_enrolled_courses(page)
    destinations = {}

    for course in courses:
        print(f"  [moodle] crawling: {course['name']}")
        dest = {
            "moodle_course_id":  course["id"],
            "moodle_course_url": course["url"],
            "forums":    _get_forums(page, course),
            "group_chats": _get_group_chats(page, course),
            "participants": _get_participants(page, course),
        }
        destinations[course["name"]] = dest

    page.close()
    return destinations


def _get_enrolled_courses(page) -> list[dict]:
    page.goto(f"{MOODLE_BASE}/my/courses.php", wait_until="networkidle")

    # Save HTML so we can see the real DOM
    debug_html = Path(__file__).parent.parent / "moodle_debug.html"
    debug_html.write_text(page.content())
    print(f"  [moodle] Saved page HTML → {debug_html}")

    # Try the only selector we know works: any link to /course/view.php
    courses = {}
    for el in page.query_selector_all("a[href*='/course/view.php']"):
        href = el.get_attribute("href") or ""
        m = re.search(r"id=(\d+)", href)
        if not m:
            continue
        cid = m.group(1)
        name = el.inner_text().strip() or f"Course_{cid}"
        if cid not in courses:
            courses[cid] = {"id": cid, "name": name, "url": href}

    return list(courses.values())


def _get_forums(page, course: dict) -> list[dict]:
    """Find all forum activities in a course."""
    page.goto(course["url"])
    forums = []
    for el in page.query_selector_all("a[href*='mod/forum']"):
        href = el.get_attribute("href") or ""
        if "/view.php" not in href:
            continue
        forums.append({
            "name": el.inner_text().strip(),
            "url":  href,
            # post URL pattern: .../mod/forum/post.php?forum=<id>
            "post_url": href.replace("view.php", "post.php").replace(
                "id=", "forum="
            ),
        })
    return forums


def _get_group_chats(page, course: dict) -> list[dict]:
    """Find BigBlueButton / Moodle chat / messaging group links."""
    page.goto(course["url"])
    chats = []
    selectors = ["a[href*='mod/chat']", "a[href*='mod/bigbluebuttonbn']"]
    for sel in selectors:
        for el in page.query_selector_all(sel):
            href = el.get_attribute("href") or ""
            chats.append({"name": el.inner_text().strip(), "url": href})
    return chats


def _get_participants(page, course: dict) -> list[dict]:
    """List course participants — tutors and lecturers first."""
    url = f"{MOODLE_BASE}/user/index.php?id={course['id']}&roleid=0"
    page.goto(url)
    participants = []
    for row in page.query_selector_all("table.userenrolment tr"):
        name_el  = row.query_selector("td.c1 a")
        role_el  = row.query_selector("td.c5")          # role column varies by Moodle version
        if not name_el:
            continue
        role = role_el.inner_text().strip() if role_el else "Student"
        participants.append({
            "name":     name_el.inner_text().strip(),
            "profile":  name_el.get_attribute("href"),
            "role":     role,
        })
    return participants


def run():
    print("[moodle] Starting discovery crawl...")
    data = {}
    if DESTINATIONS_FILE.exists():
        data = json.loads(DESTINATIONS_FILE.read_text())

    new_data = crawl()
    for course, dest in new_data.items():
        if course not in data:
            data[course] = {}
        data[course]["moodle"] = dest

    DESTINATIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[moodle] Done. {len(new_data)} courses written to {DESTINATIONS_FILE}")


if __name__ == "__main__":
    run()
