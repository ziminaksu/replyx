# embeddings.py - Vector database setup and semantic search
# Uses ChromaDB with HNSW index for fast cosine similarity search
import chromadb
import json
import numpy as np
import os

def init_db(descriptions_path="data/descriptions/slides.json",
            embeddings_path="data/descriptions/embeddings.npy"):
    client = chromadb.PersistentClient("./chroma_db")

     # cosine similarity
    col = client.get_or_create_collection(
        "slides", metadata={"hnsw:space": "cosine"})
    with open(descriptions_path) as f:
        slides = json.load(f)
#Load pre-computed embeddings from Qwen3
    embs = np.load(embeddings_path).tolist()
    col.add(
        ids=[f"s{s['id']}" for s in slides],
        embeddings=embs,
        documents=[s["text"] for s in slides],
        metadatas=[{
            "pdf": s["pdf"],
            "slide_num": s["slide_num"],
            "image_path": s["image_path"]
        } for s in slides]
    )
    print(f"Loaded {col.count()} slides into ChromaDB")
    return col

def search(col, embed_model, query, top_k=3):
    """
    Find the most relevant slides for a student's question.
    Uses HNSW graph traversal - much faster than brute force search.
    """
    q_emb = embed_model.encode(query).tolist()
    res = col.query(query_embeddings=[q_emb], n_results=top_k)
    return res["documents"][0], res["metadatas"][0]

if __name__ == "__main__":
    from sentence_transformers import SentenceTransformer
    embed_model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")
    col = init_db()
    
    # Test search
    docs, metas = search(col, embed_model, "What is virtualization?")
    print("\nTest search result:")
    print(docs[0][:200])