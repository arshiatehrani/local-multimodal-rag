"""Qdrant connection, collection setup, and CRUD helpers.

Named ``qdrant_store`` (not ``qdrant_client``) to avoid shadowing the installed
``qdrant_client`` package.
"""

import os
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    FilterSelector,
    PayloadSchemaType,
)

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
    # Keyword indexes make space/file filtering and deletion fast. Idempotent:
    # ignore errors if an index already exists.
    for field in ("space_id", "file_id"):
        try:
            client.create_payload_index(
                collection_name=COLLECTION,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass


def _space_filter(space_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="space_id", match=MatchValue(value=space_id))])


def _file_filter(file_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="file_id", match=MatchValue(value=file_id))])


def upsert_points(vectors: list, payloads: list):
    points = [
        PointStruct(id=str(uuid.uuid4()), vector=list(map(float, vec)), payload=pay)
        for vec, pay in zip(vectors, payloads)
    ]
    if points:
        client.upsert(collection_name=COLLECTION, points=points)


def search(query_vector, space_id: str, top_k: int = 20):
    """Return ScoredPoints (with payload) for the query, limited to one space."""
    result = client.query_points(
        collection_name=COLLECTION,
        query=list(map(float, query_vector)),
        limit=top_k,
        with_payload=True,
        query_filter=_space_filter(space_id),
    )
    return result.points


def count_points(space_id: str) -> int:
    """Number of vectors stored for a space (0 if none / collection missing)."""
    try:
        return client.count(
            collection_name=COLLECTION,
            count_filter=_space_filter(space_id),
            exact=True,
        ).count
    except Exception:
        return 0


def delete_by_file(file_id: str) -> None:
    """Remove all vectors belonging to a single file."""
    try:
        client.delete(
            collection_name=COLLECTION,
            points_selector=FilterSelector(filter=_file_filter(file_id)),
        )
    except Exception:
        pass


def delete_by_space(space_id: str) -> None:
    """Remove all vectors belonging to an entire space."""
    try:
        client.delete(
            collection_name=COLLECTION,
            points_selector=FilterSelector(filter=_space_filter(space_id)),
        )
    except Exception:
        pass
