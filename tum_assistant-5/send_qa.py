"""
send_qa.py — send a question/answer to the right place.
Navigation uses direct URLs from destinations.json — no AI clicking.
Gemini is only used for intent parsing and name matching.
"""
import json, os, re, requests, time
from config import ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE, DESTINATIONS_FILE

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def send_qa(course=None, dest_type=None, message=None, *,
            person=None, stream=None, topic="General",
            assignment=None, attachment=None):

    destinations = _load()

    if not dest_type:
        dest_type = _pick_type()
    if not course and dest_type not in ("dm", "stream"):
        course = _pick_course(destinations)
    if not message:
        print("Message (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        message = "\n".join(lines).strip()

    if dest_type == "forum":
        _post_moodle_forum(destinations, course, message, attachment)
    elif dest_type == "dm":
        if not person:
            person = input("Person name: ").strip()
        _send_zulip_dm(person, message, attachment)
    elif dest_type == "stream":
        if not stream:
            stream = _pick_stream()
        if topic == "General":
            topic = input("Topic name: ").strip() or "General"
        _send_zulip_stream(stream, topic, message, attachment)
    elif dest_type == "group_chat":
        _open_moodle_chat(destinations, course)
    elif dest_type == "assignment_comment":
        _post_assignment_comment(destinations, course, message, assignment)
    else:
        raise ValueError(f"Unknown dest_type: {dest_type}")


# ── Moodle forum — direct URL navigation ─────────────────────────────────────

def _post_moodle_forum(destinations, course, message, attachment):
    from utils.browser import new_page

    course_data = _find_course(destinations, course)
    moodle_data = course_data.get("moodle", {})
    forums = moodle_data.get("forums", [])

    subject = input("Post subject/title: ").strip() or "Question"
    page = new_page()

    # Pick the best forum — prefer discussion over announcements
    forum = None
    for f in forums:
        name_lower = (f.get("name") or "").lower()
        if any(kw in name_lower for kw in ["diskussion", "discussion", "frage", "question"]):
            forum = f
            break
    if not forum and forums:
        forum = forums[0]

    if not forum:
        print("[send_qa] No forums in destinations — opening course page")
        page.goto(moodle_data.get("moodle_course_url"), wait_until="networkidle")
    else:
        # Go directly to the "Add new discussion" form
        forum_id_match = re.search(r"[?&](?:id|f)=(\d+)", forum["url"])
        if forum_id_match:
            fid = forum_id_match.group(1)
            post_url = f"https://www.moodle.tum.de/mod/forum/post.php?forum={fid}"
        else:
            post_url = forum["url"]
        print(f"[send_qa] Posting to forum: {forum['name']}")
        page.goto(post_url, wait_until="networkidle")

    print(f"[send_qa] Opened: {page.title()}")

    # Fill subject
    for sel in ["#id_subject", "input[name=subject]", "input[name=Subject]"]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.fill(subject)
            print(f"[send_qa] Filled subject via {sel}")
            break

    # Fill body — Moodle uses TinyMCE (contenteditable div)
    _fill_moodle_editor(page, message)

    # Attachment
    if attachment:
        el = page.query_selector("input[type=file]")
        if el:
            el.set_input_files(attachment)

    # Submit
    for sel in ["#id_submitbutton", "input[name=submitbutton]", "button[type=submit]", "input[type=submit]"]:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            btn.click()
            print("[send_qa] Clicked submit.")
            break

    page.wait_for_load_state("networkidle")
    print("[send_qa] ✓ Forum post submitted.")
    page.close()


# ── Moodle assignment comment — direct URL navigation ─────────────────────────

def _post_assignment_comment(destinations, course, message, assignment):
    from utils.browser import new_page

    course_data = _find_course(destinations, course)
    course_url  = course_data.get("moodle", {}).get("moodle_course_url", "")
    course_id_match = re.search(r"id=(\d+)", course_url)
    if not course_id_match:
        raise RuntimeError(f"Could not extract course ID from {course_url}")
    course_id = course_id_match.group(1)

    page = new_page()

    # Go directly to the assignment list for this course
    assign_list_url = f"https://www.moodle.tum.de/mod/assign/index.php?id={course_id}"
    page.goto(assign_list_url, wait_until="networkidle")
    print(f"[send_qa] Opened assignment list: {page.title()}")

    # Try multiple selectors — Moodle versions differ
    all_links = []
    for sel in [
        "a[href*='mod/assign/view.php']",
        "a[href*='/assign/']",
        "td.c1 a",                    # table layout
        ".generaltable a",
    ]:
        all_links = page.query_selector_all(sel)
        if all_links:
            print(f"[send_qa] Found {len(all_links)} assignments via '{sel}'")
            break

    if not all_links:
        # Last resort: dump all links for debug
        all_page_links = page.query_selector_all("a[href]")
        assign_links = [l for l in all_page_links
                        if "assign" in (l.get_attribute("href") or "").lower()]
        print(f"[send_qa] Fallback: found {len(assign_links)} assign-related links")
        all_links = assign_links

    if not all_links:
        raise RuntimeError(
            f"No assignments found on {assign_list_url}\n"
            f"Page title: {page.title()}\n"
            f"Check if the course ID {course_id} is correct in destinations.json"
        )

    target_url = _match_assignment(all_links, assignment)
    if not target_url:
        raise RuntimeError(f"Could not match assignment '{assignment}' from {len(all_links)} links")

    page.goto(target_url, wait_until="networkidle")
    print(f"[send_qa] Opened: {page.title()}")

    ok = _find_and_fill_comment(page, message)
    if not ok:
        print("[send_qa] ⚠ Comment could not be posted automatically.")
        print(f"[send_qa] Page left open for 60s — add comment manually: {page.url}")
        page.wait_for_timeout(60_000)
    else:
        print("[send_qa] ✓ Assignment comment posted and saved.")
    page.close()


# ── shared editor helper ───────────────────────────────────────────────────────

def _fill_moodle_editor(page, text: str, el=None):
    """Fill Moodle's TinyMCE editor or a plain textarea."""
    if el is None:
        for sel in ["div.mce-content-body", "[contenteditable='true']",
                    "textarea#id_message", "#id_message"]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                print(f"[send_qa] Filling editor via {sel}")
                break
    if el is None:
        print("[send_qa] ⚠ Could not find message editor")
        return
    el.click()
    page.keyboard.press("Meta+a")
    page.keyboard.type(text)


# ── Moodle group chat ──────────────────────────────────────────────────────────

def _open_moodle_chat(destinations, course):
    chats = _find_course(destinations, course).get("moodle", {}).get("group_chats", [])
    if not chats:
        raise RuntimeError(f"No group chats for '{course}'.")
    import webbrowser
    webbrowser.open(chats[0]["url"])
    print(f"[send_qa] Opened: {chats[0]['url']}")


# ── Zulip DM — AI name matching ───────────────────────────────────────────────

def _send_zulip_dm(person, message, attachment):
    print(f"[send_qa] Searching Zulip for: '{person}'")

    r = requests.get(f"{ZULIP_SITE}/api/v1/users",
                     auth=(ZULIP_EMAIL, ZULIP_API_KEY),
                     params={"include_bots": "true"})
    r.raise_for_status()
    all_users = r.json()["members"]

    user_list = "\n".join([f"{u['user_id']}: {u['full_name']} ({u['email']})"
                           for u in all_users])

    result = _gemini_json(
        f'A student wants to send a Zulip DM to: "{person}"\n\n'
        f'Full user list:\n{user_list}\n\n'
        f'The name might be a nickname or partial.\n'
        f'Return JSON only:\n'
        f'- Single match: {{"user_id": 123, "full_name": "Name", "email": "e@tum.de"}}\n'
        f'- Multiple: {{"matches": [{{"user_id": 1, "full_name": "...", "email": "..."}}]}}\n'
        f'- None: {{"user_id": null}}'
    )

    if "matches" in result:
        print("[send_qa] Multiple possible matches:")
        for i, u in enumerate(result["matches"]):
            print(f"  [{i+1}] {u['full_name']} ({u['email']})")
        idx = int(input("Pick number: ").strip()) - 1
        target = result["matches"][idx]
    elif not result.get("user_id"):
        raise RuntimeError(f"Could not find anyone matching '{person}' in Zulip.")
    else:
        target = result
        print(f"[send_qa] Matched '{person}' → {target['full_name']} ({target['email']})")

    content = message
    if attachment:
        content += f"\n[{attachment}]({_zulip_upload(attachment)})"

    r = requests.post(f"{ZULIP_SITE}/api/v1/messages",
        auth=(ZULIP_EMAIL, ZULIP_API_KEY),
        data={"type": "direct",
              "to": json.dumps([target["user_id"]]),
              "content": content})
    r.raise_for_status()
    print(f"[send_qa] ✓ Zulip DM sent to {target['full_name']}.")


# ── Zulip stream ───────────────────────────────────────────────────────────────

def _send_zulip_stream(stream_name, topic, message, attachment):
    data = json.loads(DESTINATIONS_FILE.read_text())
    streams = [s["name"] for s in data.get("_zulip", {}).get("streams", [])]
    resolved = _gemini_text(
        f'Match "{stream_name}" to one of these Zulip streams: {streams}\n'
        f'Return only the exact stream name.'
    ).strip() if streams else stream_name

    content = message
    if attachment:
        content += f"\n[{attachment}]({_zulip_upload(attachment)})"

    r = requests.post(f"{ZULIP_SITE}/api/v1/messages",
        auth=(ZULIP_EMAIL, ZULIP_API_KEY),
        data={"type": "stream", "to": resolved, "topic": topic, "content": content})
    r.raise_for_status()
    print(f"[send_qa] ✓ Posted to '{resolved}' / '{topic}'.")


def _zulip_upload(path):
    with open(path, "rb") as f:
        r = requests.post(f"{ZULIP_SITE}/api/v1/user_uploads",
            auth=(ZULIP_EMAIL, ZULIP_API_KEY), files={"file": f})
    r.raise_for_status()
    return r.json()["uri"]


# ── Gemini helpers ─────────────────────────────────────────────────────────────

def _gemini_text(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    for attempt in range(3):
        resp = requests.post(
            f"{GEMINI_API}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0},
            }
        )
        if resp.status_code in (429, 503):
            time.sleep(15 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError("Gemini unavailable")


def _gemini_json(prompt: str) -> dict:
    raw = _gemini_text(prompt)
    return json.loads(re.sub(r"```json|```", "", raw).strip())


# ── Interactive helpers ────────────────────────────────────────────────────────

def _pick_course(destinations):
    courses = [k for k in destinations.keys() if not k.startswith("_")]
    print("\nAvailable courses:")
    for i, name in enumerate(courses):
        print(f"  [{i+1}] {name}")
    while True:
        val = input("Pick number or name: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(courses):
            return courses[int(val) - 1]
        matches = [c for c in courses if val.lower() in c.lower()]
        if len(matches) == 1:    return matches[0]
        elif len(matches) > 1:  print(f"  Multiple: {matches}")
        else:                   print("  Not found.")

def _pick_type():
    opts = {"1": "forum", "2": "dm", "3": "stream", "4": "group_chat"}
    print("\nSend to:\n  [1] Moodle forum\n  [2] Zulip DM\n  [3] Zulip stream\n  [4] Moodle group chat")
    while True:
        val = input("Pick: ").strip()
        if val in opts: return opts[val]
        print("  Enter 1-4.")

def _pick_stream():
    try:
        data = json.loads(DESTINATIONS_FILE.read_text())
        streams = data.get("_zulip", {}).get("streams", [])
        if streams:
            print("\nYour Zulip streams:")
            for i, s in enumerate(streams[:30]):
                print(f"  [{i+1}] {s['name']}")
            val = input("Pick number or name: ").strip()
            if val.isdigit() and 1 <= int(val) <= len(streams):
                return streams[int(val)-1]["name"]
            return val
    except Exception:
        pass
    return input("Stream name: ").strip()



# ── assignment + comment helpers ──────────────────────────────────────────────

def _match_assignment(links, assignment: str):
    """
    Match assignment purely in Python — no Gemini, no user input, never fails silently.
    Extracts the target number from the description and finds the link whose
    text contains the same number.
    e.g. "6th homework" → 6 → matches "Abgabe Blatt 06" or "Hausaufgabe 6"
    """
    if not assignment:
        return None

    items = [(link.inner_text().strip(), link.get_attribute("href")) for link in links]
    if not items:
        return None

    a = assignment.lower().strip()

    # Word → digit map for ordinals in English and German
    word_to_num = {
        "first":"1","1st":"1","eine":"1","ersten":"1","erste":"1","eins":"1",
        "second":"2","2nd":"2","zweite":"2","zweiten":"2","zwei":"2",
        "third":"3","3rd":"3","dritte":"3","dritten":"3","drei":"3",
        "fourth":"4","4th":"4","vierte":"4","vierten":"4","vier":"4",
        "fifth":"5","5th":"5","fünfte":"5","fünften":"5","fünf":"5",
        "sixth":"6","6th":"6","sechste":"6","sechsten":"6","sechs":"6",
        "seventh":"7","7th":"7","siebte":"7","siebten":"7","sieben":"7",
        "eighth":"8","8th":"8","achte":"8","achten":"8","acht":"8",
        "ninth":"9","9th":"9","neunte":"9","neunten":"9","neun":"9",
        "tenth":"10","10th":"10","zehnte":"10","zehnten":"10","zehn":"10",
        "eleventh":"11","11th":"11","elfte":"11","elften":"11",
        "twelfth":"12","12th":"12","zwölfte":"12","zwölften":"12",
    }

    # Extract target number from the assignment description
    target = None
    for word in re.split(r"[\s,./]+", a):
        if word in word_to_num:
            target = word_to_num[word]
            break
    if not target:
        nums = re.findall(r"\d+", a)
        if nums:
            target = nums[0]

    if not target:
        print(f"[send_qa] Could not extract number from '{assignment}' — using last assignment")
        return items[-1][1]

    # Find the link whose text contains that number
    for text, href in items:
        nums_in_text = re.findall(r"\d+", text)
        if any(int(n) == int(target) for n in nums_in_text):
            print(f"[send_qa] Matched '{assignment}' (#{target}) → {text}")
            return href

    # If no exact match, pick the closest number
    best, best_href = None, None
    for text, href in items:
        nums_in_text = re.findall(r"\d+", text)
        for n in nums_in_text:
            if best is None or abs(int(n) - int(target)) < abs(int(best) - int(target)):
                best, best_href = n, href
    if best_href:
        print(f"[send_qa] No exact match for #{target} — using closest: #{best}")
        return best_href

    return items[-1][1]


def _find_and_fill_comment(page, message: str) -> bool:
    """
    Post a comment on a Moodle assignment page.
    Uses JavaScript to search the full DOM — no selector guessing, no AI needed
    for finding elements. JS can see every element regardless of scroll position.
    """
    # Scroll to bottom so the comment section is in view
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)

    # ── Step 1: find and click the comment toggle via JS ─────────────────────
    # JS searches the ENTIRE DOM for any element that looks like a comment toggle
    clicked = page.evaluate("""() => {
        // Known Moodle comment toggle selectors
        const toggleSelectors = [
            '.comment-toggle',
            'a.comment-link',
            '[data-action="comment-add"]',
        ];
        for (const sel of toggleSelectors) {
            const el = document.querySelector(sel);
            if (el) { el.click(); return 'selector:' + sel; }
        }
        // Search all clickable elements for comment-related text
        const keywords = /Kommentar|Comment|Abgabekommentar/i;
        for (const el of document.querySelectorAll('a, button, span, div[role=button]')) {
            if (keywords.test(el.textContent) && el.offsetParent !== null) {
                el.click();
                return 'text:' + el.textContent.trim().slice(0, 40);
            }
        }
        return null;
    }""")

    if clicked:
        print(f"[send_qa] Expanded comment section via: {clicked}")
        page.wait_for_timeout(1200)
    else:
        print("[send_qa] No toggle found — comment area may already be open")

    # ── Step 2: find the textarea via JS ─────────────────────────────────────
    # Returns the CSS selector we can use to target the element
    input_info = page.evaluate("""() => {
        const selectors = [
            'textarea.fitvidsignore',
            'textarea[id*=comment]',
            'textarea[name*=comment]',
            '#id_submissioncomment',
            'div.mce-content-body',
            'div[contenteditable=true]',
            'textarea',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) {
                return {sel: sel, tag: el.tagName.toLowerCase(),
                        ce: el.getAttribute('contenteditable')};
            }
        }
        return null;
    }""")

    if not input_info:
        print("[send_qa] ⚠ No comment input found in DOM")
        return False

    print(f"[send_qa] Found comment input: {input_info['sel']}")

    # ── Step 3: fill via Playwright (handles focus + typing correctly) ────────
    input_el = page.query_selector(input_info["sel"])
    if not input_el:
        print("[send_qa] ⚠ Could not get element handle")
        return False

    input_el.scroll_into_view_if_needed()
    if input_info.get("ce") in ("true", ""):
        input_el.click()
        page.keyboard.press("Meta+a")
        page.keyboard.type(message)
    else:
        input_el.click()
        input_el.fill(message)
    print("[send_qa] Filled comment")

    # ── Step 4: use Gemini to identify the save button, then click via Playwright ─
    # IMPORTANT: must use Playwright click (not JS el.click()) so Moodle's
    # event listeners fire properly. JS click() bypasses React/Moodle handlers.
    # Also wait briefly — Moodle enables the save button only after text is typed.
    page.wait_for_timeout(600)

    # Collect all visible interactive elements and let Gemini identify the save button
    candidates = page.evaluate("""() => {
        const results = [];
        for (const el of document.querySelectorAll('a, button, input[type=submit]')) {
            if (el.offsetParent === null) continue;
            results.push({
                tag: el.tagName.toLowerCase(),
                text: (el.value || el.textContent || '').trim().slice(0, 80),
                id: el.id || '',
                cls: el.className || '',
                action: el.dataset.action || ''
            });
        }
        return results;
    }""")

    save_sel = _gemini_text(
        'I am on a Moodle assignment page and just filled in a comment textarea.\n'
        'I need to click the button that SAVES/POSTS the comment (not the toggle that opens/closes the comment box).\n'
        'Here are all visible interactive elements on the page (tag, id, class, data-action, text):\n'
        + json.dumps(candidates, ensure_ascii=False, indent=2) +
        '\n\nReturn ONLY a single CSS selector string for the save button, nothing else.\n'
        'Prefer data-action selectors, then id, then class. Example: a[data-action="post"] or #some-id'
    ).strip().strip("'").strip('"')

    if not save_sel:
        print("[send_qa] ⚠ Gemini could not identify save button")
        return False

    print(f"[send_qa] Gemini identified save button: {save_sel}")
    try:
        # Use Playwright locator — fires real mouse events that trigger Moodle's JS
        page.locator(save_sel).first.click()
        page.wait_for_load_state("networkidle", timeout=8_000)
        print("[send_qa] ✓ Comment saved")
        return True
    except Exception as e:
        print(f"[send_qa] ⚠ Click failed: {e}")
        return False

# ── helpers ────────────────────────────────────────────────────────────────────

def _load():
    if not DESTINATIONS_FILE.exists():
        raise RuntimeError("destinations.json not found. Run: python main.py crawl")
    return json.loads(DESTINATIONS_FILE.read_text())

def _find_course(destinations, fragment):
    for name, data in destinations.items():
        if fragment and fragment.lower() in name.lower():
            return data
    raise RuntimeError(f"Course '{fragment}' not found. Available: {[k for k in destinations if not k.startswith('_')]}")
