# backend/slides.py
import fitz
import json
import os
from pathlib import Path
from sentence_transformers import SentenceTransformer
import chromadb
from dotenv import load_dotenv

load_dotenv()

print("Loading embedding model...")
embed_model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")
print("Model loaded")

CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
os.makedirs(CHROMA_PATH, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)

try:
    chroma_client.delete_collection("slides")
except:
    pass

collection = chroma_client.create_collection(name="slides")
print("Chroma DB ready")

def process_all_pdfs(slides_dir="data/slides"):
    pdf_files = list(Path(slides_dir).glob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found in {slides_dir}/")
        print("Place your lecture PDFs in data/slides/ folder")
        return
    
    print(f"Found {len(pdf_files)} PDF files")
    
    all_slides = []
    
    for pdf in pdf_files:
        print(f"Processing: {pdf.name}")
        doc = fitz.open(str(pdf))
        total_pages = len(doc)  # Get page count BEFORE closing
        
        for page_num, page in enumerate(doc):
            text = page.get_text()
            
            if text and len(text.strip()) > 30:
                description = f"[{pdf.name} - Slide {page_num}]\n{text.strip()[:1500]}"
            else:
                description = f"[{pdf.name} - Slide {page_num}] Visual slide with diagrams and key concepts."
            
            embedding = embed_model.encode(description).tolist()
            slide_id = f"{pdf.stem}_slide_{page_num:03d}"
            
            collection.add(
                ids=[slide_id],
                embeddings=[embedding],
                documents=[description],
                metadatas=[{
                    "pdf": pdf.name,
                    "slide_num": page_num,
                    "total_pages": total_pages
                }]
            )
            
            all_slides.append({
                "id": slide_id,
                "pdf": pdf.name,
                "slide_num": page_num,
                "text": description[:500]
            })
        
        doc.close()
        print(f"Done: {total_pages} slides from {pdf.name}")
    
    os.makedirs("data/descriptions", exist_ok=True)
    with open("data/descriptions/slides.json", "w", encoding="utf-8") as f:
        json.dump(all_slides, f, ensure_ascii=False, indent=2)
    
    print(f"Success. {collection.count()} slides in database")
    return all_slides

if __name__ == "__main__":
    process_all_pdfs()