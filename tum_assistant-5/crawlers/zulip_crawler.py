"""
crawlers/zulip_crawler.py — Phase 1 discovery for Zulip.

Uses the Zulip REST API (no browser needed) to map:
  - all streams you're subscribed to
  - all users (for finding tutors/profs by name)

Run once:  python -m crawlers.zulip_crawler
"""
import json, requests
from config import ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE, DESTINATIONS_FILE


def _api(endpoint: str) -> dict:
    r = requests.get(
        f"{ZULIP_SITE}/api/v1/{endpoint}",
        auth=(ZULIP_EMAIL, ZULIP_API_KEY),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def crawl() -> dict:
    print("[zulip] Fetching subscribed streams...")
    streams_data = _api("users/me/subscriptions")
    streams = [
        {
            "name":        s["name"],
            "stream_id":   s["stream_id"],
            "description": s.get("description", ""),
        }
        for s in streams_data.get("subscriptions", [])
    ]

    print("[zulip] Fetching all users...")
    users_data = _api("users")
    users = [
        {
            "name":       u["full_name"],
            "email":      u["email"],
            "user_id":    u["user_id"],
            "role":       u.get("role", 400),   # 100=owner,200=admin,300=mod,400=member
        }
        for u in users_data.get("members", [])
        if not u.get("is_bot", False)
    ]

    return {"streams": streams, "users": users}


def find_user(name_fragment: str) -> list[dict]:
    """Helper: search users by name fragment from saved data."""
    data = json.loads(DESTINATIONS_FILE.read_text()) if DESTINATIONS_FILE.exists() else {}
    zulip = data.get("_zulip", {})
    fragment = name_fragment.lower()
    return [u for u in zulip.get("users", []) if fragment in u["name"].lower()]


def find_stream(name_fragment: str) -> list[dict]:
    data = json.loads(DESTINATIONS_FILE.read_text()) if DESTINATIONS_FILE.exists() else {}
    zulip = data.get("_zulip", {})
    fragment = name_fragment.lower()
    return [s for s in zulip.get("streams", []) if fragment in s["name"].lower()]


def run():
    result = crawl()
    data = {}
    if DESTINATIONS_FILE.exists():
        data = json.loads(DESTINATIONS_FILE.read_text())
    data["_zulip"] = result
    DESTINATIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[zulip] {len(result['streams'])} streams, {len(result['users'])} users saved.")


if __name__ == "__main__":
    run()
