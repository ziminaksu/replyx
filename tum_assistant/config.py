"""
config.py — all configuration pulled from the secure credential store.

Do NOT hardcode credentials here. Run `python main.py setup` instead.
"""
import sys as _sys
from pathlib import Path

BASE_DIR          = Path(__file__).parent
DESTINATIONS_FILE = BASE_DIR / "destinations.json"
SLIDES_DIR        = BASE_DIR / "slides"
SLIDES_DB         = BASE_DIR / "slides_db"
EMBED_MODEL       = "nomic-ai/nomic-embed-text-v1"


def _cred(key: str) -> str:
    from utils.credentials import get
    return get(key)


class _Config(_sys.modules[__name__].__class__):
    """Module-level properties so `from config import TUM_USERNAME` works lazily."""
    @property
    def TUM_USERNAME(self):     return _cred("tum_user")
    @property
    def TUM_PASSWORD(self):     return _cred("tum_password")
    @property
    def ZULIP_EMAIL(self):      return _cred("zulip_email")
    @property
    def ZULIP_API_KEY(self):    return _cred("zulip_api_key")
    @property
    def ZULIP_SITE(self):       return _cred("zulip_site")
    @property
    def STUDENT_NAME(self):     return _cred("student_name")
    @property
    def MATRIKELNUMMER(self):   return _cred("matrikelnummer")
    @property
    def MOODLE_BASE(self):      return "https://www.moodle.tum.de"
    @property
    def TUM_ONLINE_BASE(self):  return "https://campus.tum.de"

_sys.modules[__name__].__class__ = _Config
