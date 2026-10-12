"""Embedded vector search over trusted local documents, without a network server.

SQLite stores JSON metadata and float32 vectors, never pickle or executable
embedding configuration. Embeddings are supplied by the application. Ranking
uses squared L2 distance, matching the previous default collection metric.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from langchain_core.documents import Document


@dataclass
class IndexSettings:
    is_persistent: bool = False
    persist_directory: str | None = None
    anonymized_telemetry: bool = False


def _matches(metadata: dict, condition: dict | None) -> bool:
    if not condition:
        return True
    if "$and" in condition:
        return all(_matches(metadata, c) for c in condition["$and"])
    if "$or" in condition:
        return any(_matches(metadata, c) for c in condition["$or"])
    for key, value in condition.items():
        if isinstance(value, dict):
            if set(value) != {"$eq"}:
                raise ValueError("Only equality metadata filters are supported")
            value = value["$eq"]
        if metadata.get(key) != value:
            return False
    return True


class LocalVectorIndex:
    def __init__(
        self,
        *,
        collection_name: str,
        embedding_function: Any,
        client_settings: IndexSettings | None = None,
        persist_directory: str | None = None,
    ) -> None:
        settings = client_settings or IndexSettings()
        directory = persist_directory or settings.persist_directory
        database = ":memory:"
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            database = str(path / "vectors.sqlite3")
        self.embeddings = embedding_function
        self.name = collection_name
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, timeout=60, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            "collection TEXT NOT NULL, ordinal INTEGER NOT NULL, content TEXT NOT NULL, "
            "metadata TEXT NOT NULL, vector BLOB NOT NULL, "
            "PRIMARY KEY (collection, ordinal))"
        )
        self._db.commit()
        self._collection = self

    def count(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT count(*) FROM documents WHERE collection = ?", (self.name,)
            ).fetchone()[0]

    def delete_collection(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM documents WHERE collection = ?", (self.name,))

    @classmethod
    def from_documents(
        cls,
        documents: list[Document],
        embedding: Any,
        *,
        collection_name: str,
        client_settings: IndexSettings | None = None,
        persist_directory: str | None = None,
        collection_metadata: dict | None = None,
    ) -> LocalVectorIndex:
        store = cls(
            collection_name=collection_name,
            embedding_function=embedding,
            client_settings=client_settings,
            persist_directory=persist_directory,
        )
        vectors = np.asarray(
            embedding.embed_documents([d.page_content for d in documents]), dtype="<f4"
        )
        if vectors.ndim != 2 or len(vectors) != len(documents) or vectors.shape[1] == 0:
            raise ValueError("Invalid document embedding shape")
        if not np.isfinite(vectors).all():
            raise ValueError("Document embeddings must be finite")
        # One transaction: readers see either the previous or the complete index.
        with store._lock, store._db:
            store._db.execute("DELETE FROM documents WHERE collection = ?", (store.name,))
            store._db.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        store.name,
                        i,
                        doc.page_content,
                        json.dumps(doc.metadata, allow_nan=False),
                        vector.tobytes(),
                    )
                    for i, (doc, vector) in enumerate(zip(documents, vectors, strict=True))
                ],
            )
        return store

    def similarity_search(
        self, query: str, k: int = 5, filter: dict | None = None
    ) -> list[Document]:
        if k <= 0:
            return []
        vector = np.asarray(self.embeddings.embed_query(query), dtype="<f4")
        if vector.ndim != 1 or not np.isfinite(vector).all():
            raise ValueError("Query embedding must be a finite vector")
        with self._lock:
            rows = self._db.execute(
                "SELECT content, metadata, vector FROM documents WHERE collection = ? ORDER BY ordinal",
                (self.name,),
            ).fetchall()
        documents, vectors = [], []
        for content, raw_metadata, raw_vector in rows:
            metadata = json.loads(raw_metadata)
            if not _matches(metadata, filter):
                continue
            stored = np.frombuffer(raw_vector, dtype="<f4")
            if stored.shape != vector.shape or not np.isfinite(stored).all():
                raise ValueError("Stored embedding dimensions or values do not match the query")
            documents.append(Document(page_content=content, metadata=metadata))
            vectors.append(stored)
        if not documents:
            return []
        distances = np.sum((np.asarray(vectors) - vector) ** 2, axis=1)
        order = np.argsort(distances, kind="stable")[:k]
        return [documents[int(i)] for i in order]

    def close(self) -> None:
        with self._lock:
            self._db.close()
