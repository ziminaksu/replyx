"""
understand.py — parse a natural language user message into a structured action.

Instead of menus, the user just types what they want:
  "ask my Algo tutor Müller about HW3 task 2"
  "post in the Moodle forum for ML that the deadline is unclear"
  "send a DM to Prof. Schmidt asking about the exam"

Gemini reads the message + the user's destinations.json context and returns
a structured JSON action that the rest of the system can execute.
"""
import json, os
from config import DESTINATIONS_FILE


def parse_intent(user_message: str) -> dict:
    """
    Parse a natural language message into a structured action dict.

    Returns something like:
    {
        "action": "qa",
        "course": "Algorithmen und Datenstrukturen",
        "dest_type": "dm",
        "person": "Müller",
        "message": "Hi, I have a question about HW3 task 2..."
    }
    Or:
    {
        "action": "hw",
        "course": "Algorithmen",
        "sheet": "Sheet 4",
        "file": "hw4.pdf"
    }
    """
    # Load destinations so Gemini knows what courses/people exist
    context = ""
    if DESTINATIONS_FILE.exists():
        data = json.loads(DESTINATIONS_FILE.read_text())
        # Summarize: just course names and participant names (not full JSON)
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
        context = json.dumps({"courses": summary, "zulip_streams": zulip_streams}, ensure_ascii=False)

    prompt = f"""You are a TUM student assistant that parses natural language commands.

The student has these courses, tutors/profs, and Zulip streams available:
{context}

Student message: "{user_message}"

Parse this into a JSON action. Return ONLY valid JSON, no explanation.

Possible actions and their fields:

1. Sending a question/message:
{{"action": "qa", "course": "<best matching course name from above>", "dest_type": "<one of: dm, forum, stream, group_chat>", "person": "<name if dm>", "stream": "<stream name if stream>", "topic": "<topic if stream>", "message": "<the actual message text to send>"}}

2. Submitting homework:
{{"action": "hw", "course": "<course>", "sheet": "<sheet name/number>", "file": "<filename if mentioned>"}}

3. Booking a room:
{{"action": "room", "date": "<YYYY-MM-DD>", "duration": <hours as int>}}

4. Searching slides:
{{"action": "search", "query": "<search query>"}}

5. If unclear, ask for clarification:
{{"action": "clarify", "question": "<what you need to know>"}}

Rules:
- dest_type "dm" = direct message to a specific person on Zulip
- dest_type "forum" = post to Moodle discussion forum
- dest_type "stream" = post to a Zulip stream/channel
- Match course names fuzzily (e.g. "Algo" → "Algorithmen und Datenstrukturen")
- Match person names fuzzily (e.g. "Müller" → "Dr. Hans Müller")
- The "message" field should be the actual text to send, written naturally
- If no file is mentioned for hw, omit the file field
- For "dm" type, course is optional — don't ask for it if the person and message are clear
- If the user wants to respond to/comment on a submitted homework, use:
  {{"action": "qa", "course": "...", "dest_type": "assignment_comment", "message": "...", "assignment": "<hw number or name>"}}
"""

    return _call_gemini(prompt)


def _call_gemini(prompt: str) -> dict:
    import requests, re, time
    api_key = os.environ["GEMINI_API_KEY"]
    GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

    for attempt in range(3):
        resp = requests.post(
            f"{GEMINI_API}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2000, "temperature": 0},
            }
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
            # Gemini returned truncated JSON — increase tokens and retry
            print(f"[bot] Bad JSON from Gemini, retrying...")
            time.sleep(2)
            continue

    raise RuntimeError("Failed to get valid response from Gemini after 3 attempts.")

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
            summary = f"Post to Zulip stream '{intent.get('stream')}' / topic '{intent.get('topic')}'"
        else:
            summary = f"Send message via {dest_type} for {intent.get('course')}"
        print(f"\n[bot] I'm about to: {summary}")
        print(f"[bot] Message: \"{intent.get('message')}\"")

    elif action == "hw":
        print(f"\n[bot] I'm about to submit '{intent.get('file')}' as {intent.get('sheet')} for {intent.get('course')}")

    elif action == "room":
        print(f"\n[bot] I'm about to book a room on {intent.get('date')} for {intent.get('duration')} hours")

    reply = input("[bot] Confirm? (yes / no): ").strip().lower()
    return reply in ("yes", "y")


