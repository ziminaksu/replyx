"""
understand.py — parse a natural language user message into a structured action.

Single-turn:  parse_intent(message)
Multi-turn:   parse_intent_with_history(messages)  ← new, powers the chat UI

Instead of menus, the user just types what they want:
  "ask my Algo tutor Müller about HW3 task 2"
  "post in the Moodle forum for ML that the deadline is unclear"
  "send a DM to Prof. Schmidt asking about the exam"

When Gemini can't resolve who/what is meant, it returns
  {"action": "clarify", "question": "Which Müller do you mean — ...?"}
and the caller shows that question back to the user in the chat UI.
"""
import json, os
from config import DESTINATIONS_FILE


# ── shared context builder ────────────────────────────────────────────────────

def _build_context() -> str:
    """Summarise destinations.json so Gemini knows what courses/people exist."""
    if not DESTINATIONS_FILE.exists():
        return "{}"
    data = json.loads(DESTINATIONS_FILE.read_text())
    summary = {}
    for course, info in data.items():
        if course.startswith("_"):
            continue
        participants = info.get("moodle", {}).get("participants", [])
        non_students = [p["name"] for p in participants
                        if p.get("role", "").lower() not in ("student", "")]
        forums = [f["name"] for f in info.get("moodle", {}).get("forums", [])]
        summary[course] = {
            "tutors_profs": non_students[:10],
            "forums": forums,
        }
    zulip_streams = [s["name"] for s in data.get("_zulip", {}).get("streams", [])]
    return json.dumps({"courses": summary, "zulip_streams": zulip_streams}, ensure_ascii=False)


# ── prompt template shared by both entry points ───────────────────────────────

_SYSTEM_TMPL = """\
You are a TUM student assistant that parses natural language commands.

The student has these courses, tutors/profs, and Zulip streams available:
{context}

Parse the request into a JSON action. Return ONLY valid JSON, no explanation.

Possible actions and their fields:

1. Sending a question/message:
{{"action": "qa", "course": "<best matching course name from above>",
  "dest_type": "<one of: dm, forum, stream, group_chat>",
  "person": "<name if dm>",
  "stream": "<stream name if stream>", "topic": "<topic if stream>",
  "forum_name": "<specific forum or activity name if dest_type is forum, else omit>",
  "subject": "<subject/title of the post if dest_type is forum, else omit>",
  "message": "<the actual message text to send>"}}

2. Submitting homework:
{{"action": "hw", "course": "<course>", "sheet": "<sheet name/number>",
  "file": "<filename if mentioned>"}}

3. Booking a room:
{{"action": "room", "date": "<YYYY-MM-DD>", "duration": <hours as int>}}

4. Searching slides:
{{"action": "search", "query": "<search query>"}}

5. If still unclear after the full conversation, ask ONE focused question:
{{"action": "clarify", "question": "<exactly what you need to know>"}}

Rules:
- dest_type "dm" = direct message to a specific person on Zulip
- dest_type "forum" = post to Moodle discussion forum
- dest_type "stream" = post to a Zulip stream/channel
- Match course names fuzzily (e.g. "Algo" → "Algorithmen und Datenstrukturen")
- Match person names fuzzily (e.g. "Müller" → "Dr. Hans Müller")
- The "message" field should be the actual text to send, written naturally
- If no file is mentioned for hw, omit the file field
- For "dm" type, course is optional — don't ask for it if person + message are clear
- For assignment comments: {{"action": "qa", "course": "...",
    "dest_type": "assignment_comment", "message": "...", "assignment": "<hw number>"}}
- For forum posts: extract the forum_name (e.g. "Übungsblatt discussion", "Diskussionsforum"),
  the subject/title (e.g. "Question about Abgabeblatt 6"), and the message body separately.
  If the user hasn't specified a forum name, omit forum_name.
  If the user hasn't specified a subject, generate a short descriptive one from the message.
- If the person or forum is AMBIGUOUS (multiple matches), return action=clarify
  with a concrete question that lists the options, e.g.
  "Which Müller do you mean — Dr. Hans Müller (tutor) or Prof. Anna Müller (lecturer)?"
- Only return clarify when truly needed; if one interpretation is clearly most likely,
  go with it.\
"""


# ── public API ────────────────────────────────────────────────────────────────

def parse_intent(user_message: str) -> dict:
    """
    Single-turn: parse one message into a structured action dict.
    """
    context = _build_context()
    prompt = _SYSTEM_TMPL.format(context=context) + f'\n\nStudent message: "{user_message}"'
    return _call_gemini(prompt)


def parse_intent_with_history(messages: list) -> dict:
    """
    Multi-turn intent parsing.

    messages = [{"role": "user"|"assistant", "content": "..."}]

    Gemini sees the full conversation so it can resolve ambiguity from
    previous clarifying answers.  Returns the same shape as parse_intent —
    possibly {"action": "clarify", "question": "..."} if still unclear.
    """
    context = _build_context()
    system = _SYSTEM_TMPL.format(context=context)

    history_lines = []
    for m in messages:
        speaker = "Student" if m["role"] == "user" else "Assistant"
        history_lines.append(f"{speaker}: {m['content']}")
    history = "\n".join(history_lines)

    prompt = (
        f"{system}\n\n"
        f"Conversation so far:\n{history}\n\n"
        "Based on the full conversation above, return the resolved intent as JSON.\n"
        "If you still cannot determine a required field, return "
        "{\"action\": \"clarify\", \"question\": \"...\"}."
    )
    return _call_gemini(prompt)


# ── Gemini call ───────────────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> dict:
    import requests, re, time
    api_key = os.environ["GEMINI_API_KEY"]
    GEMINI_API = (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/gemini-2.5-flash:generateContent"
    )

    for attempt in range(3):
        resp = requests.post(
            f"{GEMINI_API}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2000, "temperature": 0},
            },
        )
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"[bot] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 503:
            time.sleep(5)
            continue
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        clean = re.sub(r"```json|```", "", raw).strip()
        print(f"[debug] raw response: {clean[:500]}")
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            print("[bot] Bad JSON from Gemini, retrying...")
            time.sleep(2)
            continue

    raise RuntimeError("Failed to get valid response from Gemini after 3 attempts.")


# ── CLI confirmation helper (used by main.py / send_qa CLI flow) ──────────────

def confirm_intent(intent: dict) -> bool:
    """
    Show the user what we're about to do and ask for confirmation.
    Returns True if confirmed, False if cancelled.
    """
    action = intent.get("action")

    if action == "qa":
        dest_type = intent.get("dest_type")
        if dest_type == "dm":
            summary = f"Send a Zulip DM to {intent.get('person')} ({intent.get('course')})"
        elif dest_type == "forum":
            summary = f"Post to the Moodle forum for {intent.get('course')}"
        elif dest_type == "stream":
            summary = (
                f"Post to Zulip stream '{intent.get('stream')}'"
                f" / topic '{intent.get('topic')}'"
            )
        else:
            summary = f"Send message via {dest_type} for {intent.get('course')}"
        print(f"\n[bot] I'm about to: {summary}")
        print(f"[bot] Message: \"{intent.get('message')}\"")

    elif action == "hw":
        print(
            f"\n[bot] I'm about to submit '{intent.get('file')}'"
            f" as {intent.get('sheet')} for {intent.get('course')}"
        )

    elif action == "room":
        print(
            f"\n[bot] I'm about to book a room on {intent.get('date')}"
            f" for {intent.get('duration')} hours"
        )

    reply = input("[bot] Confirm? (yes / no): ").strip().lower()
    return reply in ("yes", "y")
