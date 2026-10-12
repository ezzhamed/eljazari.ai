from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.documents import Document

from services.vector_index import IndexSettings, LocalVectorIndex


class Embeddings:
    def embed_documents(self, texts):
        return [[float(text), 0.0] for text in texts]

    def embed_query(self, text):
        return [float(text), 0.0]


def documents():
    return [
        Document(page_content=str(n), metadata={"evaluated": n != 2, "topology": "quad"})
        for n in (1, 2, 4)
    ]


def test_ranking_filters_and_persistence(tmp_path):
    settings = IndexSettings(is_persistent=True, persist_directory=str(tmp_path))
    index = LocalVectorIndex.from_documents(
        documents(), Embeddings(), collection_name="test", client_settings=settings
    )
    assert [d.page_content for d in index.similarity_search("2", k=2)] == ["2", "1"]
    selected = index.similarity_search(
        "2",
        filter={
            "$and": [
                {"evaluated": {"$eq": True}},
                {"topology": {"$eq": "quad"}},
            ]
        },
    )
    assert [d.page_content for d in selected] == ["1", "4"]
    index.close()
    reopened = LocalVectorIndex(
        collection_name="test", embedding_function=Embeddings(), client_settings=settings
    )
    assert reopened.count() == 3
    assert reopened.similarity_search("3.9", k=1)[0].page_content == "4"
    reopened.close()


def test_memory_collections_and_concurrent_queries_are_isolated():
    first = LocalVectorIndex.from_documents(documents(), Embeddings(), collection_name="same")
    other = LocalVectorIndex(collection_name="same", embedding_function=Embeddings())
    assert other.count() == 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: first.similarity_search("2", k=1)[0].page_content, range(12))
        )
    assert results == ["2"] * 12
    first.close()
    other.close()


def test_rebuild_replaces_documents_and_failed_embeddings_preserve_previous_index(tmp_path):
    settings = IndexSettings(persist_directory=str(tmp_path))
    first = LocalVectorIndex.from_documents(
        documents(), Embeddings(), collection_name="test", client_settings=settings
    )

    class Broken(Embeddings):
        def embed_documents(self, texts):
            return [[float("nan"), 0] for _ in texts]

    with pytest.raises(ValueError, match="finite"):
        LocalVectorIndex.from_documents(
            documents(), Broken(), collection_name="test", client_settings=settings
        )
    assert first.count() == 3
    replacement = LocalVectorIndex.from_documents(
        documents()[:1], Embeddings(), collection_name="test", client_settings=settings
    )
    assert first.count() == replacement.count() == 1
    first.close()
    replacement.close()


def test_invalid_dimensions_and_filter_operators_fail_closed():
    index = LocalVectorIndex.from_documents(documents(), Embeddings(), collection_name="test")
    with pytest.raises(ValueError, match="equality"):
        index.similarity_search("2", filter={"topology": {"$code": "anything"}})
    index.embeddings.embed_query = lambda text: [1.0]
    with pytest.raises(ValueError, match="dimensions"):
        index.similarity_search("2")
    index.close()
