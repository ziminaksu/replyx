"""
crawlers/tumonline_crawler.py — Phase 1 discovery for TUM Online.

Crawls CAMPUSonline (campus.tum.de) and records every assignment
submission box across all enrolled courses, including:
  - submission URL (direct link to the upload page)
  - deadline
  - course name

Run once per semester (or when new sheets appear):
    python -m crawlers.tumonline_crawler
"""
import json, re
from config import TUM_ONLINE_BASE, DESTINATIONS_FILE
from utils.browser import new_page


# TUM Online uses CAMPUSonline. The exact URL paths depend on your TUM setup.
# Adjust COURSE_LIST_PATH and SUBMISSION_SELECTOR to match what you see in the DOM.
COURSE_LIST_PATH   = "/wbstudent.overview"        # typical student overview page
SUBMISSION_KEYWORD = "Abgabe"                      # German: "submission"


def crawl() -> dict:
    page = new_page()
    page.goto(f"{TUM_ONLINE_BASE}{COURSE_LIST_PATH}")
    page.wait_for_load_state("networkidle")

    courses = _get_courses(page)
    destinations = {}

    for course in courses:
        print(f"  [tumonline] crawling: {course['name']}")
        submissions = _get_submission_boxes(page, course)
        if submissions:
            destinations[course["name"]] = {
                "tumonline_course_url": course["url"],
                "submission_boxes": submissions,
            }

    page.close()
    return destinations


def _get_courses(page) -> list[dict]:
    """Parse enrolled courses from the student overview."""
    courses = []
    # CAMPUSonline typically lists courses in a table with links
    for el in page.query_selector_all("a[href*='wbLV.wbShowLVDetail'], a[href*='WBLV']"):
        href = el.get_attribute("href") or ""
        if not href:
            continue
        # Make absolute if relative
        if href.startswith("/"):
            href = TUM_ONLINE_BASE + href
        courses.append({
            "name": el.inner_text().strip()[:80],
            "url":  href,
        })
    # deduplicate by URL
    seen, unique = set(), []
    for c in courses:
        if c["url"] not in seen:
            seen.add(c["url"])
            unique.append(c)
    return unique


def _get_submission_boxes(page, course: dict) -> list[dict]:
    """Visit a course page and find assignment submission upload boxes."""
    try:
        page.goto(course["url"], timeout=12_000)
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        return []

    boxes = []
    # Look for links that contain upload / submission keywords
    for el in page.query_selector_all("a"):
        text = el.inner_text().strip()
        href = el.get_attribute("href") or ""
        if not any(kw in text for kw in [SUBMISSION_KEYWORD, "Upload", "Einreichung", "submission"]):
            continue
        if not href:
            continue
        if href.startswith("/"):
            href = TUM_ONLINE_BASE + href

        # Try to find a deadline nearby (look for sibling text with date pattern)
        deadline = None
        parent = el.evaluate_handle("el => el.closest('tr, li, div')")
        if parent:
            parent_text = page.evaluate("el => el ? el.innerText : ''", parent)
            date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", parent_text or "")
            if date_match:
                deadline = date_match.group(0)

        boxes.append({
            "name":     text,
            "url":      href,
            "deadline": deadline,
        })
    return boxes


def run():
    print("[tumonline] Starting discovery crawl...")
    data = {}
    if DESTINATIONS_FILE.exists():
        data = json.loads(DESTINATIONS_FILE.read_text())

    new_data = crawl()
    for course, dest in new_data.items():
        if course not in data:
            data[course] = {}
        data[course]["tumonline"] = dest

    DESTINATIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[tumonline] Done. {len(new_data)} courses with submissions written.")


if __name__ == "__main__":
    run()
