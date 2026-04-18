# ReplyX Backend - AI Campus Copilot for TUM Students
# RAG pipeline: Gemini Vision → Qwen3 Embeddings → ChromaDB → Claude Bedrock Socratic Tutor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os
import chromadb
import boto3
import json

load_dotenv()

# AWS Bedrock client - Claude Sonnet via AWS
bedrock = boto3.client('bedrock-runtime', region_name='eu-north-1')
MODEL_ID = 'eu.anthropic.claude-sonnet-4-5-20250929-v1:0'

# Qwen3 embedding model - converts text to vectors for semantic search
embed_model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")

# ChromaDB with HNSW index - finds similar slides in milliseconds
db = chromadb.PersistentClient("./chroma_db")
col = db.get_collection("slides")

app = FastAPI(title="ReplyX API", description="TUM AI Campus Copilot")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class QuestionRequest(BaseModel):
    question: str

class SearchRequest(BaseModel):
    query: str
    top_k: int = 3

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

@app.get("/")
def root():
    return {"status": "ReplyX API running!", "slides": col.count(), "llm": "Claude Sonnet via AWS Bedrock"}

@app.post("/api/search")
def search(req: SearchRequest):
    # Embed query and find top-k similar slides
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
    # Find relevant slides
    q_emb = embed_model.encode(req.question).tolist()
    res = col.query(query_embeddings=[q_emb], n_results=3)
    context = "\n\n".join(res["documents"][0])

    # Socratic tutor prompt - guides student to answer instead of giving it directly
    prompt = f"""You are a Socratic tutor at TUM (Technical University of Munich).
Context from lecture slides:
{context}

Student question: {req.question}

Ask ONE guiding question to help the student think through the answer themselves. 
Never give the full answer directly. Reference the slide content naturally.
Keep it concise and encouraging."""

    answer = ask_claude(prompt)

    return {
        "answer": answer,
        "sources": res["metadatas"][0]
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "slides_count": col.count(), "llm": "AWS Bedrock Claude"}