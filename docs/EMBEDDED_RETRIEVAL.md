# Embedded retrieval index

AircraftVerse retrieval uses an embedded SQLite index with NumPy squared-L2 ranking. It stores document text, JSON metadata and float32 vectors. It has no network listener, server authorization API, remote model configuration or pickle deserialization.

Chroma was removed from the runtime requirements during release preparation because four server advisories had no patched release. This removes that dependency rather than suppressing its audit findings. The application's sentence-transformer/Ollama embedding providers and evaluated-multirotor metadata filters remain in place.

Set `AIRCRAFTVERSE_INDEX_PATH` to choose the index base directory. The older `AIRCRAFTVERSE_CHROMA_PATH` name remains a compatibility alias; the new name wins when both are present. With no override, the default is `data/aircraftverse/vector_index`. Each corpus/model key has its own directory containing `vectors.sqlite3`.

Existing Chroma indexes are not opened or deleted. The first design or `python -m scripts.prepare_assets --index` builds a new index. Its disk size and build time depend on the corpus and embedding model; prior Chroma timings do not measure this implementation.

Tests cover persistence, filtered retrieval, ranking, independent memory collections, concurrent queries, atomic replacement and invalid vectors. Indexes are local generated state and excluded from public archives and image build contexts.
