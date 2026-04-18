"""
actions/submit_hw.py — prepare and submit homework to TUM Online.

Pipeline:
  1. Generate Deckblatt PDF (name, matrikel, course, sheet number)
  2. Merge Deckblatt + your HW PDF
  3. Compress if > max_size_kb
  4. Navigate directly to the right TUM Online submission box and upload

Usage
-----
submit_hw("Algorithmen", "Sheet 4", "hw4_solutions.pdf")
submit_hw("Algorithmen", "Sheet 4", "hw4_solutions.pdf", add_deckblatt=True, max_size_kb=8000)
"""
import os, json, subprocess, tempfile
from pathlib import Path
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from pypdf import PdfWriter, PdfReader
from config import (
    STUDENT_NAME, MATRIKELNUMMER, DESTINATIONS_FILE
)
from utils.browser import new_page


def submit_hw(
    course: str,
    sheet: str,
    hw_pdf: str,
    *,
    add_deckblatt: bool = True,
    max_size_kb: int = 8000,
):
    dest = _find_submission_box(course, sheet)
    print(f"[submit_hw] Target: {dest['name']} → {dest['url']}")

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

        _upload_to_tumonline(dest["url"], final_pdf)

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
        ("Course",             course),
        ("Name",               STUDENT_NAME),
        ("Matrikelnummer",     MATRIKELNUMMER),
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
    """
    quality options: /screen (72dpi), /ebook (150dpi), /printer (300dpi)
    /ebook is a good default for HW: readable quality, significantly smaller.
    """
    result = subprocess.run([
        "gs",
        "-dBATCH", "-dNOPAUSE", "-q",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={quality}",
        f"-sOutputFile={out_path}",
        in_path,
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript failed: {result.stderr}")
    old_kb = os.path.getsize(in_path) / 1024
    new_kb = os.path.getsize(out_path) / 1024
    print(f"[submit_hw] Compressed: {old_kb:.0f} KB → {new_kb:.0f} KB")


# ── TUM Online upload ─────────────────────────────────────────────────────────

def _upload_to_tumonline(submission_url: str, pdf_path: str):
    page = new_page()
    page.goto(submission_url)
    page.wait_for_load_state("networkidle")

    # Find file upload input — TUM Online uses a standard file input
    file_input = page.query_selector("input[type='file']")
    if not file_input:
        print("[submit_hw] No file input found — page opened for manual upload.")
        print(f"            URL: {submission_url}")
        page.wait_for_timeout(60_000)   # keep open for 60s
        page.close()
        return

    print(f"[submit_hw] Uploading {pdf_path} ...")
    file_input.set_input_files(pdf_path)

    # Submit — button text varies by course
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
        "input[value*='bsend']",
        "input[value*='Abgeben']",
        "input[value*='Submit']",
    ]:
        btn = page.query_selector(selector)
        if btn:
            btn.click()
            break

    page.wait_for_load_state("networkidle")
    print("[submit_hw] Upload complete.")
    page.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_submission_box(course: str, sheet: str) -> dict:
    if not DESTINATIONS_FILE.exists():
        raise RuntimeError("destinations.json not found. Run the crawlers first.")
    data = json.loads(DESTINATIONS_FILE.read_text())

    course_lower = course.lower()
    sheet_lower  = sheet.lower()

    for course_name, course_data in data.items():
        if course_lower not in course_name.lower():
            continue
        boxes = course_data.get("tumonline", {}).get("submission_boxes", [])
        for box in boxes:
            if sheet_lower in box["name"].lower():
                return box
        # If no exact sheet match, return first box with a warning
        if boxes:
            print(f"[submit_hw] WARNING: No box matching '{sheet}' — using first available: {boxes[0]['name']}")
            return boxes[0]

    raise RuntimeError(
        f"No submission box found for course='{course}' sheet='{sheet}'.\n"
        f"Run 'python -m crawlers.tumonline_crawler' to refresh destinations."
    )
