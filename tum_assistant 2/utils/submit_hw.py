"""
submit_hw.py — prepare and submit homework to Moodle (and optionally TUM Online).

Pipeline:
  1. Generate Deckblatt PDF (name, matrikel, course, sheet number)
  2. Merge Deckblatt + your HW PDF
  3. Compress if > max_size_kb
  4. Navigate to the right Moodle submission box and upload
     - Course page is scraped live; Gemini picks the right assignment
     - ALL clicking/button-finding goes through ai_click / find_selector
       (no hardcoded button-text comparisons)

Usage
-----
submit_hw("Algorithmen", "Sheet 4", "hw4_solutions.pdf")
submit_hw("Algorithmen", "Sheet 4", "hw4_solutions.pdf",
          add_deckblatt=True, max_size_kb=8000)
"""
import json, os, re, subprocess, tempfile, time
from pathlib import Path
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from pypdf import PdfWriter, PdfReader
from config import STUDENT_NAME, MATRIKELNUMMER, DESTINATIONS_FILE
from utils.browser import new_page


def submit_hw(
    course: str,
    sheet: str,
    hw_pdf: str,
    *,
    add_deckblatt: bool = True,
    max_size_kb: int = 8000,
):
    final_pdf = hw_pdf
    tmp_files = []

    try:
        if add_deckblatt:
            db_path = tempfile.mktemp(suffix="_deckblatt.pdf")
            tmp_files.append(db_path)
            _make_deckblatt(db_path, course=course, sheet=sheet)
            merged_path = tempfile.mktemp(suffix="_merged.pdf")
            tmp_files.append(merged_path)
            _merge_pdfs([db_path, hw_pdf], merged_path)
            final_pdf = merged_path

        size_kb = os.path.getsize(final_pdf) / 1024
        if size_kb > max_size_kb:
            print(f"[submit_hw] File is {size_kb:.0f} KB > {max_size_kb} KB — compressing...")
            compressed = tempfile.mktemp(suffix="_compressed.pdf")
            tmp_files.append(compressed)
            _compress_pdf(final_pdf, compressed)
            final_pdf = compressed

        _upload_to_moodle(course, sheet, final_pdf)

    finally:
        for f in tmp_files:
            if os.path.exists(f):
                os.unlink(f)


# ── Deckblatt ─────────────────────────────────────────────────────────────────

def _make_deckblatt(out_path: str, course: str, sheet: str):
    c = rl_canvas.Canvas(out_path, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(w / 2, h - 120, f"Homework {sheet}")

    c.setFont("Helvetica", 13)
    fields = [
        ("Course",         course),
        ("Name",           STUDENT_NAME),
        ("Matrikelnummer", MATRIKELNUMMER),
    ]
    y = h - 200
    for label, value in fields:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(80, y, f"{label}:")
        c.setFont("Helvetica", 12)
        c.drawString(220, y, value)
        y -= 28

    c.setFont("Helvetica", 10)
    from datetime import date
    c.drawString(80, 80, f"Submission date: {date.today().strftime('%d.%m.%Y')}")

    c.showPage()
    c.save()
    print(f"[submit_hw] Deckblatt written: {out_path}")


# ── PDF merge ─────────────────────────────────────────────────────────────────

def _merge_pdfs(paths: list[str], out_path: str):
    writer = PdfWriter()
    for path in paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
    with open(out_path, "wb") as f:
        writer.write(f)
    print(f"[submit_hw] Merged {len(paths)} PDFs → {out_path}")


# ── PDF compression via Ghostscript ──────────────────────────────────────────

def _compress_pdf(in_path: str, out_path: str, quality: str = "/ebook"):
    result = subprocess.run(
        [
            "gs",
            "-dBATCH", "-dNOPAUSE", "-q",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.5",
            f"-dPDFSETTINGS={quality}",
            f"-sOutputFile={out_path}",
            in_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript failed: {result.stderr}")
    old_kb = os.path.getsize(in_path) / 1024
    new_kb = os.path.getsize(out_path) / 1024
    print(f"[submit_hw] Compressed: {old_kb:.0f} KB → {new_kb:.0f} KB")


# ── Moodle upload ─────────────────────────────────────────────────────────────

def _upload_to_moodle(course: str, sheet: str, pdf_path: str):
    """
    Navigate to Moodle, find the right assignment for this course + sheet,
    and upload the PDF.

    Navigation strategy (mirrors send_qa._post_moodle_forum):
      1. Open the live Moodle course page.
      2. Scrape every /mod/assign/ link.
      3. Ask Gemini to pick the correct one for `sheet`.
      4. Navigate there; click "Add / Edit submission" via ai_click.
      5. Set the file on the standard file input (Playwright API, not click).
      6. Click save via ai_click — never by guessing button text.
    """
    from utils.ai_navigator import ai_click, find_selector

    # ── find course URL from the live dashboard (reflects real enrolments) ───
    page = new_page()
    _, course_url = _live_moodle_course(course, page)

    print(f"[submit_hw] Opening course page: {course_url}")
    page.goto(course_url, wait_until="networkidle")
    print(f"[submit_hw] Course page: {page.title()}")

    # ── scrape all assignment links from the live page ─────────────────────────
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
                results.push({
                    section: currentSection,
                    text:    text.slice(0, 200),
                    url:     node.href,
                    numbers: (text.match(/\d+/g) || []).map(Number),
                });
            }
        }
        // deduplicate by URL
        const seen = new Set();
        return results.filter(it => {
            if (seen.has(it.url)) return false;
            seen.add(it.url); return true;
        });
    }""")

    if not items:
        page.close()
        raise RuntimeError(
            f"No assignment submission links found on the course page for '{course}'. "
            "Make sure you are enrolled and the assignments are visible."
        )

    print(f"[submit_hw] Found {len(items)} assignment links on course page")

    # ── ask Gemini to pick the right one ──────────────────────────────────────
    item_list = "\n".join(
        f"{i}: section='{it['section']}' | text='{it['text']}' | "
        f"numbers_in_text={it['numbers']}"
        for i, it in enumerate(items)
    )

    # Extract target number hint from the sheet description
    target_nums = [int(n) for n in re.findall(r"\d+", sheet)]
    num_hint = ""
    if target_nums:
        num_hint = (
            f"\nTARGET NUMBER HINT: the student said '{sheet}' which contains "
            f"number(s) {target_nums}. Strongly prefer items whose "
            f"numbers_in_text contains {target_nums[0]}."
        )

    pick = _gemini_json(
        f"A student wants to submit homework for: \"{sheet}\".\n\n"
        f"Here are all assignment links on the Moodle course page:\n"
        f"{item_list}\n"
        f"{num_hint}\n\n"
        f"Pick the single best submission target.\n"
        f"Return JSON only: {{\"index\": <number>, \"reason\": \"<why>\"}}."
    )

    idx = int(pick.get("index", 0))
    if not (0 <= idx < len(items)):
        idx = 0
    target = items[idx]
    print(f"[submit_hw] Gemini picked: {target['text']} — {pick.get('reason', '')}")

    # ── navigate to the assignment ─────────────────────────────────────────────
    page.goto(target["url"], wait_until="networkidle")
    print(f"[submit_hw] Assignment page: {page.title()}")

    # ── click "Add submission" / "Edit submission" — AI decides, no guessing ──
    # Check first whether the page already shows a file input (edit mode)
    file_input = page.query_selector("input[type='file']")
    if not file_input:
        try:
            ai_click(page, "Add submission or Edit submission button")
            page.wait_for_load_state("networkidle")
            file_input = page.query_selector("input[type='file']")
        except Exception as e:
            print(f"[submit_hw] No submission button found ({e}), checking for file input anyway")
            file_input = page.query_selector("input[type='file']")

    if not file_input:
        # Last resort: ask AI where the file input is
        try:
            sel = find_selector(page, "file upload input field for homework submission")
            file_input = page.query_selector(sel)
        except Exception:
            pass

    if not file_input:
        page.close()
        raise RuntimeError(
            f"Could not find a file upload field on '{target['text']}'. "
            f"Page: {page.url}"
        )

    # ── upload the file (file inputs are set via Playwright API, never by clicking) ─
    print(f"[submit_hw] Uploading {pdf_path} ...")
    file_input.set_input_files(pdf_path)
    page.wait_for_timeout(800)   # give Moodle's JS time to register the file

    # ── click save — AI identifies the correct button ─────────────────────────
    ai_click(page, "Save changes or Submit assignment button")
    page.wait_for_load_state("networkidle")

    title = (page.title() or "").lower()
    if "fehler" in title or title.startswith("error"):
        page.close()
        raise RuntimeError(
            f"Moodle returned error page ('{page.title()}') after upload attempt. "
            "The submission may not have gone through."
        )

    print("[submit_hw] ✓ Moodle submission complete.")
    page.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _gemini_json(prompt: str) -> dict:
    """Call Gemini and parse the JSON response."""
    import requests
    api_key = os.environ.get("GEMINI_API_KEY", "")
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
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0},
            },
        )
        if resp.status_code in (429, 503):
            time.sleep(15 * (attempt + 1))
            continue
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        clean = re.sub(r"```json|```", "", raw).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            time.sleep(2)
    raise RuntimeError("Gemini unavailable after 3 attempts")


def _live_moodle_course(course_query: str, page) -> tuple:
    """
    Find the Moodle course URL for the currently logged-in user.

    Scrapes the live dashboard (/my/) — never destinations.json — so it
    always reflects the real enrolments of whoever is logged in.
    Uses Gemini for fuzzy matching, no string comparisons.

    Returns (matched_course_name, course_url).
    """
    from config import MOODLE_BASE

    print(f"[submit_hw] Looking up '{course_query}' on live Moodle dashboard...")
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

    if not courses:
        raise RuntimeError(
            "No courses found on your Moodle dashboard. "
            "Make sure you are logged in and have enrolled courses."
        )

    print(f"[submit_hw] Found {len(courses)} courses on dashboard")

    course_list = "\n".join(f"{i}: {c['name']}" for i, c in enumerate(courses))
    pick = _gemini_json(
        f'A TUM student wants to submit homework for the course: "{course_query}"\n\n'
        f'Their enrolled Moodle courses:\n{course_list}\n\n'
        f'Pick the single best match (fuzzy — partial names, abbreviations, etc).\n'
        f'Return JSON only: {{"index": <number>, "reason": "<why>"}}'
    )

    idx = int(pick.get("index", 0))
    if not (0 <= idx < len(courses)):
        idx = 0

    target = courses[idx]
    print(f"[submit_hw] Matched '{course_query}' → '{target['name']}' ({pick.get('reason', '')})")
    return target["name"], target["url"]
