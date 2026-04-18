from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from google import genai
from dotenv import load_dotenv
import os
import chromadb
import numpy as np
import json

# RAG pipeline: Gemini Vision → Qwen3 Embeddings → ChromaDB → Socratic Tutor

load_dotenv()
# Google Gemini client for vision + reasoning
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
embed_model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")

db = chromadb.PersistentClient("./chroma_db")
col = db.get_collection("slides")

app = FastAPI()
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class QuestionRequest(BaseModel):
    question: str

class SearchRequest(BaseModel):
    query: str
    top_k: int = 3

@app.get("/")
def root():
    # Health check + slide count
    return {"status": "ReplyX API running!", "slides": col.count()}


#embed the query using Qwen3
# #find top-k most similar slides via cosine similarity
@app.post("/api/search")
def search(req: SearchRequest):
    q_emb = embed_model.encode(req.query).tolist()
    res = col.query(query_embeddings=[q_emb], n_results=req.top_k)
    return {
        "query": req.query,
        "results": [
            {"text": doc[:300], "metadata": meta}
            for doc, meta in zip(res["documents"][0], res["metadatas"][0])
        ]
    }

 
@app.post("/api/ask")
def ask(req: QuestionRequest):
     # Find relevant slides for the student's question
    q_emb = embed_model.encode(req.question).tolist()
    res = col.query(query_embeddings=[q_emb], n_results=3)
    context = "\n\n".join(res["documents"][0])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[{"role": "user", "parts": [{"text": f"""You are a Socratic tutor at TUM.
Context from lecture slides:
{context}

Student question: {req.question}

Ask ONE guiding question first. Never give the full answer directly."""}]}]
    )
    return {
        "answer": response.text,
        "sources": res["metadatas"][0]
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "slides_count": col.count()}

@app.post("/api/upload-embeddings")
def upload_embeddings(data: EmbeddingUpload):
    col.add(
        ids=[f"ext_{hash(str(data.embedding[:5]))}"],
        embeddings=[data.embedding],
        documents=[data.text_preview],
        metadatas=[data.metadata]
    )
    return {"status": "added"}
