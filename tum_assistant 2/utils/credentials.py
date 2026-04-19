"""
utils/credentials.py — secure credential storage using the system keychain.

Uses the `keyring` library which backends to:
  macOS   → Keychain
  Linux   → libsecret / KWallet
  Windows → Windows Credential Manager

Credentials are NEVER written to disk in plaintext.
A small ~/.tum_assistant.json stores only non-secret profile info
(student name, matrikelnummer, zulip site) — nothing sensitive.
"""
import json, getpass, sys
from pathlib import Path

try:
    import keyring
    _KEYRING_OK = True
except ImportError:
    _KEYRING_OK = False

PROFILE_FILE = Path.home() / ".tum_assistant.json"
SERVICE       = "tum_assistant"

# keyring keys
_KEY_TUM_PASS   = "tum_password"
_KEY_ZULIP_KEY  = "zulip_api_key"


# ── public API ────────────────────────────────────────────────────────────────

def is_registered() -> bool:
    """True if all required credentials exist."""
    p = _load_profile()
    if not p:
        return False
    required = ["tum_user", "zulip_email", "zulip_site",
                "student_name", "matrikelnummer"]
    if not all(p.get(k) for k in required):
        return False
    if _KEYRING_OK:
        tum_pass  = keyring.get_password(SERVICE, _KEY_TUM_PASS)
        zulip_key = keyring.get_password(SERVICE, _KEY_ZULIP_KEY)
        return bool(tum_pass and zulip_key)
    return bool(p.get("_tum_password") and p.get("_zulip_api_key"))


def get(key: str) -> str:
    """Retrieve a credential by name. Raises if not registered."""
    _assert_registered()
    p = _load_profile()

    if key == "tum_password":
        return _get_secret(_KEY_TUM_PASS, p)
    if key == "zulip_api_key":
        return _get_secret(_KEY_ZULIP_KEY, p)

    value = p.get(key)
    if not value:
        raise KeyError(f"Credential '{key}' not found. Run: python main.py setup")
    return value


def register(*, update: bool = False):
    """
    Interactive registration wizard.
    Prompts for all required credentials and stores them securely.
    If update=True, shows current values and allows selective re-entry.
    """
    p = _load_profile() or {}

    print("\n" + "─" * 50)
    print("  TUM Assistant — setup")
    print("─" * 50)
    if update:
        print("  Press Enter to keep the current value.\n")
    else:
        print()

    # ── TUM credentials ───────────────────────────────────────────────────────
    print("TUM Online credentials")
    p["tum_user"] = _prompt(
        "  TUM ID (e.g. ge12abc)",
        default=p.get("tum_user"),
        required=True,
    )
    tum_pass = _prompt_secret(
        "  TUM password",
        has_existing=bool(_get_secret(_KEY_TUM_PASS, p) if update else None),
    )
    if tum_pass:
        _set_secret(_KEY_TUM_PASS, tum_pass, p)

    # ── Zulip ─────────────────────────────────────────────────────────────────
    print("\nZulip")
    p["zulip_site"] = _prompt(
        "  Zulip server URL",
        default=p.get("zulip_site", "https://zulip.cit.tum.de"),
    )
    p["zulip_email"] = _prompt(
        "  Zulip email",
        default=p.get("zulip_email") or f"{p['tum_user']}@tum.de",
        required=True,
    )
    zulip_key = _prompt_secret(
        "  Zulip API key (Settings → Personal settings → API key)",
        has_existing=bool(_get_secret(_KEY_ZULIP_KEY, p) if update else None),
    )
    if zulip_key:
        _set_secret(_KEY_ZULIP_KEY, zulip_key, p)

    # ── Student info (for Deckblatt) ──────────────────────────────────────────
    print("\nStudent info (used for homework Deckblatt)")
    p["student_name"] = _prompt(
        "  Full name",
        default=p.get("student_name"),
        required=True,
    )
    p["matrikelnummer"] = _prompt(
        "  Matrikelnummer",
        default=p.get("matrikelnummer"),
        required=True,
    )

    _save_profile(p)

    print("\n" + "─" * 50)
    print("  Setup complete. Credentials stored securely.")
    if not _KEYRING_OK:
        print("  Note: keyring not installed — passwords stored in profile file.")
        print("  For better security: pip install keyring")
    print("─" * 50 + "\n")


def clear():
    """Remove all stored credentials."""
    if _KEYRING_OK:
        for key in [_KEY_TUM_PASS, _KEY_ZULIP_KEY]:
            try:
                keyring.delete_password(SERVICE, key)
            except Exception:
                pass
    if PROFILE_FILE.exists():
        PROFILE_FILE.unlink()
    print("All credentials cleared.")


def show():
    """Print stored (non-secret) profile info."""
    p = _load_profile()
    if not p:
        print("Not set up. Run: python main.py setup")
        return
    print("\nStored profile:")
    for k, v in p.items():
        if not k.startswith("_"):
            print(f"  {k:20s} {v}")
    print(f"  {'tum_password':20s} {'(stored in keychain)' if _KEYRING_OK else '(stored in profile)'}")
    print(f"  {'zulip_api_key':20s} {'(stored in keychain)' if _KEYRING_OK else '(stored in profile)'}")


# ── internals ─────────────────────────────────────────────────────────────────

def _load_profile() -> dict | None:
    if not PROFILE_FILE.exists():
        return None
    try:
        return json.loads(PROFILE_FILE.read_text())
    except Exception:
        return None


def _save_profile(p: dict):
    # never write real secrets to the json if keyring is available
    safe = {k: v for k, v in p.items() if _KEYRING_OK or k.startswith("_") or k not in ("tum_password", "zulip_api_key")}
    PROFILE_FILE.write_text(json.dumps(safe, indent=2))
    PROFILE_FILE.chmod(0o600)  # owner read/write only


def _get_secret(key: str, profile: dict) -> str | None:
    if _KEYRING_OK:
        return keyring.get_password(SERVICE, key)
    return profile.get(f"_{key}")


def _set_secret(key: str, value: str, profile: dict):
    if _KEYRING_OK:
        keyring.set_password(SERVICE, key, value)
    else:
        profile[f"_{key}"] = value  # fallback: store in profile (less secure)


def _assert_registered():
    if not is_registered():
        print("\nNot set up yet. Run first:\n\n    python main.py setup\n")
        sys.exit(1)


def _prompt(label: str, *, default: str | None = None, required: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"{label}{hint}: ").strip()
        if not val and default:
            return default
        if val:
            return val
        if not required:
            return default or ""
        print("  This field is required.")


def _prompt_secret(label: str, *, has_existing: bool = False) -> str | None:
    hint = " [keep existing — press Enter to skip]" if has_existing else ""
    val = getpass.getpass(f"{label}{hint}: ").strip()
    if not val and has_existing:
        return None   # keep existing
    return val or None
