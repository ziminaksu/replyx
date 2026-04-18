"""
send_qa.py — send a question/answer to the right place.
Moodle: fully AI-driven (no hardcoded selectors).
Zulip: API + AI name matching.
"""
import json, os, re, requests, time
from config import ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE, DESTINATIONS_FILE

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

def _post_assignment_comment(destinations, course, message, assignment):
    from utils.browser import new_page
    from utils.ai_navigator import ai_click, ai_fill, ai_do

    course_data = _find_course(destinations, course)
    course_url  = course_data.get("moodle", {}).get("moodle_course_url")

    page = new_page()
    page.goto(course_url, wait_until="networkidle")

    # AI navigates to Aufgabenabgabe / assignment section
    ai_do(page,
        f'Find the homework submission section called "Aufgabenabgabe" or similar. '
        f'Then find assignment number/name: "{assignment}". '
        f'Open it and find the comment or feedback text box to leave a response.'
    )

    ai_fill(page, "comment or feedback text area for the homework submission", message)
    ai_click(page, "save or submit button for the comment")
    print("[send_qa] ✓ Assignment comment posted.")
    page.close()

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


# ── Moodle forum — fully AI-driven ────────────────────────────────────────────
def _post_moodle_forum(destinations, course, message, attachment):
    from utils.browser import new_page
    from utils.ai_navigator import ai_fill, ai_click, ai_do

    course_data = _find_course(destinations, course)
    course_url  = course_data.get("moodle", {}).get("moodle_course_url")

    subject = input("Post subject/title: ").strip() or "Question"
    page = new_page()

    page.goto(course_url, wait_until="networkidle")
    print(f"[send_qa] Opened: {page.title()}")

    # Tell Gemini exactly what we want to do — it figures out where to click
    ai_do(page,
        f'Find the most appropriate place to post this student message: "{message}". '
        f'It could be a forum, a Q&A section, an assignment comment box, or any discussion area. '
        f'Navigate there and open the form to write a new post or reply.'
    )

    # Now fill in the post
    ai_fill(page, "subject or title input field for the post", subject)
    ai_fill(page, "main message body text area or rich text editor", message)

    if attachment:
        from utils.ai_navigator import find_selector
        try:
            sel = find_selector(page, "file upload input")
            page.set_input_files(sel, attachment)
        except Exception:
            print("[send_qa] ⚠ Skipping attachment")

    ai_click(page, "submit or post button to publish")
    print("[send_qa] ✓ Post submitted.")
    page.close()


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
        f'The name might be a nickname or partial (e.g. "Ksusha" = "Ksenia").\n'
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
    api_key = os.environ["GEMINI_API_KEY"]
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
        if len(matches) == 1:   return matches[0]
        elif len(matches) > 1: print(f"  Multiple: {matches}")
        else:                  print("  Not found.")

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
