"""
actions/slide_search.py — index slide PDFs and search with HNSW cosine distance.

Model: nomic-ai/nomic-embed-text-v1 (768-dim, ~8GB RAM recommended)
DB:    Qdrant local mode (no Docker) with HNSW index

Usage
-----
from actions.slide_search import index_pdf, search

index_pdf("slides/algo_lecture3.pdf")
hits = search("master theorem recurrence relation", top_k=5)
"""
import fitz  # pymupdf
import subprocess
from pathlib import Path
from config import SLIDES_DB, EMBED_MODEL

try:
    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct, HnswConfigDiff
    )
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

COLLECTION = "slides"
VECTOR_DIM = 768      # nomic-embed-text-v1 and bge-large-en-v1.5 are 768/1024 resp.

_model  = None
_client = None


def _get_model():
    global _model
    if _model is None:
        if not _DEPS_OK:
            raise ImportError("Run: pip install sentence-transformers qdrant-client pymupdf")
        print(f"[search] Loading embedding model: {EMBED_MODEL}  (first load may take a moment)")
        _model = SentenceTransformer(EMBED_MODEL, trust_remote_code=True)
    return _model


def _get_client():
    global _client
    if _client is None:
        Path(SLIDES_DB).mkdir(parents=True, exist_ok=True)
        _client = QdrantClient(path=str(SLIDES_DB))
        # create collection if it doesn't exist
        existing = [c.name for c in _client.get_collections().collections]
        if COLLECTION not in existing:
            _client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(
                    size=VECTOR_DIM,
                    distance=Distance.COSINE,
                ),
                hnsw_config=HnswConfigDiff(
                    m=16,               # neighbours per node — higher = better recall, more RAM
                    ef_construct=200,   # candidates during indexing — higher = better quality
                    full_scan_threshold=10_000,
                ),
            )
            print(f"[search] Created Qdrant collection '{COLLECTION}' with HNSW + cosine.")
    return _client


def index_pdf(pdf_path: str):
    """Extract text from each slide/page and upsert into the vector DB."""
    model  = _get_model()
    client = _get_client()
    doc    = fitz.open(pdf_path)
    points = []

    print(f"[search] Indexing {len(doc)} pages from {pdf_path} ...")
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text").strip()
        if not text:
            continue

        # nomic-embed-text uses task prefixes for better quality
        vec = model.encode(f"search_document: {text}", normalize_embeddings=True).tolist()

        # Use a stable ID based on file + page to allow re-indexing
        point_id = abs(hash(f"{pdf_path}:{page_num}")) % (2**31)

        points.append(PointStruct(
            id=point_id,
            vector=vec,
            payload={
                "pdf":      pdf_path,
                "page":     page_num,       # 0-indexed
                "page_num": page_num + 1,   # 1-indexed for display
                "text":     text[:500],     # store first 500 chars for preview
            },
        ))

    if points:
        client.upsert(collection_name=COLLECTION, points=points)
        print(f"[search] Indexed {len(points)} slides (skipped {len(doc)-len(points)} empty pages).")
    else:
        print("[search] No text found in PDF — is it a scanned PDF? Try OCR first.")

    doc.close()


def search(query: str, top_k: int = 5, open_best: bool = True) -> list[dict]:
    """
    Find the top_k most relevant slides for a query.
    If open_best=True, opens the best match PDF to the exact page.
    """
    model  = _get_model()
    client = _get_client()

    q_vec = model.encode(f"search_query: {query}", normalize_embeddings=True).tolist()

    hits = client.search(
        collection_name=COLLECTION,
        query_vector=q_vec,
        limit=top_k,
        with_payload=True,
        score_threshold=0.3,   # ignore very weak matches
    )

    if not hits:
        print("[search] No results found.")
        return []

    print(f"\n[search] Top {len(hits)} results for: '{query}'\n")
    results = []
    for i, hit in enumerate(hits):
        p = hit.payload
        print(f"  {i+1}. [score {hit.score:.3f}]  {p['pdf']}  p.{p['page_num']}")
        print(f"       {p['text'][:120].replace(chr(10), ' ')}...")
        results.append({**p, "score": hit.score})

    if open_best and results:
        _open_pdf_page(results[0]["pdf"], results[0]["page"])

    return results


def _open_pdf_page(pdf_path: str, page: int):
    """Open a PDF to a specific page using the system viewer."""
    import sys, os
    if sys.platform == "darwin":
        # macOS: open with Preview (doesn't support --page directly, use osascript)
        subprocess.Popen(["open", pdf_path])
    elif sys.platform == "win32":
        # Windows: SumatraPDF supports page jumping
        sumatra = r"C:\Program Files\SumatraPDF\SumatraPDF.exe"
        if os.path.exists(sumatra):
            subprocess.Popen([sumatra, f"-page", str(page + 1), pdf_path])
        else:
            os.startfile(pdf_path)
    else:
        # Linux: evince, okular, or zathura — all support page index
        for viewer in ["evince", "okular", "zathura"]:
            try:
                if viewer == "evince":
                    subprocess.Popen([viewer, f"--page-index={page}", pdf_path])
                elif viewer == "okular":
                    subprocess.Popen([viewer, f"--page", str(page + 1), pdf_path])
                elif viewer == "zathura":
                    subprocess.Popen([viewer, "-P", str(page + 1), pdf_path])
                break
            except FileNotFoundError:
                continue
