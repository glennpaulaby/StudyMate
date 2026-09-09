import struct
from study_agent.memory.embeddings import (
    InnKubeEmbedder,
    MockEmbedder,
    cosine_similarity,
    embedding_dim,
    load_sqlite_vector,
    pack_embedding,
    unpack_embedding,
)


def test_innkube_embedder_offline_surface():
    """Verify model plumbing and empty batch short-circuit (no network)."""
    emb = InnKubeEmbedder(api_key="", base_url="https://mock.local", model="octen-embedding-8b")
    assert emb.model == "octen-embedding-8b"
    assert emb.embed([]) == []


def test_pack_unpack_f32_roundtrip():
    """Verify Float32 pack/unpack roundtrips (sqlite-vector wire format)."""
    vec = [1.0, -0.5, 0.25, 3.0]
    blob = pack_embedding(vec)
    assert len(blob) == 16
    assert embedding_dim(blob) == 4
    assert unpack_embedding(blob) == vec


def test_unpack_legacy_f64_for_migration():
    """Verify legacy Float64 blobs remain readable for one-way migration."""
    blob = struct.pack("<2d", 1.0, 2.0)
    assert unpack_embedding(blob, fmt="d") == [1.0, 2.0]
    try:
        unpack_embedding(b"\x00" * 7)
        raise AssertionError("expected ValueError for corrupt blob")
    except ValueError:
        pass


def test_cosine_similarity_values():
    """Verify cosine math incl. zero-vector and dim-mismatch guards."""
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_mock_embedder_scripted_and_offline(tmp_path):
    """Verify MockEmbedder returns scripted vectors and records calls (no network)."""
    _ = tmp_path
    mock = MockEmbedder(vectors={"hello": [1.0, 0.0]}, dim=2, model="mock-v1")
    assert mock.model == "mock-v1"
    assert mock.embed(["hello", "unknown"]) == [[1.0, 0.0], [0.0, 0.0]]
    assert mock.calls == [["hello", "unknown"]]


def test_load_sqlite_vector_extension():
    """Verify the extension loads and reports a version (real SQL, no network)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        assert load_sqlite_vector(conn) is True
        version = conn.execute("SELECT vector_version();").fetchone()[0]
        assert isinstance(version, str) and len(version) > 0
    finally:
        conn.close()
