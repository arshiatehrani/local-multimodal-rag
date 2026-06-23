"""Qdrant connection, collection setup, hybrid search, and CRUD helpers."""

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
    MatchText,
    FilterSelector,
    PayloadSchemaType,
    Range,
)

COLLECTION = "multimodal_rag"
DIM = 2048

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
    indexes = {
        "space_id": PayloadSchemaType.KEYWORD,
        "file_id": PayloadSchemaType.KEYWORD,
        "text": PayloadSchemaType.TEXT,
        "page": PayloadSchemaType.INTEGER,
        "page_from_end": PayloadSchemaType.INTEGER,
        "paragraph_index": PayloadSchemaType.INTEGER,
        "global_paragraph_index": PayloadSchemaType.INTEGER,
        "word_start": PayloadSchemaType.INTEGER,
        "word_end": PayloadSchemaType.INTEGER,
        "doc_word_start": PayloadSchemaType.INTEGER,
        "doc_word_end": PayloadSchemaType.INTEGER,
        "page_word_start": PayloadSchemaType.INTEGER,
        "page_word_end": PayloadSchemaType.INTEGER,
        "para_word_start": PayloadSchemaType.INTEGER,
        "para_word_end": PayloadSchemaType.INTEGER,
        "doc_word_count": PayloadSchemaType.INTEGER,
        "page_word_count": PayloadSchemaType.INTEGER,
        "para_word_count": PayloadSchemaType.INTEGER,
        "region": PayloadSchemaType.KEYWORD,
        "para_position_on_page": PayloadSchemaType.KEYWORD,
        "page_position": PayloadSchemaType.KEYWORD,
        "leading_words": PayloadSchemaType.TEXT,
    }
    for field, schema in indexes.items():
        try:
            client.create_payload_index(
                collection_name=COLLECTION, field_name=field, field_schema=schema,
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


def search(query_vector, space_id: str, top_k: int = 20, extra_filter: Filter | None = None):
    must = list(_space_filter(space_id).must)
    if extra_filter is not None and extra_filter.must:
        must.extend(extra_filter.must)
    flt = Filter(must=must)
    result = client.query_points(
        collection_name=COLLECTION,
        query=list(map(float, query_vector)),
        limit=top_k,
        with_payload=True,
        query_filter=flt,
    )
    return result.points


def _positional_filter(space_id: str, hints: dict) -> Filter | None:
    """Build a Qdrant filter from parsed positional hints."""
    if not hints:
        return None
    must = [FieldCondition(key="space_id", match=MatchValue(value=space_id))]

    if "page" in hints:
        must.append(FieldCondition(key="page", match=MatchValue(value=hints["page"])))
    if "page_from_end" in hints:
        must.append(FieldCondition(
            key="page_from_end", match=MatchValue(value=hints["page_from_end"]),
        ))
    if "paragraph_index" in hints:
        must.append(FieldCondition(
            key="paragraph_index", match=MatchValue(value=hints["paragraph_index"]),
        ))
    if "global_paragraph_index" in hints:
        must.append(FieldCondition(
            key="global_paragraph_index", match=MatchValue(value=hints["global_paragraph_index"]),
        ))
    if "region" in hints:
        must.append(FieldCondition(key="region", match=MatchValue(value=hints["region"])))
    if hints.get("para_position_on_page"):
        must.append(FieldCondition(
            key="para_position_on_page",
            match=MatchValue(value=hints["para_position_on_page"]),
        ))

    # Document-absolute word position
    if "word_target" in hints:
        wt = hints["word_target"]
        must.append(FieldCondition(key="doc_word_start", range=Range(lte=wt)))
        must.append(FieldCondition(key="doc_word_end", range=Range(gte=wt)))
    if "doc_word_target" in hints:
        dwt = hints["doc_word_target"]
        must.append(FieldCondition(key="doc_word_start", range=Range(lte=dwt)))
        must.append(FieldCondition(key="doc_word_end", range=Range(gte=dwt)))

    # Page-relative word position
    if "page_word_target" in hints:
        pwt = hints["page_word_target"]
        must.append(FieldCondition(key="page_word_start", range=Range(lte=pwt)))
        must.append(FieldCondition(key="page_word_end", range=Range(gte=pwt)))

    # Paragraph-relative word (requires page or document paragraph scope)
    if "para_word_target" in hints:
        pwt = hints["para_word_target"]
        must.append(FieldCondition(key="para_word_start", range=Range(lte=pwt)))
        must.append(FieldCondition(key="para_word_end", range=Range(gte=pwt)))

    if len(must) == 1:
        return None
    return Filter(must=must)


def _keyword_search(query_vector, query_text: str, space_id: str, top_k: int):
    """Full-text payload match within a space."""
    try:
        flt = Filter(must=[
            FieldCondition(key="space_id", match=MatchValue(value=space_id)),
            FieldCondition(key="text", match=MatchText(text=query_text)),
        ])
        result = client.query_points(
            collection_name=COLLECTION,
            query=list(map(float, query_vector)),
            limit=top_k,
            with_payload=True,
            query_filter=flt,
        )
        return result.points
    except Exception:
        return []


def _rrf_merge(lists: list, top_k: int, k: int = 60) -> list:
    """Reciprocal rank fusion across multiple hit lists."""
    scores: dict = {}
    by_id: dict = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank + 1)
            by_id[hit.id] = hit
    ordered = sorted(scores.keys(), key=lambda i: scores[i], reverse=True)
    return [by_id[i] for i in ordered[:top_k]]


def hybrid_search(
    query_vector,
    query_text: str,
    space_id: str,
    top_k: int = 20,
    pos_hints: dict | None = None,
):
    """Vector + keyword + optional positional filter, merged with RRF."""
    lists = []

    pos_flt = _positional_filter(space_id, pos_hints or {})
    vector_hits = search(query_vector, space_id, top_k, extra_filter=pos_flt)
    lists.append(vector_hits)

    # Also run unconstrained vector search and merge (positional filter may be too strict).
    if pos_flt is not None:
        lists.append(search(query_vector, space_id, top_k))

    kw_hits = _keyword_search(query_vector, query_text, space_id, top_k)
    if kw_hits:
        lists.append(kw_hits)

    hints = pos_hints or {}
    anchor = hints.get("anchor_word") or hints.get("anchor_phrase")
    if anchor:
        extra = _keyword_search(query_vector, anchor, space_id, top_k)
        if extra:
            lists.append(extra)

    # Extract quoted phrases for extra keyword passes.
    import re
    for phrase in re.findall(r'"([^"]+)"', query_text):
        extra = _keyword_search(query_vector, phrase, space_id, top_k // 2)
        if extra:
            lists.append(extra)

    if not lists:
        return []
    if len(lists) == 1:
        return lists[0][:top_k]
    return _rrf_merge(lists, top_k)


def count_points(space_id: str) -> int:
    try:
        return client.count(
            collection_name=COLLECTION,
            count_filter=_space_filter(space_id),
            exact=True,
        ).count
    except Exception:
        return 0


def delete_by_file(file_id: str) -> None:
    try:
        client.delete(
            collection_name=COLLECTION,
            points_selector=FilterSelector(filter=_file_filter(file_id)),
        )
    except Exception:
        pass


def delete_by_space(space_id: str) -> None:
    try:
        client.delete(
            collection_name=COLLECTION,
            points_selector=FilterSelector(filter=_space_filter(space_id)),
        )
    except Exception:
        pass
