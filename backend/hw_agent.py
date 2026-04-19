# hw_agent.py - Autonomous Homework Submission Agent
# Reads HW PDF → identifies course → generates TUM Deckblatt → merges → compresses to 5MB

import os
import re
import json
import boto3
from fpdf import FPDF
from datetime import datetime
import fitz

# Map known course codes -> full names (for regex-based fallback)
COURSE_CODE_MAP = {
    "IN0001": "Einfuehrung in die Informatik",
    "IN0003": "Praktikum Grundlagen der Programmierung",
    "IN0004": "Einfuehrung in die Rechnerarchitektur",
    "IN0006": "Algorithmen und Datenstrukturen",
    "IN0007": "Einfuehrung in die Softwaretechnik",
    "IN0009": "Grundlagen Betriebssysteme",
    "IN0010": "Grundlagen Datenbanken",
    "IN0011": "Einfuehrung in die Theoretische Informatik",
    "IN0015": "Diskrete Strukturen",
    "IN0018": "Grundlagen Rechnernetze und Verteilte Systeme",
    "IN0019": "Numerisches Programmieren",
    "IN2001": "Betriebssysteme advanced",
    "IN2003": "Computergrafik",
    "IN2010": "Compilerbau",
    "IN2064": "Programmierparadigmen",
    "IN4010": "Kryptographie",
    "IN4152": "Cloud Computing",
    "IN4189": "Distributed Systems",
    "IN4199": "Deep Learning",
    "MA0001": "Analysis 1 fuer Informatik",
    "MA0002": "Analysis 2 fuer Informatik",
    "MA0003": "Lineare Algebra 1 fuer Informatik",
    "MA0004": "Lineare Algebra 2 fuer Informatik",
    "MA0901": "Diskrete Strukturen",
    "MA0902": "Analysis fuer Informatik",
    "MA2409": "Diskrete Wahrscheinlichkeitstheorie",
}


def _extract_hints(hw_text: str) -> dict:
    """Pull course code and Blatt number out of the HW text via regex."""
    code_match = re.search(r"\b(IN\d{4}|MA\d{4})\b", hw_text)
    course_code = code_match.group(1) if code_match else None
    course_name = COURSE_CODE_MAP.get(course_code) if course_code else None

    blatt_match = re.search(
        r"(?:Uebungs?|Hausaufgaben?|Aufgaben?|Tutor|)\s*[Bb]latt[\s:#]*(\d+)",
        hw_text,
    )
    blatt_number = int(blatt_match.group(1)) if blatt_match else None

    return {
        "course_code": course_code,
        "course_name": course_name,
        "blatt_number": blatt_number,
    }

# Absolute paths — don't depend on where uvicorn was launched from
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DECKBLATT_TMP = os.path.join(DATA_DIR, "deckblatt_temp.pdf")
MERGED_TMP = os.path.join(DATA_DIR, "merged_temp.pdf")
FINAL_PDF = os.path.join(DATA_DIR, "final_submission.pdf")

bedrock = boto3.client('bedrock-runtime', region_name='eu-north-1')
MODEL_ID = 'eu.anthropic.claude-sonnet-4-5-20250929-v1:0'

MAX_SIZE_MB = 5
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

TUM_COURSES = """
INFORMATIK PFLICHTMODULE (FPSO 2024/25):
- IN0001 Einfuehrung in die Informatik (EidI)
- IN0003 Praktikum Grundlagen der Programmierung (PGdP)
- IN0004 Einfuehrung in die Rechnerarchitektur (ERA)
- IN0006 Algorithmen und Datenstrukturen (AuD)
- IN0007 Einfuehrung in die Softwaretechnik (ESE)
- IN0009 Grundlagen Betriebssysteme (BS / GBS)
- IN0010 Grundlagen Datenbanken (GDB)
- IN0011 Einfuehrung in die Theoretische Informatik (EITIIT)
- IN0012 Bachelor-Praktikum
- IN0014 Seminar
- IN0018 Grundlagen Rechnernetze und Verteilte Systeme (GRNVS)
- IN0019 Numerisches Programmieren
- IN0015 Diskrete Strukturen (DS)
- IN2001 Betriebssysteme advanced
- IN2003 Computergrafik
- IN2010 Compilerbau
- IN2064 Programmierparadigmen
- IN4010 Kryptographie
- IN4152 Cloud Computing
- IN4189 Distributed Systems
- IN4199 Deep Learning

MATHEMATIK PFLICHTMODULE (FPSO 2024/25):
- MA0001 Analysis 1 fuer Informatik
- MA0002 Analysis 2 fuer Informatik
- MA0003 Lineare Algebra 1 fuer Informatik
- MA0004 Lineare Algebra 2 fuer Informatik
- MA0901 Diskrete Strukturen (DS)
- MA2409 Diskrete Wahrscheinlichkeitstheorie (DWT)
- MA0902 Analysis fuer Informatik (older FPSO still in use)
"""


def _ascii_safe(text: str) -> str:
    """Helvetica (Core14) can't render Umlauts. Normalize to ASCII."""
    if not text:
        return ""
    replacements = {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "ß": "ss", "–": "-", "—": "-", "…": "...",
        "„": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    # Drop anything else non-latin1 so FPDF doesn't crash
    return text.encode("latin-1", "replace").decode("latin-1")


def calculate_blatt_number() -> int:
    semester_start = datetime(2026, 4, 14)
    today = datetime.now()
    weeks = (today - semester_start).days // 7 + 1
    return max(1, weeks)


def compress_pdf(input_path: str, output_path: str, max_bytes: int = MAX_SIZE_BYTES) -> str:
    doc = fitz.open(input_path)
    current_size = os.path.getsize(input_path)

    if current_size <= max_bytes:
        print(f"PDF already under {MAX_SIZE_MB}MB ({current_size/1024/1024:.1f}MB)")
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return output_path

    print(f"Compressing: {current_size/1024/1024:.1f}MB -> target {MAX_SIZE_MB}MB")

    for image_quality in [85, 70, 50, 30, 15]:
        temp_path = output_path + ".tmp.pdf"
        new_doc = fitz.open()
        for page in doc:
            mat = fitz.Matrix(1.5, 1.5)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
            img_page.insert_image(img_page.rect, pixmap=pix)

        new_doc.save(temp_path, garbage=4, deflate=True, deflate_images=True)
        new_doc.close()

        new_size = os.path.getsize(temp_path)
        print(f"  Quality {image_quality}%: {new_size/1024/1024:.1f}MB")

        if new_size <= max_bytes:
            os.replace(temp_path, output_path)
            doc.close()
            return output_path

        if os.path.exists(temp_path):
            os.remove(temp_path)

    # Last resort — lowest quality
    new_doc = fitz.open()
    for page in doc:
        mat = fitz.Matrix(1.0, 1.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
        img_page.insert_image(img_page.rect, pixmap=pix)

    new_doc.save(output_path, garbage=4, deflate=True, deflate_images=True)
    new_doc.close()
    doc.close()
    return output_path


def identify_course(hw_text: str) -> dict:
    semester_blatt = calculate_blatt_number()
    hints = _extract_hints(hw_text)
    print(f"Regex hints: {hints}")
    print(f"First 500 chars of HW:\n{hw_text[:500]}\n---")

    # If we already have strong regex hits, we can skip Claude (faster + cheaper)
    if hints["course_code"] and hints["blatt_number"]:
        return {
            "course_code": hints["course_code"],
            "course_name": hints["course_name"] or hints["course_code"],
            "short_name": hints["course_name"] or hints["course_code"],
            "blatt_number": max(1, hints["blatt_number"]),
            "confidence": 0.95,
            "reason": "Matched course code and Blatt number directly in PDF text.",
        }

    prompt = f"""You are analyzing a TUM Informatik student's homework PDF.

Extracted text from the homework PDF (first 1500 chars):
\"\"\"
{hw_text[:1500]}
\"\"\"

Hints from regex scan:
- Detected course code: {hints["course_code"] or "(none)"}
- Detected course name: {hints["course_name"] or "(none)"}
- Detected Blatt number: {hints["blatt_number"] or "(none)"}

Available TUM courses (pick the closest match):
{TUM_COURSES}

Today: {datetime.now().strftime('%d.%m.%Y')}
Current semester week (fallback if Blatt not found): {semester_blatt}

Rules:
- If a course code is visible in the text or hints, prefer that.
- Blatt number should be the integer labelled on the PDF (look for "Blatt", "Uebungsblatt", "Aufgabenblatt"). If none, use {semester_blatt}.
- If you genuinely cannot tell, still pick the most likely course — do not return UNKNOWN.

Respond with ONLY valid JSON, no prose:
{{
  "course_code": "IN0009",
  "course_name": "Grundlagen Betriebssysteme",
  "short_name": "BS",
  "blatt_number": 3,
  "confidence": 0.9,
  "reason": "Short explanation based on the PDF text."
}}"""

    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 400,
                'messages': [{'role': 'user', 'content': prompt}]
            })
        )
        result = json.loads(response['body'].read())
        text = result['content'][0]['text']
        print(f"Claude raw response for course ID:\n{text}\n---")

        start = text.find('{')
        end = text.rfind('}') + 1
        parsed = json.loads(text[start:end])
        # Hygiene: never let blatt_number be <1
        parsed["blatt_number"] = max(1, int(parsed.get("blatt_number") or semester_blatt))
        # If Claude gave up, use regex hints
        if parsed.get("course_code") in (None, "", "UNKNOWN"):
            if hints["course_code"]:
                parsed["course_code"] = hints["course_code"]
                parsed["course_name"] = hints["course_name"] or hints["course_code"]
                parsed["short_name"] = hints["course_name"] or hints["course_code"]
                parsed["confidence"] = max(parsed.get("confidence", 0.0), 0.7)
                parsed["reason"] = "Claude failed, used regex match"
        return parsed
    except Exception as e:
        print(f"identify_course fell through to fallback: {type(e).__name__}: {e}")
        return {
            "course_code": hints["course_code"] or "UNKNOWN",
            "course_name": hints["course_name"] or "Unknown Course",
            "short_name": hints["course_name"] or "Unknown",
            "blatt_number": max(1, hints["blatt_number"] or semester_blatt),
            "confidence": 0.3 if hints["course_code"] else 0.0,
            "reason": "Claude error — using regex hints.",
        }


def generate_deckblatt(
    members: list,
    group_number: str,
    course_code: str,
    course_name: str,
    blatt_number: int,
    output_path: str = DECKBLATT_TMP,
) -> str:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_margins(20, 20, 20)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(10)
    pdf.cell(0, 8, _ascii_safe("Abgabeaufgaben fuer"), align="C",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _ascii_safe(f"{course_name} [{course_code}]"), align="C",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 7, "im SoSe 2026", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(40, 10, "Blatt Nr.:", new_x="RIGHT", new_y="TOP")
    pdf.set_font("Helvetica", "", 14)
    display_blatt = max(1, int(blatt_number or 1))
    pdf.cell(0, 10, f"  {display_blatt}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(55, 10, _ascii_safe(f"Gruppen-Nr.:  {group_number}"),
             new_x="RIGHT", new_y="TOP")
    pdf.cell(0, 10, _ascii_safe("    Mitglieder der Gruppe (Name, Matr.-Nr.):"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    col_num = 12
    col_name = 90
    col_mat = 60

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(col_num, 10, "", border=1, fill=True, new_x="RIGHT", new_y="TOP")
    pdf.cell(col_name, 10, "Name", border=1, fill=True, new_x="RIGHT", new_y="TOP")
    pdf.cell(col_mat, 10, "Matrikelnummer", border=1, fill=True,
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 11)
    for i in range(4):
        num = str(i + 1)
        name = _ascii_safe(members[i]["name"]) if i < len(members) else ""
        matrikel = _ascii_safe(members[i]["matrikel"]) if i < len(members) else ""
        pdf.cell(col_num, 12, num, border=1, new_x="RIGHT", new_y="TOP")
        pdf.cell(col_name, 12, name, border=1, new_x="RIGHT", new_y="TOP")
        pdf.cell(col_mat, 12, matrikel, border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _ascii_safe("Von Korrekteur/in auszufuellen"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    aufgaben = ["Abgabeaufgabe", "2", "3", "4", "5", "6", "7"]
    col_w = [40, 18, 18, 18, 18, 18, 18]

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(230, 230, 230)
    for label, w in zip(aufgaben, col_w):
        pdf.cell(w, 9, label, border=1, fill=True, new_x="RIGHT", new_y="TOP")
    pdf.ln(9)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(col_w[0], 9, "Korrigiert", border=1, new_x="RIGHT", new_y="TOP")
    for w in col_w[1:]:
        pdf.cell(w, 9, "", border=1, new_x="RIGHT", new_y="TOP")
    pdf.ln(9)

    pdf.cell(col_w[0], 9, "& sinnvoll bearbeitet", border=1,
             new_x="RIGHT", new_y="TOP")
    for w in col_w[1:]:
        pdf.cell(w, 9, "", border=1, new_x="RIGHT", new_y="TOP")
    pdf.ln(12)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(60, 8, "Zeichen Korrekteur/in: ", new_x="RIGHT", new_y="TOP")
    pdf.line(pdf.get_x(), pdf.get_y() + 7, pdf.get_x() + 60, pdf.get_y() + 7)
    pdf.ln(15)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Anmerkungen:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    box_y = pdf.get_y()
    pdf.rect(20, box_y, 170, 35)
    pdf.ln(3)
    notes = [
        "- Pro Woche wird nur eine Aufgabe des Uebungsblattes ausgewaehlt und korrigiert.",
        "- Durch gemeinschaftliche Abgabe versichern alle Mitglieder gleichmaessige Beteiligung.",
        "- Alle genannten Personen versichern, dass es sich um ihr eigenes Werk handelt.",
    ]
    for note in notes:
        pdf.set_x(22)
        pdf.multi_cell(166, 5, _ascii_safe(note))

    pdf.set_y(-15)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 8, "Generated by ReplyX - TUM AI Campus Copilot", align="C")

    os.makedirs(os.path.dirname(output_path) or DATA_DIR, exist_ok=True)
    pdf.output(output_path)
    return output_path


def merge_pdfs(deckblatt_path: str, hw_path: str,
               output_path: str = MERGED_TMP) -> str:
    result = fitz.open()
    result.insert_pdf(fitz.open(deckblatt_path))
    result.insert_pdf(fitz.open(hw_path))
    os.makedirs(os.path.dirname(output_path) or DATA_DIR, exist_ok=True)
    result.save(output_path)
    result.close()
    return output_path


def process_homework(
    hw_file_path: str,
    members: list,
    group_number: str,
) -> dict:
    print(f"\nReplyX Agent: processing homework for group {group_number}")

    try:
        hw_doc = fitz.open(hw_file_path)
        hw_text = "".join(page.get_text() for page in hw_doc)
        hw_doc.close()
        print(f"Extracted {len(hw_text)} chars")

        print("Identifying course with Claude...")
        course_info = identify_course(hw_text)
        print(f"Course: {course_info['course_name']} | Blatt: {course_info['blatt_number']}")

        print("Generating Deckblatt...")
        deckblatt_path = generate_deckblatt(
            members=members,
            group_number=group_number,
            course_code=course_info["course_code"],
            course_name=course_info["course_name"],
            blatt_number=course_info["blatt_number"],
            output_path=DECKBLATT_TMP,
        )

        print("Merging PDFs...")
        merged_path = merge_pdfs(deckblatt_path, hw_file_path, MERGED_TMP)

        print("Compressing for Moodle (max 5MB)...")
        compress_pdf(merged_path, FINAL_PDF)

        final_size = os.path.getsize(FINAL_PDF)

        return {
            "status": "success",
            "course": f"{course_info['course_name']} [{course_info['course_code']}]",
            "blatt_number": course_info["blatt_number"],
            "confidence": course_info["confidence"],
            "final_pdf": FINAL_PDF,
            "size_mb": round(final_size / 1024 / 1024, 2),
            "message": f"Ready! Deckblatt added, compressed to {round(final_size/1024/1024, 2)}MB for Moodle."
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
        }


if __name__ == "__main__":
    result = process_homework(
        hw_file_path=os.path.join(DATA_DIR, "gruppe205_blatt02.pdf"),
        members=[
            {"name": "Ilya Kats", "matrikel": "03805006"},
            {"name": "Ksenija Zimina", "matrikel": "03780490"},
            {"name": "Valerina Kankacheva", "matrikel": "03782935"},
        ],
        group_number="205"
    )
    print(result)
