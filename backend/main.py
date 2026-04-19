# ReplyX Backend - AI Campus Copilot for TUM Students

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os
import base64          # ← NEW
import chromadb
import boto3
import json
import shutil
import re
import fitz            # ← NEW (PyMuPDF — already installed for hw_agent)

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────
BACKEND_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT  = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
CHROMA_PATH   = os.path.join(PROJECT_ROOT, "chroma_db")
DATA_PATH     = os.path.join(PROJECT_ROOT, "data")
UPLOADS_PATH  = os.path.join(DATA_PATH, "uploads")
SLIDES_PATH   = os.path.join(DATA_PATH, "slides")          # ← NEW
FINAL_PDF_PATH = os.path.join(DATA_PATH, "final_submission.pdf")

os.makedirs(CHROMA_PATH,  exist_ok=True)
os.makedirs(UPLOADS_PATH, exist_ok=True)
os.makedirs(SLIDES_PATH,  exist_ok=True)                   # ← NEW

# ── ChromaDB ─────────────────────────────────────────────────────────────────
db = chromadb.PersistentClient(path=CHROMA_PATH)

try:
    col = db.get_collection("slides")
    print(f"Slides collection loaded: {col.count()} slides")
except Exception as e:
    print(f"Error: {e}")
    print("Run: python3 backend/slides.py first")
    col = None

# ── AWS Bedrock + Embeddings ──────────────────────────────────────────────────
bedrock  = boto3.client('bedrock-runtime', region_name='eu-north-1')
MODEL_ID = 'eu.anthropic.claude-sonnet-4-5-20250929-v1:0'
embed_model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="ReplyX API", description="TUM AI Campus Copilot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Pydantic models ───────────────────────────────────────────────────────────
class QuestionRequest(BaseModel):
    question: str

class SearchRequest(BaseModel):
    query: str
    top_k: int = 3

class MoodleSyncRequest(BaseModel):          # ← NEW
    username: str
    password: str

# ── Helper: call Claude via Bedrock ──────────────────────────────────────────
def ask_claude(prompt: str) -> str:
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'max_tokens': 1000,
            'messages': [{'role': 'user', 'content': prompt}]
        })
    )
    result = json.loads(response['body'].read())
    return result['content'][0]['text']

# ── Helper: render one slide page → base64 PNG ───────────────────────────────
def render_slide_image(pdf_name: str, page_num: int) -> str | None:
    """
    Opens data/slides/<pdf_name>, renders page <page_num> at 1.8× zoom,
    returns a base64-encoded PNG string, or None on any error.
    """
    pdf_path = os.path.join(SLIDES_PATH, pdf_name)
    if not os.path.exists(pdf_path):
        return None
    try:
        doc = fitz.open(pdf_path)
        if page_num >= len(doc):
            doc.close()
            return None
        page = doc[page_num]
        mat  = fitz.Matrix(1.8, 1.8)          # crisp but not huge
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("utf-8")
    except Exception:
        return None

# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    count = col.count() if col else 0
    return {
        "status": "ReplyX API running",
        "slides": count,
        "llm": "Claude Sonnet via AWS Bedrock"
    }

# ── Search ────────────────────────────────────────────────────────────────────
@app.post("/api/search")
def search(req: SearchRequest):
    if not col:
        return {"error": "Database not initialized"}
    q_emb = embed_model.encode(req.query).tolist()
    res   = col.query(query_embeddings=[q_emb], n_results=req.top_k)
    return {
        "query": req.query,
        "results": [
            {"text": doc[:300], "metadata": meta}
            for doc, meta in zip(res["documents"][0], res["metadatas"][0])
        ]
    }

# ── Ask (RAG Q&A) ─────────────────────────────────────────────────────────────
@app.post("/api/ask")
def ask(req: QuestionRequest):
    if not col:
        return {"answer": "Database not ready.", "sources": [], "slide_image": None}

    question_lower = req.question.lower()

    # 1. Detect which PDF the student wants
    pdf_keywords = {
        'dwt':             'dwt_script_26.pdf',
        'probability':     'dwt_script_26.pdf',
        'virtualisierung': 'Virtualisierung.pdf',
        'virtualization':  'Virtualisierung.pdf',
        'virt':            'Virtualisierung.pdf',
        'speicherverwaltung': 'Speicherverwaltung.pdf',
        'memory':          'Speicherverwaltung.pdf',
        'synchronisation': 'Synchronisation.pdf',
        'sync':            'Synchronisation.pdf',
    }

    target_pdf = None
    for keyword, pdf_name in pdf_keywords.items():
        if keyword in question_lower:
            target_pdf = pdf_name
            break

    # 2. Detect explicit slide / page number
    patterns = [
        r'(?:page|slide|slide #?|nummer|folio)\s+(\d+)',
        r'^(\d+)$',
        r'give me (\d+)',
        r'show me (\d+)',
        r'number (\d+)',
        r'#(\d+)',
    ]
    slide_num = None
    for pattern in patterns:
        match = re.search(pattern, question_lower)
        if match:
            slide_num = int(match.group(1))
            break

    # ── INTERNAL helper: build response dict with optional slide image ────────
    def make_response(answer: str, sources: list) -> dict:
        """
        Given the LLM answer and source metadata list, attempt to attach
        a base64 PNG of the most relevant slide so the frontend can display it.
        """
        slide_image = None
        if sources:
            first    = sources[0]
            pdf_name = first.get("pdf")
            page_num = first.get("slide_num", 0)
            if pdf_name:
                # render_slide_image returns None if file is missing
                slide_image = render_slide_image(pdf_name, page_num)

        return {
            "answer":      answer,
            "sources":     sources,
            "slide_image": slide_image,   # base64 PNG or null
        }

    # 3. Exact slide-number lookup
    if slide_num is not None:
        all_slides     = col.get()
        matching_slides = []

        for i, meta in enumerate(all_slides['metadatas']):
            pdf_name = meta.get('pdf', '')
            if target_pdf and pdf_name != target_pdf:
                continue
            if meta.get('slide_num') == slide_num:
                matching_slides.append({
                    'document': all_slides['documents'][i],
                    'metadata': meta
                })

        if matching_slides:
            slide_data = matching_slides[0]
            context    = slide_data['document']
            sources    = [slide_data['metadata']]
            prompt = f"""You are ReplyX. The student asked for slide {slide_num} from {sources[0].get('pdf')}.

Slide content:
{context}

Answer based ONLY on this slide. Format with **bold** for key terms. Keep concise."""
            answer = ask_claude(prompt)
            return make_response(answer, sources)

        # Fallback: semantic search within same PDF
        if target_pdf:
            q_emb = embed_model.encode(req.question).tolist()
            res   = col.query(
                query_embeddings=[q_emb],
                n_results=3,
                where={"pdf": target_pdf},
            )
            if res["documents"][0]:
                context = "\n\n".join(res["documents"][0])
                prompt  = f"""You are ReplyX. The student asked for slide {slide_num} from {target_pdf} but it wasn't found.

Here are the closest slides I found from {target_pdf}:
{context}

Tell the student slide {slide_num} wasn't found, but offer these related slides instead."""
                answer = ask_claude(prompt)
                return make_response(answer, res["metadatas"][0])

        return make_response(
            f"Slide {slide_num} from {target_pdf} not found. Try a different slide number.",
            []
        )

    # 4. Pure semantic search
    q_emb = embed_model.encode(req.question).tolist()
    res   = col.query(query_embeddings=[q_emb], n_results=5)

    if not res["documents"][0]:
        return make_response("No relevant slides found.", [])

    context = "\n\n".join(res["documents"][0])
    prompt  = f"""You are ReplyX. Answer based on these slides:

{context}

Student question: {req.question}

Rules:
- Reference PDF name and slide number
- Use **bold** for key terms
- If slides are from the wrong course, say so
- Be concise"""

    answer = ask_claude(prompt)
    return make_response(answer, res["metadatas"][0])

# ── Slide image endpoint ──────────────────────────────────────────────────────
@app.get("/api/slide-image")
def slide_image_endpoint(pdf: str, page: int = 0):
    """
    Returns a base64 PNG of a specific slide.
    Example: GET /api/slide-image?pdf=Virtualisierung.pdf&page=3
    """
    img = render_slide_image(pdf, page)
    if img is None:
        return {"error": f"Could not render {pdf} page {page}"}
    return {"image": img, "pdf": pdf, "page": page}

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    count = col.count() if col else 0
    return {"status": "ok", "slides_count": count, "llm": "AWS Bedrock Claude"}

# ── Homework submit ───────────────────────────────────────────────────────────
@app.post("/api/submit-homework")
async def submit_homework(
    file: UploadFile = File(...),
    group_number: str = Form(...),
    members: str = Form(...),
):
    from .hw_agent import process_homework
    hw_path = os.path.join(UPLOADS_PATH, file.filename)
    with open(hw_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    members_list = json.loads(members)
    result = process_homework(hw_path, members_list, group_number)
    return result

# ── Homework download ─────────────────────────────────────────────────────────
@app.get("/api/download-hw")
def download_hw():
    if not os.path.exists(FINAL_PDF_PATH):
        return {"error": "No file found. Submit homework first."}
    return FileResponse(
        FINAL_PDF_PATH,
        media_type="application/pdf",
        filename="final_submission.pdf"
    )

# ── Moodle sync ───────────────────────────────────────────────────────────────
@app.post("/api/moodle-sync")
async def moodle_sync(req: MoodleSyncRequest):
    """
    AI agent logs into TUM Moodle via Shibboleth SSO,
    finds all lecture PDFs, downloads new ones, re-indexes ChromaDB.
    """
    from .moodle_agent import MoodleAgent
    agent = MoodleAgent(req.username, req.password)
    return agent.sync()

@app.get("/api/moodle-status")
def moodle_status():
    """How many files have been synced from Moodle so far."""
    cache_path = os.path.join(PROJECT_ROOT, "data", "moodle_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)
        return {
            "synced_files": len(cache.get("downloaded", [])),
            "last_sync":    cache.get("last_sync", "never"),
        }
    return {"synced_files": 0, "last_sync": "never"}