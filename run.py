"""
ReplyX — unified launcher for the project.
Usage:
    python run.py
Does three things:
    1. Starts the existing FastAPI backend (slides + homework).
    2. Adds /api/send-message — a thin wrapper around tum_assistant.send_qa
       (send_qa itself is not modified, only called).
    3. Serves a single HTML interface at / and opens it in the browser.
"""
from __future__ import annotations
import os, sys, io, json, threading, webbrowser, builtins, traceback, time
from pathlib import Path

# ────────────────────────────────────────────────────────────────
# 0. Paths / sys.path / .env
# ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
TUM_DIR     = ROOT / "tum_assistant"
BACKEND_DIR = ROOT / "backend"
DATA_DIR    = ROOT / "data"
CHROMA_DIR  = ROOT / "chroma_db"

# Add tum_assistant and root to sys.path so both tum modules
# and backend (from backend.main import app) can be imported.
sys.path.insert(0, str(TUM_DIR))
sys.path.insert(0, str(ROOT))

# backend must be a package (it uses `from .hw_agent import ...`)
(BACKEND_DIR / "__init__.py").touch(exist_ok=True)

# GEMINI_API_KEY lives in tum_assistant/.env
try:
    from dotenv import load_dotenv
    load_dotenv(TUM_DIR / ".env")
except Exception:
    pass

# Ensure required subdirectories exist
(DATA_DIR / "slides").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(exist_ok=True)


# ────────────────────────────────────────────────────────────────
# 1. Import the existing FastAPI app from backend
# ────────────────────────────────────────────────────────────────
print("[ReplyX] Loading backend …")
try:
    from backend.main import app  # re-use the existing app
except Exception as e:
    print(f"[ReplyX] backend.main failed to import: {e}")
    traceback.print_exc()
    print("[ReplyX] Creating minimal fallback app.")
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    app = FastAPI(title="ReplyX (fallback)")
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

from fastapi import Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel


# ────────────────────────────────────────────────────────────────
# 2. Helper — safe send_qa call without modifying send_qa
# ────────────────────────────────────────────────────────────────
class _StubInput:
    """
    Replaces builtins.input when send_qa is called from the API.
    If input() is still triggered (e.g. pick number / subject),
    returns the next answer from the queue. Returns empty string if queue is empty.
    """
    def __init__(self, answers):
        self.answers = list(answers)
    def __call__(self, prompt: str = "") -> str:
        return self.answers.pop(0) if self.answers else ""


def _send_qa_worker(params: dict, out: dict):
    """
    Actual send_qa call. Runs in a separate OS thread without an
    asyncio event loop — Playwright sync API breaks inside FastAPI otherwise.
    """
    old_stdout = sys.stdout
    old_input  = builtins.input
    buf = io.StringIO()
    sys.stdout = buf
    preset = params.pop("_input_queue", [])
    builtins.input = _StubInput(preset)

    try:
        # Lazy import inside the worker thread — tum_assistant.config
        # lazily loads credentials and may sys.exit(1) if missing.
        try:
            from send_qa import send_qa  # noqa: E402
        except SystemExit:
            out.update({
                "status":  "error",
                "message": "Credentials not configured. Open the Setup tab and save them.",
                "log":     buf.getvalue(),
            })
            return
        except Exception as exc:
            out.update({
                "status":  "error",
                "message": f"tum_assistant import failed: {exc}",
                "log":     buf.getvalue(),
                "trace":   traceback.format_exc(),
            })
            return

        try:
            send_qa(
                course      = params.get("course"),
                dest_type   = params.get("dest_type"),
                message     = params.get("message"),
                person      = params.get("person"),
                stream      = params.get("stream"),
                topic       = params.get("topic") or "General",
                assignment  = params.get("assignment"),
                attachment  = params.get("attachment"),
            )
            out.update({"status": "ok", "log": buf.getvalue()})
        except SystemExit:
            out.update({
                "status":  "error",
                "message": "Credentials not configured. Open the Setup tab and save them.",
                "log":     buf.getvalue(),
            })
        except Exception as exc:
            out.update({
                "status":  "error",
                "message": str(exc),
                "log":     buf.getvalue(),
                "trace":   traceback.format_exc(),
            })
    finally:
        sys.stdout     = old_stdout
        builtins.input = old_input


def _run_send_qa(params: dict) -> dict:
    """
    Runs send_qa in a clean thread — no event loop.
    Required because _post_moodle_forum / submit_hw use
    Playwright sync API which does not work inside an asyncio loop
    (FastAPI runs handlers in an anyio thread pool that has a bound loop).
    """
    out: dict = {}
    t = threading.Thread(
        target=_send_qa_worker,
        args=(params, out),
        daemon=True,
        name="send_qa_worker",
    )
    t.start()
    # Moodle login + page load can take a while — allow enough time.
    t.join(timeout=180)
    if t.is_alive():
        return {"status": "error",
                "message": "send_qa timed out after 180 s"}
    return out or {"status": "error", "message": "unknown error"}


# ────────────────────────────────────────────────────────────────
# 3. New endpoints: send messages + intent parsing
# ────────────────────────────────────────────────────────────────
class SendMessageRequest(BaseModel):
    dest_type:  str           # "dm" | "stream" | "forum" | "group_chat" | "assignment_comment"
    message:    str | None = None
    course:     str | None = None
    person:     str | None = None
    stream:     str | None = None
    topic:      str | None = "General"
    subject:    str | None = None   # subject/title for Moodle forum posts
    forum_name: str | None = None   # specific forum/activity name hint
    assignment: str | None = None
    attachment: str | None = None


@app.post("/api/send-message")
def api_send_message(req: SendMessageRequest):
    """Thin wrapper around tum_assistant.send_qa (send_qa is not modified)."""
    params = req.model_dump()
    # subject is only needed for forum and is fetched via input() inside send_qa
    subject = params.pop("subject", None)
    input_queue = []
    if req.dest_type == "forum" and subject:
        input_queue.append(subject)
    params["_input_queue"] = input_queue
    return _run_send_qa(params)


class ParseIntentRequest(BaseModel):
    message: str


@app.post("/api/parse-intent")
def api_parse_intent(req: ParseIntentRequest):
    """Single-turn: natural language → intent (backward compat)."""
    try:
        from understand import parse_intent
        return {"status": "ok", "intent": parse_intent(req.message)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


class ChatIntentRequest(BaseModel):
    messages: list   # [{"role": "user"|"assistant", "content": "..."}]


@app.post("/api/chat-intent")
def api_chat_intent(req: ChatIntentRequest):
    """
    Multi-turn intent parsing.
    Accepts the full conversation history and returns either:
      {"status": "clarify", "question": "..."}   — Gemini needs more info
      {"status": "resolved", "intent": {...}}     — ready to execute
    """
    try:
        from understand import parse_intent_with_history
        intent = parse_intent_with_history(req.messages)
        if intent.get("action") == "clarify":
            return {"status": "clarify", "question": intent.get("question", "Can you clarify?")}
        return {"status": "resolved", "intent": intent}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


class SetupRequest(BaseModel):
    tum_user:       str | None = None
    tum_password:   str | None = None
    zulip_site:     str | None = None
    zulip_email:    str | None = None
    zulip_api_key:  str | None = None
    student_name:   str | None = None
    matrikelnummer: str | None = None


@app.post("/api/setup")
def api_setup(req: SetupRequest):
    """Saves credentials via tum_assistant.utils.credentials (keyring + profile)."""
    try:
        from utils.credentials import (
            _load_profile, _save_profile, _set_secret,
            _KEY_TUM_PASS, _KEY_ZULIP_KEY,
        )
        p = _load_profile() or {}
        for key in ("tum_user", "zulip_site", "zulip_email",
                    "student_name", "matrikelnummer"):
            v = getattr(req, key)
            if v: p[key] = v
        if req.tum_password:  _set_secret(_KEY_TUM_PASS,  req.tum_password,  p)
        if req.zulip_api_key: _set_secret(_KEY_ZULIP_KEY, req.zulip_api_key, p)
        _save_profile(p)
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/setup-status")
def api_setup_status():
    try:
        from utils.credentials import is_registered, _load_profile
        p = _load_profile() or {}
        return {
            "registered":     is_registered(),
            "tum_user":       p.get("tum_user", ""),
            "zulip_email":    p.get("zulip_email", ""),
            "zulip_site":     p.get("zulip_site", ""),
            "student_name":   p.get("student_name", ""),
            "matrikelnummer": p.get("matrikelnummer", ""),
        }
    except Exception:
        return {"registered": False}


@app.get("/api/destinations")
def api_destinations():
    """
    Returns Zulip stream subscriptions (live API call) for the stream autocomplete.
    Courses are intentionally not pre-fetched here — Playwright is not thread-safe
    and the browser context is shared with send_qa. Course matching always happens
    live inside send_qa via _live_moodle_course at send time.
    """
    streams: list[str] = []
    try:
        import requests as _rq
        from config import ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE
        if ZULIP_EMAIL and ZULIP_API_KEY and ZULIP_SITE:
            r = _rq.get(
                f"{ZULIP_SITE}/api/v1/users/me/subscriptions",
                auth=(ZULIP_EMAIL, ZULIP_API_KEY),
                timeout=10,
            )
            if r.ok:
                streams = [s["name"] for s in r.json().get("subscriptions", [])]
    except Exception as exc:
        print(f"[ReplyX] live Zulip streams fetch failed: {exc}")

    return {"courses": [], "streams": streams}



@app.get("/api/hw-courses")
def api_hw_courses():
    """Return the list of courses the user is enrolled in on Moodle."""
    def _worker(out):
        try:
            from utils.browser import new_page
            from config import MOODLE_BASE
            page = new_page()
            page.goto(f"{MOODLE_BASE}/my/", wait_until="networkidle")
            courses = page.evaluate("""() => {
                const seen = new Set();
                const results = [];
                for (const a of document.querySelectorAll('a[href*="/course/view.php"]')) {
                    if (seen.has(a.href)) continue;
                    seen.add(a.href);
                    const name = (a.textContent || '').replace(/ +/g, ' ').trim();
                    if (name) results.push({name: name.slice(0, 200), url: a.href});
                }
                return results;
            }""")
            page.close()
            out["courses"] = courses
        except Exception as exc:
            out["error"] = str(exc)
    out = {}
    t = threading.Thread(target=_worker, args=(out,), daemon=True)
    t.start()
    t.join(timeout=60)
    if "error" in out:
        return JSONResponse({"status": "error", "message": out["error"]}, status_code=500)
    return {"status": "ok", "courses": out.get("courses", [])}


@app.get("/api/hw-assignments")
def api_hw_assignments(course_url: str):
    """Return all assignment submission links for a given Moodle course URL."""
    def _worker(out):
        try:
            from utils.browser import new_page
            page = new_page()
            page.goto(course_url, wait_until="networkidle")
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
                        /\/mod\/assign\//i.test(node.href)) {
                        const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
                        if (!text) continue;
                        const seen_key = node.href;
                        results.push({
                            section: currentSection,
                            text: text.slice(0, 200),
                            url: node.href,
                        });
                    }
                }
                const seen = new Set();
                return results.filter(it => {
                    if (seen.has(it.url)) return false;
                    seen.add(it.url); return true;
                });
            }""")
            page.close()
            out["items"] = items
        except Exception as exc:
            out["error"] = str(exc)
    out = {}
    t = threading.Thread(target=_worker, args=(out,), daemon=True)
    t.start()
    t.join(timeout=60)
    if "error" in out:
        return JSONResponse({"status": "error", "message": out["error"]}, status_code=500)
    return {"status": "ok", "assignments": out.get("items", [])}



@app.post("/api/submit-homework")
async def api_submit_homework(
    file: UploadFile = File(...),
    course_name: str = Form(...),
    assignment_url: str = Form(...),
    assignment_text: str = Form(""),
):
    """Save uploaded PDF, then upload it to the Moodle assignment URL directly."""
    import tempfile, shutil
    from pathlib import Path

    # Save the upload to a temp file
    suffix = Path(file.filename).suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        pdf_path = tmp.name
    except Exception as exc:
        return JSONResponse({"status": "error", "message": f"File save failed: {exc}"})

    out: dict = {}
    log_buf = []

    def _worker():
        import io, sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            from utils.browser import new_page
            page = new_page()
            page.goto(assignment_url, wait_until="networkidle")
            print(f"[submit_hw] Opened: {page.title()}")

            # Click "Add submission" / "Edit submission" if needed
            file_input = page.query_selector("input[type='file']")
            if not file_input:
                # Find and click any visible button that opens the submission form
                clicked = page.evaluate("""() => {
                    for (const el of document.querySelectorAll('a[href], button')) {
                        if (el.offsetParent === null) continue;
                        const href = el.href || '';
                        if (/action=(editsubmission|edit)/.test(href) ||
                            el.name === 'edit' || el.name === 'editsubmission') {
                            el.click(); return true;
                        }
                    }
                    return false;
                }""")
                if clicked:
                    page.wait_for_load_state("networkidle")
                    file_input = page.query_selector("input[type='file']")

            if not file_input:
                # Try navigating to the edit URL directly
                edit_url = assignment_url.rstrip("/")
                if "view.php" in edit_url:
                    edit_url = edit_url.replace("view.php", "view.php") + "&action=editsubmission"
                page.goto(edit_url, wait_until="networkidle")
                file_input = page.query_selector("input[type='file']")

            if not file_input:
                out.update({"status": "error", "message": "Could not find file upload field on assignment page."})
                page.close()
                return

            print(f"[submit_hw] Uploading {pdf_path}")
            file_input.set_input_files(pdf_path)
            page.wait_for_timeout(1000)

            # Click save
            saved = False
            for sel in ["#id_submitbutton", "input[name=savechanges]",
                        "input[type=submit]", "button[type=submit]"]:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    print(f"[submit_hw] Clicked submit via {sel}")
                    saved = True
                    break

            if not saved:
                out.update({"status": "error", "message": "Could not find save button."})
                page.close()
                return

            page.wait_for_load_state("networkidle", timeout=15_000)
            print(f"[submit_hw] Result page: {page.title()}")
            page.close()
            out.update({"status": "ok"})

        except Exception as exc:
            import traceback
            out.update({"status": "error", "message": str(exc),
                        "trace": traceback.format_exc()})
        finally:
            sys.stdout = old_stdout
            out["log"] = buf.getvalue()
            import os
            try: os.unlink(pdf_path)
            except Exception: pass

    t = threading.Thread(target=_worker, daemon=True, name="submit_hw_worker")
    t.start()
    t.join(timeout=180)
    if t.is_alive():
        return JSONResponse({"status": "error", "message": "Timed out after 180s"})
    return out or {"status": "error", "message": "unknown error"}


# ────────────────────────────────────────────────────────────────
# 4. Embedded HTML interface
# ────────────────────────────────────────────────────────────────
HTML_UI = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ReplyX — TUM AI Campus Copilot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,html,body{box-sizing:border-box;margin:0;padding:0;background:#0D0D0F;}
body{font-family:'Inter',-apple-system,sans-serif;color:#fff;min-height:100vh;display:flex;flex-direction:column;}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-thumb{background:#222;border-radius:2px;}
a{color:#00C8C8;text-decoration:none;}
.grad{background:linear-gradient(135deg,#00C8C8,#0080FF,#00C8C8);background-size:200% 200%;
 animation:gradient 4s ease infinite;-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
@keyframes gradient{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes wave{0%,100%{transform:scaleY(.4)}50%{transform:scaleY(1.2)}}
.wave-bar{width:4px;height:20px;border-radius:3px;background:linear-gradient(180deg,#00C8C8,#0080FF);
 animation:wave 1s ease-in-out infinite;transform-origin:center;}
header{padding:14px 28px;border-bottom:1px solid #1a1a1f;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:#0D0D0F;z-index:10;}
.logo{font-size:22px;font-weight:800;}
.sub{color:#2a2a3a;font-size:13px;margin-left:12px;}
.tabs{display:flex;gap:6px;}
.tab{padding:7px 18px;border-radius:22px;border:1px solid #222;background:transparent;color:#444;
 cursor:pointer;font-size:13px;font-weight:600;transition:all .2s;font-family:inherit;}
.tab.active{border-color:#00C8C8;background:rgba(0,200,200,.1);color:#00C8C8;}
.tab:hover{border-color:#00C8C8;color:#00C8C8;}
main{flex:1;display:flex;flex-direction:column;}
.pane{display:none;flex:1;flex-direction:column;}
.pane.active{display:flex;}
.container{max-width:720px;width:100%;margin:0 auto;padding:32px 24px;flex:1;display:flex;flex-direction:column;gap:24px;}
.chat{overflow-y:auto;display:flex;flex-direction:column;gap:20px;}
.msg{display:flex;flex-direction:column;gap:6px;}
.msg.u{align-items:flex-end;}.msg.a{align-items:flex-start;}
.role{font-size:10px;color:#2a2a3a;font-weight:700;letter-spacing:.08em;}
.bubble{max-width:82%;padding:14px 18px;border-radius:20px;font-size:14px;line-height:1.7;text-align:left;}
.bubble.u{background:linear-gradient(135deg,#00C8C8,#0080FF);color:#fff;border-radius:20px 20px 6px 20px;}
.bubble.a{background:#111116;border:1px solid #1a1a22;color:#c8c8d4;border-radius:4px 20px 20px 20px;}
.bubble.a strong{color:#00C8C8;font-weight:600;}
.bubble.a code{background:#1e1e2a;padding:2px 6px;border-radius:4px;font-size:13px;color:#00C8C8;}
.sources{display:flex;gap:6px;flex-wrap:wrap;}
.src{padding:3px 10px;border-radius:12px;background:rgba(0,200,200,.04);
 border:1px solid rgba(0,200,200,.15);font-size:11px;color:#00A8A8;}
.input-row{display:flex;gap:8px;background:#111116;border:1px solid #1a1a22;
 border-radius:26px;padding:5px 5px 5px 16px;}
.input-row input{flex:1;background:transparent;border:none;color:#c8c8d4;font-size:14px;
 outline:none;padding:8px 0;font-family:inherit;}
.input-row input::placeholder{color:#333;}
.btn{padding:9px 20px;border-radius:20px;border:none;background:linear-gradient(135deg,#00C8C8,#0080FF);
 color:#fff;font-weight:700;cursor:pointer;font-size:13px;font-family:inherit;}
.btn:disabled{background:#1a1a22;color:#333;cursor:not-allowed;}
.btn-block{width:100%;padding:14px;border-radius:14px;font-size:15px;margin-top:8px;}
.field{margin-bottom:18px;}
.label{font-size:12px;color:#555;font-weight:600;letter-spacing:.05em;display:block;margin-bottom:8px;}
.input{width:100%;padding:10px 14px;border-radius:10px;border:1px solid #1a1a22;
 background:#0D0D0F;color:#c8c8d4;font-size:14px;outline:none;font-family:inherit;}
.input:focus{border-color:#00C8C8;}
.drop{border:2px dashed #1a1a22;border-radius:12px;padding:32px;text-align:center;cursor:pointer;transition:all .2s;}
.drop:hover{border-color:#00C8C8;background:rgba(0,200,200,.03);}
.drop input{display:none;}
.title{font-size:20px;font-weight:700;margin-bottom:6px;}
.muted{color:#555;font-size:13px;}
.pill-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
.pill{padding:7px 14px;border-radius:18px;border:1px solid #2a2a3a;background:transparent;
 color:#666;cursor:pointer;font-size:12px;font-family:inherit;transition:all .2s;}
.pill:hover,.pill.active{border-color:#00C8C8;color:#00C8C8;background:rgba(0,200,200,.05);}
.members{background:#111116;border:1px solid #1a1a22;border-radius:12px;overflow:hidden;}
.members-hdr{display:grid;grid-template-columns:30px 1fr 1fr;padding:8px 14px;border-bottom:1px solid #1a1a22;background:#0D0D0F;font-size:11px;color:#444;}
.members-row{display:grid;grid-template-columns:30px 1fr 1fr;gap:8px;padding:10px 14px;
 border-bottom:1px solid #0D0D0F;align-items:center;font-size:13px;color:#444;}
.members-row input{padding:8px 10px;font-size:13px;}
.result{margin-top:20px;padding:18px;border-radius:12px;}
.result.ok{background:rgba(0,200,200,.05);border:1px solid rgba(0,200,200,.2);}
.result.err{background:rgba(255,80,80,.05);border:1px solid rgba(255,80,80,.2);color:#ff5050;}
.code{font-family:monospace;font-size:12px;color:#888;background:#0a0a0c;padding:8px 10px;border-radius:6px;white-space:pre-wrap;margin-top:8px;}
.loading{display:flex;gap:4px;align-items:center;padding-left:4px;}
.loading span{color:#444;font-size:12px;margin-left:8px;}
select.input{appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path fill='%2300C8C8' d='M6 9L1 3h10z'/></svg>");background-repeat:no-repeat;background-position:right 14px center;padding-right:34px;}
.slide-img{max-width:100%;border-radius:10px;margin-top:10px;border:1px solid #1a1a22;}
hr.sep{border:none;border-top:1px solid #1a1a1f;margin:24px 0;}
.intent-card{background:#111116;border:1px solid #1a1a22;border-radius:12px;padding:14px;margin-top:10px;}
.intent-card .kv{display:flex;gap:10px;font-size:13px;margin:4px 0;}
.intent-card .k{color:#555;min-width:90px;}
.intent-card .v{color:#c8c8d4;}
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;">
    <span class="logo grad">ReplyX</span>
    <span class="sub">TUM AI Campus Copilot</span>
  </div>
  <div class="tabs">
    <button class="tab active" data-tab="learn">Learn</button>
    <button class="tab" data-tab="submit">Submit HW</button>
    <button class="tab" data-tab="send">Send Message</button>
    <button class="tab" data-tab="setup">Setup</button>
  </div>
</header>

<main>

<!-- ────────── LEARN ────────── -->
<section class="pane active" id="pane-learn">
  <div class="container" style="gap:24px;">
    <div id="chat" class="chat">
      <div style="text-align:center;margin-top:40px;" id="chat-empty">
        <div class="title grad" style="font-size:22px;margin-bottom:8px;">Ask anything about your TUM lectures</div>
        <div class="muted" style="margin-bottom:18px;">Claude AI · Qwen3 Embeddings · 500+ slides</div>
        <div class="pill-row" style="justify-content:center;">
          <button class="pill" onclick="setAsk('What is virtualization?')">What is virtualization?</button>
          <button class="pill" onclick="setAsk('Give me page 2')">Give me page 2</button>
          <button class="pill" onclick="setAsk('Explain paging')">Explain paging</button>
          <button class="pill" onclick="setAsk('Bayesian vs Frequentist')">Bayesian vs Frequentist</button>
        </div>
      </div>
    </div>
    <div class="input-row">
      <input id="ask-input" placeholder="Ask about your TUM lectures..." onkeydown="if(event.key==='Enter')ask()"/>
      <button class="btn" id="ask-btn" onclick="ask()">Send</button>
    </div>
  </div>
</section>

<!-- ────────── SUBMIT HW ────────── -->
<section class="pane" id="pane-submit">
  <div class="container" style="max-width:680px;">
    <div style="text-align:center;">
      <div class="title grad">Submit Homework</div>
      <div class="muted">Pick course → pick assignment → upload PDF</div>
    </div>

    <!-- Step 1: upload PDF -->
    <div class="field">
      <label class="label">STEP 1 — HOMEWORK PDF</label>
      <label class="drop" id="hw-drop">
        <input type="file" accept=".pdf" id="hw-file"/>
        <div id="hw-drop-text">
          <div style="font-size:32px;margin-bottom:8px;">📄</div>
          <div style="color:#555;font-size:14px;">Click to upload your homework PDF</div>
        </div>
      </label>
    </div>

    <!-- Step 2: pick course (loaded from Moodle) -->
    <div class="field" id="hw-step2" style="display:none;">
      <label class="label">STEP 2 — COURSE</label>
      <div id="hw-course-list" style="display:flex;flex-direction:column;gap:8px;max-height:260px;overflow-y:auto;"></div>
    </div>

    <!-- Step 3: pick assignment (loaded from Moodle) -->
    <div class="field" id="hw-step3" style="display:none;">
      <label class="label">STEP 3 — ASSIGNMENT SLOT</label>
      <div id="hw-assign-list" style="display:flex;flex-direction:column;gap:8px;max-height:260px;overflow-y:auto;"></div>
    </div>

    <!-- Step 4: confirm & submit -->
    <div id="hw-step4" style="display:none;">
      <div style="background:#111116;border:1px solid #1a1a22;border-radius:12px;padding:14px;margin-bottom:16px;font-size:13px;">
        <div style="color:#555;margin-bottom:6px;">READY TO SUBMIT</div>
        <div id="hw-summary" style="color:#c8c8d4;line-height:1.8;"></div>
      </div>
      <button class="btn btn-block" id="hw-submit" onclick="submitHW()">Upload to Moodle</button>
    </div>

    <div id="hw-result" style="margin-top:16px;"></div>
  </div>
</section>

<!-- ────────── SEND MESSAGE ────────── -->
<section class="pane" id="pane-send">
  <div class="container" style="max-width:680px;">
    <div style="text-align:center;">
      <div class="title grad">Send a message</div>
      <div class="muted">Just describe what you want — AI fills in the rest</div>
    </div>

    <!-- ── Chat widget for natural language input ── -->
    <div id="nl-chat" style="background:#111116;border:1px solid #1a1a22;border-radius:14px;
         padding:16px;display:flex;flex-direction:column;gap:12px;min-height:80px;
         max-height:300px;overflow-y:auto;margin-bottom:10px;">
      <div id="nl-chat-empty" style="color:#333;font-size:13px;text-align:center;padding:16px 0;">
        Describe what you want to send, e.g.<br>
        <em style="color:#444;">"ask Müller about HW3 task 2"</em><br>
        <em style="color:#444;">"post in the Moodle OS forum that I have a question about scheduling"</em>
      </div>
    </div>
    <div class="input-row" style="margin-bottom:0;">
      <input id="nl-chat-input" placeholder="Describe your message in plain language…"
             onkeydown="if(event.key==='Enter')nlChat()"/>
      <button class="btn" id="nl-chat-btn" onclick="nlChat()">Ask</button>
    </div>

    <hr class="sep"/>

    <!-- ── Form — auto-filled by AI, editable by user ── -->
    <div class="field">
      <label class="label">DESTINATION TYPE</label>
      <select class="input" id="dest-type" onchange="updateDestFields()">
        <option value="dm">Zulip direct message (DM)</option>
        <option value="stream">Zulip stream</option>
        <option value="forum">Moodle forum</option>
      </select>
    </div>

    <div class="field" id="f-course">
      <label class="label">COURSE</label>
      <input class="input" id="send-course" placeholder="e.g. Grundlagen Betriebssysteme" list="courses-list"/>
      <datalist id="courses-list"></datalist>
    </div>

    <div class="field" id="f-person">
      <label class="label">PERSON (for DM)</label>
      <input class="input" id="send-person" placeholder="e.g. Müller"/>
    </div>

    <div class="field" id="f-stream" style="display:none;">
      <label class="label">STREAM</label>
      <input class="input" id="send-stream" placeholder="Zulip stream name" list="streams-list"/>
      <datalist id="streams-list"></datalist>
    </div>

    <div class="field" id="f-topic" style="display:none;">
      <label class="label">TOPIC</label>
      <input class="input" id="send-topic" placeholder="General" value="General"/>
    </div>

    <div class="field" id="f-forum-name" style="display:none;">
      <label class="label">FORUM NAME</label>
      <input class="input" id="send-forum-name" placeholder="e.g. Übungsblatt discussion, Diskussionsforum"/>
    </div>

    <div class="field" id="f-subject" style="display:none;">
      <label class="label">MESSAGE TITLE</label>
      <input class="input" id="send-subject" placeholder="e.g. Question about Abgabeblatt 6"/>
    </div>

    <div class="field">
      <label class="label">MESSAGE</label>
      <textarea class="input" id="send-msg" rows="6" placeholder="Type your message..."></textarea>
    </div>

    <button class="btn btn-block" id="send-btn" onclick="sendMessage()">Send</button>
    <div id="send-result"></div>
  </div>
</section>

<!-- ────────── SETUP ────────── -->
<section class="pane" id="pane-setup">
  <div class="container" style="max-width:680px;">
    <div style="text-align:center;">
      <div class="title grad">Setup credentials</div>
      <div class="muted">Zulip + TUM + name for Deckblatt. Stored securely in the system keychain.</div>
    </div>

    <div id="setup-status" class="muted" style="text-align:center;"></div>

    <div class="field"><label class="label">TUM ID (e.g. ge12abc)</label>
      <input class="input" id="s-tum-user"/></div>
    <div class="field"><label class="label">TUM PASSWORD</label>
      <input class="input" id="s-tum-pass" type="password" placeholder="leave empty to keep existing"/></div>

    <div class="field"><label class="label">ZULIP SITE</label>
      <input class="input" id="s-zulip-site" placeholder="https://zulip.cit.tum.de"/></div>
    <div class="field"><label class="label">ZULIP EMAIL</label>
      <input class="input" id="s-zulip-email"/></div>
    <div class="field"><label class="label">ZULIP API KEY</label>
      <input class="input" id="s-zulip-key" type="password" placeholder="leave empty to keep existing"/></div>

    <div class="field"><label class="label">STUDENT NAME (Deckblatt)</label>
      <input class="input" id="s-name"/></div>
    <div class="field"><label class="label">MATRIKELNUMMER</label>
      <input class="input" id="s-mat"/></div>

    <button class="btn btn-block" onclick="saveSetup()">Save</button>
    <div id="setup-result"></div>
  </div>
</section>

</main>

<script>
const API = "";  // same origin

// ────────── Tabs ──────────
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('pane-' + t.dataset.tab).classList.add('active');
});

// ────────── LEARN ──────────
function setAsk(q){document.getElementById('ask-input').value=q;}
const chatEl = document.getElementById('chat');
function addMsg(role, html, sources, img){
  const empty = document.getElementById('chat-empty');
  if(empty) empty.remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + (role==='user'?'u':'a');
  const rl = document.createElement('div');
  rl.className = 'role';
  rl.textContent = role==='user'?'YOU':'REPLYX';
  wrap.appendChild(rl);
  const b = document.createElement('div');
  b.className = 'bubble ' + (role==='user'?'u':'a');
  b.innerHTML = html;
  wrap.appendChild(b);
  if(img){
    const im = document.createElement('img');
    im.className = 'slide-img';
    im.src = 'data:image/png;base64,' + img;
    wrap.appendChild(im);
  }
  if(sources && sources.length){
    const sw = document.createElement('div');
    sw.className = 'sources';
    sources.forEach(s => {
      const p = document.createElement('span');
      p.className = 'src';
      p.textContent = '📄 ' + (s.pdf||'') + ' · Slide ' + (s.slide_num||0);
      sw.appendChild(p);
    });
    wrap.appendChild(sw);
  }
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
}
function mdToHtml(s){
  if(!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\n\n/g,'</p><p>')
    .replace(/\n/g,'<br>');
}
async function ask(){
  const inp = document.getElementById('ask-input');
  const q = inp.value.trim();
  if(!q) return;
  inp.value = '';
  addMsg('user', q.replace(/</g,'&lt;'));
  const btn = document.getElementById('ask-btn');
  btn.disabled = true; btn.textContent = '...';
  try{
    const r = await fetch(API+'/api/ask', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:q})});
    const d = await r.json();
    addMsg('assistant', '<p>'+mdToHtml(d.answer||d.error||'(empty)')+'</p>',
      d.sources||[], d.slide_image);
  }catch(e){
    addMsg('assistant', 'Cannot connect to backend: '+e);
  }
  btn.disabled = false; btn.textContent = 'Send';
}

// ────────── SUBMIT HW ──────────
const membersEl = document.getElementById('members');
// ────────── SUBMIT HW — step-by-step ──────────
let hwFile = null;
let hwSelectedCourse = null;   // {name, url}
let hwSelectedAssign = null;   // {text, url}

document.getElementById('hw-file').onchange = async e => {
  hwFile = e.target.files[0] || null;
  if(!hwFile) return;
  const t = document.getElementById('hw-drop-text');
  t.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">✅</div>
    <div style="color:#00C8C8;font-weight:600;">${hwFile.name}</div>
    <div style="color:#444;font-size:12px;margin-top:4px;">${(hwFile.size/1024/1024).toFixed(2)} MB — loading courses…</div>`;
  // reset downstream steps
  hwSelectedCourse = null; hwSelectedAssign = null;
  document.getElementById('hw-step2').style.display = 'none';
  document.getElementById('hw-step3').style.display = 'none';
  document.getElementById('hw-step4').style.display = 'none';
  await hwLoadCourses();
};

async function hwLoadCourses(){
  const list = document.getElementById('hw-course-list');
  list.innerHTML = '<div style="color:#555;font-size:13px;">Loading your Moodle courses…</div>';
  document.getElementById('hw-step2').style.display = '';
  try{
    const r = await fetch(API+'/api/hw-courses');
    const d = await r.json();
    if(d.status !== 'ok' || !d.courses.length){
      list.innerHTML = '<div style="color:#ff5050;font-size:13px;">Could not load courses: '+(d.message||'none found')+'</div>';
      return;
    }
    list.innerHTML = '';
    d.courses.forEach(c => {
      const btn = document.createElement('button');
      btn.className = 'pill';
      btn.textContent = c.name;
      btn.onclick = () => hwPickCourse(c, btn);
      list.appendChild(btn);
    });
  }catch(e){
    list.innerHTML = '<div style="color:#ff5050;font-size:13px;">Error: '+e+'</div>';
  }
}

async function hwPickCourse(course, btnEl){
  hwSelectedCourse = course;
  hwSelectedAssign = null;
  // highlight selection
  document.querySelectorAll('#hw-course-list .pill').forEach(b => b.classList.remove('active'));
  btnEl.classList.add('active');
  // reset step 3
  document.getElementById('hw-step3').style.display = 'none';
  document.getElementById('hw-step4').style.display = 'none';
  await hwLoadAssignments(course.url);
}

async function hwLoadAssignments(courseUrl){
  const list = document.getElementById('hw-assign-list');
  list.innerHTML = '<div style="color:#555;font-size:13px;">Loading assignments…</div>';
  document.getElementById('hw-step3').style.display = '';
  try{
    const r = await fetch(API+'/api/hw-assignments?course_url='+encodeURIComponent(courseUrl));
    const d = await r.json();
    if(d.status !== 'ok' || !d.assignments.length){
      list.innerHTML = '<div style="color:#ff5050;font-size:13px;">No assignment slots found in this course.</div>';
      return;
    }
    list.innerHTML = '';
    d.assignments.forEach(a => {
      const btn = document.createElement('button');
      btn.className = 'pill';
      btn.innerHTML = `<span style="color:#555;font-size:11px;margin-right:6px;">${a.section}</span>${a.text}`;
      btn.onclick = () => hwPickAssign(a, btn);
      list.appendChild(btn);
    });
  }catch(e){
    list.innerHTML = '<div style="color:#ff5050;font-size:13px;">Error: '+e+'</div>';
  }
}

function hwPickAssign(assign, btnEl){
  hwSelectedAssign = assign;
  document.querySelectorAll('#hw-assign-list .pill').forEach(b => b.classList.remove('active'));
  btnEl.classList.add('active');
  // show summary + submit button
  document.getElementById('hw-summary').innerHTML =
    `📚 <b>${hwSelectedCourse.name}</b><br>📝 ${assign.text}<br>📄 ${hwFile.name}`;
  document.getElementById('hw-step4').style.display = '';
}

async function submitHW(){
  if(!hwFile || !hwSelectedCourse || !hwSelectedAssign){
    alert('Please complete all steps first.');
    return;
  }
  const btn = document.getElementById('hw-submit');
  btn.disabled = true; btn.textContent = 'Uploading…';
  const out = document.getElementById('hw-result');
  const fd = new FormData();
  fd.append('file', hwFile);
  fd.append('course_name', hwSelectedCourse.name);
  fd.append('assignment_url', hwSelectedAssign.url);
  fd.append('assignment_text', hwSelectedAssign.text);
  try{
    const r = await fetch(API+'/api/submit-homework', {method:'POST', body:fd});
    const d = await r.json();
    if(d.status === 'ok' || d.status === 'success'){
      out.className='result ok';
      out.innerHTML = '✅ Submitted successfully!'+(d.log?`<div class="code">${d.log.replace(/</g,'&lt;')}</div>`:'');
    } else {
      out.className='result err';
      out.innerHTML = '❌ '+(d.message||JSON.stringify(d))+(d.log?`<div class="code">${d.log.replace(/</g,'&lt;')}</div>`:'');
    }
  }catch(e){
    out.className='result err';
    out.textContent = 'Cannot connect to backend: '+e;
  }
  btn.disabled = false; btn.textContent = 'Upload to Moodle';
}

// ────────── SEND MESSAGE ──────────
function updateDestFields(){
  const t = document.getElementById('dest-type').value;
  document.getElementById('f-person').style.display  = (t==='dm') ? '' : 'none';
  document.getElementById('f-stream').style.display  = (t==='stream') ? '' : 'none';
  document.getElementById('f-topic').style.display   = (t==='stream') ? '' : 'none';
  document.getElementById('f-forum-name').style.display = (t==='forum') ? '' : 'none';
  document.getElementById('f-subject').style.display = (t==='forum') ? '' : 'none';
  document.getElementById('f-course').style.display  = (t==='forum') ? '' : 'none';
}
updateDestFields();

async function loadDestinations(){
  try{
    const r = await fetch(API+'/api/destinations');
    const d = await r.json();
    const cl = document.getElementById('courses-list');
    const sl = document.getElementById('streams-list');
    cl.innerHTML = (d.courses||[]).map(c => `<option value="${c.name}">`).join('');
    sl.innerHTML = (d.streams||[]).map(s => `<option value="${s}">`).join('');
  }catch(e){}
}
loadDestinations();

// ── NL Chat widget ──
let nlHistory = [];   // [{role:'user'|'assistant', content:'...'}]

function _addNlBubble(role, text){
  const chat = document.getElementById('nl-chat');
  const empty = document.getElementById('nl-chat-empty');
  if(empty) empty.remove();
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;flex-direction:column;gap:4px;align-items:'+(role==='user'?'flex-end':'flex-start');
  const lbl = document.createElement('div');
  lbl.style.cssText = 'font-size:10px;color:#2a2a3a;font-weight:700;letter-spacing:.08em;';
  lbl.textContent = role === 'user' ? 'YOU' : 'AI';
  const bub = document.createElement('div');
  bub.style.cssText = 'max-width:88%;padding:10px 14px;border-radius:16px;font-size:13px;line-height:1.6;' +
    (role==='user'
      ? 'background:linear-gradient(135deg,#00C8C8,#0080FF);color:#fff;border-radius:16px 16px 4px 16px;'
      : 'background:#0D0D0F;border:1px solid #1a1a22;color:#c8c8d4;border-radius:4px 16px 16px 16px;');
  bub.textContent = text;
  wrap.appendChild(lbl);
  wrap.appendChild(bub);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
}

function fillFromIntent(i){
  if(i.dest_type){document.getElementById('dest-type').value = i.dest_type; updateDestFields();}
  if(i.course)  document.getElementById('send-course').value  = i.course;
  if(i.person)  document.getElementById('send-person').value  = i.person;
  if(i.stream)  document.getElementById('send-stream').value  = i.stream;
  if(i.topic)   document.getElementById('send-topic').value   = i.topic;
  if(i.forum_name) document.getElementById('send-forum-name').value = i.forum_name;
  if(i.subject)    document.getElementById('send-subject').value    = i.subject;
  if(i.message) document.getElementById('send-msg').value     = i.message;
}

function _intentSummary(intent){
  const dt = intent.dest_type || '';
  const parts = [];
  if(dt === 'dm')     parts.push(`DM → ${intent.person || '?'}`);
  if(dt === 'forum')  parts.push(`Moodle forum (${intent.course || '?'})`);
  if(dt === 'stream') parts.push(`Stream "${intent.stream || '?'}" / topic "${intent.topic || 'General'}"`);
  if(intent.message)  parts.push(`Message: "${intent.message.slice(0,80)}${intent.message.length>80?'…':''}"`);
  return parts.join(' · ') || JSON.stringify(intent);
}

async function nlChat(){
  const inp = document.getElementById('nl-chat-input');
  const msg = inp.value.trim();
  if(!msg) return;
  inp.value = '';

  nlHistory.push({role:'user', content: msg});
  _addNlBubble('user', msg);

  const btn = document.getElementById('nl-chat-btn');
  btn.disabled = true; btn.textContent = '…';

  try{
    const r = await fetch(API+'/api/chat-intent', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({messages: nlHistory})
    });
    const d = await r.json();

    if(d.status === 'error'){
      const errMsg = 'Error: ' + (d.message || 'something went wrong');
      nlHistory.push({role:'assistant', content: errMsg});
      _addNlBubble('assistant', errMsg);

    } else if(d.status === 'clarify'){
      // Gemini needs more info — show the question, wait for user reply
      nlHistory.push({role:'assistant', content: d.question});
      _addNlBubble('assistant', d.question);

    } else if(d.status === 'resolved'){
      // Intent is clear — fill the form and confirm
      fillFromIntent(d.intent || {});
      const summary = 'Got it! ' + _intentSummary(d.intent) + ' — check the form below and click Send.';
      nlHistory.push({role:'assistant', content: summary});
      _addNlBubble('assistant', summary);
    }
  } catch(e){
    const errMsg = 'Cannot reach backend: ' + e;
    nlHistory.push({role:'assistant', content: errMsg});
    _addNlBubble('assistant', errMsg);
  }

  btn.disabled = false; btn.textContent = 'Ask';
}

async function sendMessage(){
  const dt = document.getElementById('dest-type').value;
  const body = {
    dest_type: dt,
    message:   document.getElementById('send-msg').value,
    course:    document.getElementById('send-course').value,
    person:    document.getElementById('send-person').value,
    stream:    document.getElementById('send-stream').value,
    topic:     document.getElementById('send-topic').value || 'General',
    subject:    document.getElementById('send-subject').value,
    forum_name: document.getElementById('send-forum-name').value,
  };
  if(!body.message.trim()){ alert('Message is empty'); return; }
  const btn = document.getElementById('send-btn');
  btn.disabled = true; btn.textContent = 'Sending…';
  const out = document.getElementById('send-result');
  try{
    const r = await fetch(API+'/api/send-message', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const d = await r.json();
    if(d.status==='ok'){
      out.className='result ok';
      out.innerHTML = '✅ Sent.'+(d.log?`<div class="code">${d.log.replace(/</g,'&lt;')}</div>`:'');
    } else {
      out.className='result err';
      out.innerHTML = '❌ ' + (d.message||'failed') + (d.log?`<div class="code">${d.log.replace(/</g,'&lt;')}</div>`:'');
    }
  }catch(e){
    out.className='result err';
    out.textContent = 'Cannot connect to backend: '+e;
  }
  btn.disabled=false; btn.textContent='Send';
}

// ────────── SETUP ──────────
async function loadSetup(){
  try{
    const r = await fetch(API+'/api/setup-status');
    const d = await r.json();
    document.getElementById('setup-status').textContent =
      d.registered ? '✓ Credentials present — fill fields to update.' : 'Not yet configured.';
    document.getElementById('s-tum-user').value    = d.tum_user || '';
    document.getElementById('s-zulip-site').value  = d.zulip_site || 'https://zulip.cit.tum.de';
    document.getElementById('s-zulip-email').value = d.zulip_email || '';
    document.getElementById('s-name').value        = d.student_name || '';
    document.getElementById('s-mat').value         = d.matrikelnummer || '';
  }catch(e){}
}
loadSetup();
async function saveSetup(){
  const payload = {
    tum_user:       document.getElementById('s-tum-user').value.trim(),
    tum_password:   document.getElementById('s-tum-pass').value,
    zulip_site:     document.getElementById('s-zulip-site').value.trim(),
    zulip_email:    document.getElementById('s-zulip-email').value.trim(),
    zulip_api_key:  document.getElementById('s-zulip-key').value,
    student_name:   document.getElementById('s-name').value.trim(),
    matrikelnummer: document.getElementById('s-mat').value.trim(),
  };
  const out = document.getElementById('setup-result');
  try{
    const r = await fetch(API+'/api/setup', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)});
    const d = await r.json();
    out.className = d.status==='ok' ? 'result ok' : 'result err';
    out.textContent = d.status==='ok' ? '✅ Saved to keychain.' : ('❌ '+(d.message||''));
    loadSetup();
  }catch(e){ out.className='result err'; out.textContent = 'Error: '+e; }
}
</script>
</body>
</html>
"""

# The backend already registers GET "/" (JSON status). We override it with the HTML UI.
# The JSON status remains accessible at /api/status.
try:
    _old_root = next((r for r in app.router.routes
                      if getattr(r, "path", None) == "/" and "GET" in getattr(r, "methods", set())), None)
    if _old_root is not None:
        _old_endpoint = _old_root.endpoint
        app.router.routes.remove(_old_root)

        @app.get("/api/status")
        def _backend_root_status():
            return _old_endpoint()
except Exception:
    pass


@app.get("/", response_class=HTMLResponse)
def ui_root():
    return HTML_UI


# ────────────────────────────────────────────────────────────────
# 5. Entry point
# ────────────────────────────────────────────────────────────────
def _open_browser_later(url: str, delay: float = 1.5):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        print("\n[ReplyX] Install dependencies first:\n  pip install -r requirements.txt\n")
        sys.exit(1)

    HOST = os.environ.get("REPLYX_HOST", "127.0.0.1")
    PORT = int(os.environ.get("REPLYX_PORT", "8000"))
    url  = f"http://{HOST}:{PORT}"

    print("\n" + "─" * 60)
    print(f"  ReplyX is starting at {url}")
    print("  Browser will open automatically.")
    print("─" * 60 + "\n")

    _open_browser_later(url)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
