"""Qdrant connection, collection setup, and CRUD helpers.

Named ``qdrant_store`` (not ``qdrant_client``) to avoid shadowing the installed
``qdrant_client`` package.
"""

import os
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

COLLECTION = "multimodal_rag"
DIM = 2048  # Qwen3-VL-Embedding-2B output dimension

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def ensure_collection():
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
        )


def upsert_points(vectors: list, payloads: list):
    points = [
        PointStruct(id=str(uuid.uuid4()), vector=list(map(float, vec)), payload=pay)
        for vec, pay in zip(vectors, payloads)
    ]
    if points:
        client.upsert(collection_name=COLLECTION, points=points)


def count_points() -> int:
    """Number of stored vectors. Returns 0 if the collection doesn't exist yet."""
    try:
        return client.count(collection_name=COLLECTION, exact=True).count
    except Exception:
        return 0


def search(query_vector, top_k: int = 20):
    """Return a list of ScoredPoint (with payload) for the query vector."""
    result = client.query_points(
        collection_name=COLLECTION,
        query=list(map(float, query_vector)),
        limit=top_k,
        with_payload=True,
    )
    return result.points
