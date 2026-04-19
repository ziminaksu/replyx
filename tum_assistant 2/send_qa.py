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


# ── Moodle forum — live-scrape EVERYTHING on the course page ─────────────────

def _post_moodle_forum(destinations, course, message, attachment):
    """
    Scrape every activity link on the live course page.
    Gemini ranks them. We navigate to each in order until we land on a page
    where we can actually post. Playwright handles the form directly — no
    Gemini guessing of element indices on the post form itself.
    """
    from utils.browser import new_page

    subject = input("Post subject/title: ").strip() or "Question"
    page = new_page()

    _, course_url = _live_moodle_course(course, page)

    print(f"[send_qa] Opening course page: {course_url}")
    page.goto(course_url, wait_until="networkidle")
    print(f"[send_qa] Course page: {page.title()}")

    # Scrape every /mod/.../ link — section, kind, text, url. No filtering.
    items = page.evaluate(r"""() => {
        const results = [];
        let currentSection = '';
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_ELEMENT, null
        );
        let node;
        while (node = walker.nextNode()) {
            if (node.matches(
                'h3, h4, .sectionname, .section-title, .section-heading, ' +
                '.content h3, li.section .sectionname'
            )) {
                const t = (node.textContent || '').trim();
                if (t) currentSection = t.slice(0, 120);
            }
            if (node.tagName === 'A' && node.href &&
                /\/mod\/\w+\//i.test(node.href)) {
                const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text) continue;
                const m = node.href.match(/\/mod\/(\w+)\//);
                results.push({
                    section: currentSection,
                    kind:    m ? m[1].toLowerCase() : '',
                    text:    text.slice(0, 200),
                    url:     node.href,
                });
            }
        }
        return results;
    }""")

    seen, unique = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique.append(it)
    items = unique

    if not items:
        page.close()
        raise RuntimeError(f"No activity links found on course page for '{course}'.")

    print(f"[send_qa] Found {len(items)} activities on course page")

    # Gemini ranks — all data as JSON, no interpolation of user strings
    ranked_indices = list(range(len(items)))
    try:
        pick = _gemini_json(
            "A student wants to post on Moodle. Rank all activity items "
            "from most to least suitable for this post. "
            "Return JSON only: {\"ranked\": [index, ...]}\n"
            + json.dumps({
                "subject": subject,
                "message": message,
                "items": [{"index": i, "kind": it["kind"],
                           "section": it["section"], "text": it["text"]}
                          for i, it in enumerate(items)],
            }, ensure_ascii=False)
        )
        ranked = [int(i) for i in pick.get("ranked", []) if 0 <= int(i) < len(items)]
        ranked_set = set(ranked)
        ranked_indices = ranked + [i for i in range(len(items)) if i not in ranked_set]
    except Exception as exc:
        print(f"[send_qa] Gemini ranking failed ({exc}), using original order.")

    posted = False
    for idx in ranked_indices:
        candidate = items[idx]
        print(f"[send_qa] Trying [{candidate['kind']}] {candidate['text']}")
        try:
            if page.is_closed():
                page = new_page()
            success = _try_post_to(page, candidate, subject, message, attachment)
        except Exception as exc:
            print(f"[send_qa] ✗ '{candidate['text']}': {exc}")
            success = False
            if page.is_closed():
                page = new_page()
        if success:
            posted = True
            break

    if not page.is_closed():
        page.close()

    if not posted:
        raise RuntimeError(
            f"Could not post to any of the {len(ranked_indices)} activities tried."
        )
    print("[send_qa] ✓ Posted.")


def _try_post_to(page, item, subject, message, attachment) -> bool:
    """
    Navigate to item['url'].

    Playwright probes the page directly for known Moodle posting surfaces:
      1. Already on a post form (has #id_subject) — fill it directly.
      2. Has a visible "add discussion" button/link — click it, then fill form.
      3. Not a postable page — return False so the next candidate is tried.

    No Gemini involved in navigation or form-filling. Gemini only verifies
    success from the page title after submission.
    """
    page.goto(item["url"], wait_until="networkidle")
    page.wait_for_timeout(800)

    # ── Case 1: already on the post form ──────────────────────────────────────
    if _is_post_form(page):
        print("[send_qa] Already on post form, filling directly")
        return _fill_moodle_post_form(page, subject, message, attachment)

    # ── Case 2: forum view page — find and click the "add discussion" button ──
    # Moodle renders this as a button or link. We collect ALL visible buttons
    # and links, ask Gemini which one opens a new post, then click it.
    clickables = page.evaluate("""() => {
        const results = [];
        for (const el of document.querySelectorAll('a[href], button')) {
            if (el.offsetParent === null) continue;
            results.push({
                index: results.length,
                tag:   el.tagName.toLowerCase(),
                text:  (el.textContent || el.value || '').trim().slice(0, 120),
                href:  el.href || '',
                id:    el.id || '',
                cls:   (el.className || '').slice(0, 80),
            });
        }
        return results;
    }""")

    print(f"[send_qa] DEBUG clickables on {page.title()!r}:")
    for c in clickables:
        print(f"  [{c['index']}] {c['tag']} id={c['id']!r} text={c['text']!r} href={c['href'][:60]!r}")

    pick = _gemini_json(
        "I am on a Moodle forum view page and want to start a new post.\n"
        "Which element in this list is the button/link to add a new discussion or post?\n"
        "If none exists (e.g. this is a resource, folder, PDF, or announcement-only forum), "
        "return {\"index\": null}.\n"
        "Return JSON only: {\"index\": N} or {\"index\": null}\n"
        + json.dumps({"page_title": page.title(), "clickables": clickables},
                     ensure_ascii=False)
    )

    idx = pick.get("index")
    print(f"[send_qa] DEBUG Gemini picked index={idx}")
    if idx is None:
        print("[send_qa] ✗ No add-post button found on this page")
        return False

    el = clickables[int(idx)]
    print(f"[send_qa] Clicking: tag={el['tag']} id={el['id']!r} href={el['href']!r} text={el['text']!r}")

    if el.get("href"):
        page.goto(el["href"], wait_until="networkidle")
    elif el.get("id"):
        page.locator(f"#{el['id']}").first.click()
        page.wait_for_timeout(1000)
    else:
        page.get_by_text(el["text"], exact=False).first.click()
        page.wait_for_timeout(1000)

    page.wait_for_timeout(2000)
    print(f"[send_qa] DEBUG after click: title={page.title()!r}")
    print(f"[send_qa] DEBUG #id_subject visible: {bool(page.query_selector('#id_subject'))}")
    print(f"[send_qa] DEBUG #id_message_ifr present: {bool(page.query_selector('#id_message_ifr'))}")
    print(f"[send_qa] DEBUG #collapseAddForm present: {bool(page.query_selector('#collapseAddForm'))}")

    # Try waiting for the inline form to expand
    try:
        page.wait_for_selector("#id_subject, #id_message_ifr, textarea[name]",
                               state="visible", timeout=5_000)
        print("[send_qa] DEBUG form appeared")
    except Exception as e:
        print(f"[send_qa] DEBUG form did not appear: {e}")

    return _fill_moodle_post_form(page, subject, message, attachment)


def _is_post_form(page) -> bool:
    """True if the current page has a visible Moodle post form."""
    subj = page.query_selector("#id_subject")
    if subj and subj.is_visible():
        return True
    # Also check for the TinyMCE iframe which only appears on post forms
    ifr = page.query_selector("#id_message_ifr")
    return bool(ifr)


def _fill_moodle_post_form(page, subject, message, attachment) -> bool:
    """
    Fill and submit a Moodle forum new-post form using Playwright directly.
    Handles TinyMCE (iframe-based editor) and plain textarea fallback.
    No string comparisons, no hardcoded text — only stable Moodle HTML ids.
    """
    # Wait for the form to fully render (inline forms expand after button click)
    try:
        page.wait_for_selector("#id_subject, #id_message_ifr, textarea",
                               state="visible", timeout=5_000)
    except Exception:
        pass

    # ── Subject field ──────────────────────────────────────────────────────────
    subj = page.query_selector("#id_subject")
    if subj and subj.is_visible():
        subj.fill(subject)
        print("[send_qa] Filled subject (#id_subject)")

    # ── Message body ───────────────────────────────────────────────────────────
    filled = False

    # 1. Try TinyMCE iframe
    try:
        iframe_el = page.query_selector("#id_message_ifr")
        if iframe_el:
            frame = iframe_el.content_frame()
            if frame:
                body = frame.query_selector("body")
                if body:
                    body.click()
                    page.keyboard.press("Control+a")
                    page.keyboard.type(message)
                    print("[send_qa] Filled body via TinyMCE iframe")
                    filled = True
    except Exception as exc:
        print(f"[send_qa] TinyMCE iframe fill failed ({exc}), trying fallback")

    # 2. Fallback: contenteditable div (some Moodle themes use Atto editor)
    if not filled:
        try:
            editor = page.query_selector("div.editor_atto_content[contenteditable='true']")
            if not editor:
                editor = page.query_selector("[contenteditable='true']")
            if editor and editor.is_visible():
                editor.click()
                page.keyboard.press("Control+a")
                page.keyboard.type(message)
                print("[send_qa] Filled body via contenteditable div")
                filled = True
        except Exception as exc:
            print(f"[send_qa] contenteditable fill failed ({exc})")

    # 3. Fallback: plain textarea
    if not filled:
        try:
            ta = page.query_selector("#id_message")
            if not ta:
                ta = page.query_selector("textarea")
            if ta and ta.is_visible():
                ta.fill(message)
                print("[send_qa] Filled body via textarea")
                filled = True
        except Exception as exc:
            print(f"[send_qa] textarea fill failed ({exc})")

    if not filled:
        print("[send_qa] ✗ Could not find message body field")
        return False

    # ── Attachment ─────────────────────────────────────────────────────────────
    if attachment:
        file_el = page.query_selector("input[type=file]")
        if file_el:
            file_el.set_input_files(attachment)

    # ── Submit ─────────────────────────────────────────────────────────────────
    # Moodle forum post forms always have id="id_submitbutton"
    submitted = False
    btn = page.query_selector("#id_submitbutton")
    if btn and btn.is_visible():
        btn.click()
        print("[send_qa] Clicked #id_submitbutton")
        submitted = True

    if not submitted:
        for sel in ["input[type=submit]", "button[type=submit]"]:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                print(f"[send_qa] Clicked submit via {sel}")
                submitted = True
                break

    if not submitted:
        print("[send_qa] ✗ No submit button found")
        return False

    page.wait_for_load_state("networkidle", timeout=15_000)

    # Verify: ask Gemini to read the resulting page title
    verdict = _gemini_json(
        "A Moodle forum post was just submitted. Did it succeed or fail?\n"
        "Return JSON only: {\"success\": true} or {\"success\": false, \"reason\": \"...\"}\n"
        + json.dumps({"page_title": page.title()}, ensure_ascii=False)
    )
    if verdict.get("success"):
        return True
    print(f"[send_qa] ✗ Submission failed: {verdict.get('reason', '')}")
    return False


# ── Moodle assignment comment — direct URL navigation ─────────────────────────

def _post_assignment_comment(destinations, course, message, assignment):
    from utils.browser import new_page

    page = new_page()

    # Resolve course live — never trust destinations.json for enrolments
    _, course_url = _live_moodle_course(course, page)
    course_id_match = re.search(r"id=(\d+)", course_url)
    if not course_id_match:
        raise RuntimeError(f"Could not extract course ID from {course_url}")
    course_id = course_id_match.group(1)

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
    from utils.browser import new_page
    page = new_page()
    _, course_url = _live_moodle_course(course, page)
    # Open the course page and let the user find the chat there
    import webbrowser
    webbrowser.open(course_url)
    print(f"[send_qa] Opened course page for chat: {course_url}")
    page.close()


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

def _fetch_live_zulip_streams():
    """
    Pull the subscriptions of the CURRENTLY logged-in Zulip account.
    destinations.json is a crawl artefact of whoever ran `python -m crawlers.zulip_crawler`
    — it's almost always someone else's account.  Always go live.
    Returns a list of {"name": str, "stream_id": int} for the logged-in user.
    """
    r = requests.get(
        f"{ZULIP_SITE}/api/v1/users/me/subscriptions",
        auth=(ZULIP_EMAIL, ZULIP_API_KEY),
        timeout=15,
    )
    r.raise_for_status()
    subs = r.json().get("subscriptions", [])
    return [{"name": s["name"], "stream_id": s["stream_id"]} for s in subs]


def _resolve_stream(stream_name, live_streams):
    """
    Map a user-typed stream name to the exact stream in the logged-in
    account.  Exact (case-insensitive) match first, then substring, then
    Gemini fuzzy match as last resort.  Returns the matching dict
    {"name", "stream_id"} or raises with a helpful list.
    """
    if not live_streams:
        raise RuntimeError(
            "You have no Zulip stream subscriptions on this account "
            f"({ZULIP_EMAIL} @ {ZULIP_SITE}). Subscribe to a stream in Zulip first."
        )

    target = (stream_name or "").strip()
    if not target:
        raise RuntimeError("No stream name given.")

    # 1. exact, case-insensitive
    for s in live_streams:
        if s["name"].lower() == target.lower():
            return s
    # 2. substring, case-insensitive
    subs = [s for s in live_streams if target.lower() in s["name"].lower()]
    if len(subs) == 1:
        return subs[0]
    # 3. Gemini fuzzy match across the user's real subscriptions
    try:
        names = [s["name"] for s in live_streams]
        pick = _gemini_text(
            f'Match "{target}" to one of these Zulip streams: {names}\n'
            f'Return only the exact stream name from the list, nothing else.'
        ).strip().strip('"').strip("'")
        for s in live_streams:
            if s["name"] == pick:
                return s
    except Exception as exc:
        print(f"[send_qa] Gemini fuzzy match failed: {exc}")

    available = ", ".join(s["name"] for s in live_streams[:20])
    raise RuntimeError(
        f"No Zulip stream matches '{target}' on your account. "
        f"Your streams: {available}"
    )


def _send_zulip_stream(stream_name, topic, message, attachment):
    live_streams = _fetch_live_zulip_streams()
    target = _resolve_stream(stream_name, live_streams)

    if not topic or not topic.strip():
        raise RuntimeError("Zulip streams require a non-empty topic.")

    content = message
    if attachment:
        content += f"\n[{attachment}]({_zulip_upload(attachment)})"

    # Zulip requires `to` to be a JSON-encoded list (or a numeric stream_id).
    # Passing a bare stream-name string was the source of the 400.
    # We send the numeric stream_id — it's unambiguous and avoids Unicode issues.
    r = requests.post(
        f"{ZULIP_SITE}/api/v1/messages",
        auth=(ZULIP_EMAIL, ZULIP_API_KEY),
        data={
            "type":    "stream",
            "to":      target["stream_id"],
            "topic":   topic.strip(),
            "content": content,
        },
        timeout=15,
    )
    if not r.ok:
        # Surface Zulip's actual error body instead of a bare 400.
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise RuntimeError(
            f"Zulip rejected the message ({r.status_code}): {err}"
        )
    print(f"[send_qa] ✓ Posted to '{target['name']}' / '{topic}'.")


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
    """
    Show the streams of the CURRENTLY logged-in Zulip account (live from
    /api/v1/users/me/subscriptions).  destinations.json is a per-developer
    crawl artefact, so it must not be the source of truth here.
    """
    try:
        streams = _fetch_live_zulip_streams()
    except Exception as exc:
        print(f"[send_qa] Could not fetch your Zulip streams ({exc}).")
        streams = []

    if streams:
        print("\nYour Zulip streams:")
        for i, s in enumerate(streams[:30]):
            print(f"  [{i+1}] {s['name']}")
        val = input("Pick number or name: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(streams):
            return streams[int(val) - 1]["name"]
        return val
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
    """Legacy helper — only used for fallback paths. Prefer _live_moodle_course."""
    for name, data in destinations.items():
        if fragment and fragment.lower() in name.lower():
            return data
    raise RuntimeError(f"Course '{fragment}' not found. Available: {[k for k in destinations if not k.startswith('_')]}")


def _live_moodle_course(course_query: str, page) -> tuple:
    """
    Find the Moodle course URL for the currently logged-in user.

    Scrapes the live dashboard (/my/) — never destinations.json — so it
    always reflects the real enrolments of whoever is logged in.
    Uses Gemini for fuzzy matching, no string comparisons.

    Returns (matched_course_name, course_url).
    """
    from config import MOODLE_BASE

    print(f"[send_qa] Looking up '{course_query}' on live Moodle dashboard...")
    page.goto(f"{MOODLE_BASE}/my/", wait_until="networkidle")

    courses = page.evaluate("""() => {
        const seen = new Set();
        const results = [];
        for (const a of document.querySelectorAll('a[href*="/course/view.php"]')) {
            const url = a.href.split('?')[0] + '?' + a.href.split('?')[1];
            if (seen.has(a.href)) continue;
            seen.add(a.href);
            const name = (a.textContent || '').trim().replace(/ +/g, ' ').trim();
            if (name) results.push({name: name.slice(0, 200), url: a.href});
        }
        return results;
    }""")

    if not courses:
        raise RuntimeError(
            "No courses found on your Moodle dashboard. "
            "Make sure you are logged in and have enrolled courses."
        )

    print(f"[send_qa] Found {len(courses)} courses on dashboard")

    course_list = "\n".join(f"{i}: {c['name']}" for i, c in enumerate(courses))
    pick = _gemini_json(
        f'A TUM student wants to find the course: "{course_query}"\n\n'
        f'Their enrolled Moodle courses:\n{course_list}\n\n'
        f'Pick the single best match (fuzzy — e.g. "Modelbildung" matches '
        f'"Modelbildung und Simulation (IN2023)").\n'
        f'Return JSON only: {{"index": <number>, "reason": "<why>"}}'
    )

    idx = int(pick.get("index", 0))
    if not (0 <= idx < len(courses)):
        idx = 0

    target = courses[idx]
    print(f"[send_qa] Matched '{course_query}' → '{target['name']}' ({pick.get('reason', '')})")
    return target["name"], target["url"]
