import json
from datetime import date

from fastapi.testclient import TestClient

import app.main as main_module
from app.auth.principal import create_principal_token
import app.auth.service as auth_service
from app.core.config import get_settings
from app.knowledge.job_store import RedisKnowledgeJobStore
from app.main import app
from app.security.url_fetcher import SafeFetchResult


def _user_headers() -> dict[str, str]:
    settings = get_settings()
    token = create_principal_token(
        "knowledge-admin",
        "user",
        "admin",
        settings.jwt_secret_key,
        settings.jwt_algorithm,
        30,
    )
    return {"Authorization": f"Bearer {token}"}


def _set_admin_role(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "get_auth_connections", lambda: object())
    monkeypatch.setattr(
        auth_service,
        "get_user_by_id",
        lambda _connections, user_id: {"id": user_id, "username": "admin", "role": "admin"},
    )


class FakeEmbeddingService:
    dimensions = 4

    def __init__(self):
        self.texts = []

    def embed_query(self, text):
        self.texts.append(text)
        return [1.0, 0.0, 0.0, 0.0]


class BatchEmbeddingService:
    dimensions = 4

    def __init__(self):
        self.query_texts = []
        self.document_batches = []

    def embed_query(self, text):
        self.query_texts.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeKnowledgeJobRedis:
    def __init__(self):
        self.hashes = {}
        self.commands = []

    def pipeline(self, transaction=True):
        assert transaction is True
        self.commands = []
        return self

    def hset(self, key, mapping):
        self.commands.append(("hset", key, mapping))
        return self

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, _ttl):
        self.commands.append(("expire", key, _ttl))
        return self

    def execute(self):
        _, key, mapping = self.commands[0]
        self.hashes.setdefault(key, {}).update(mapping)
        return [len(mapping), True]

    def eval(self, _script, _numkeys, key, *args):
        if key not in self.hashes:
            return 0
        (
            status_flag,
            status,
            message_flag,
            message,
            result_flag,
            result_json,
            error_flag,
            error_code,
            updated_at,
            _ttl,
        ) = args
        for flag, field, value in (
            (status_flag, "status", status),
            (message_flag, "message", message),
            (result_flag, "result_json", result_json),
            (error_flag, "error_code", error_code),
        ):
            if flag == "1":
                self.hashes[key][field] = value
        self.hashes[key]["updated_at"] = updated_at
        return 1


class FakeCursor:
    def __init__(self):
        self.calls = []
        self._next_row = None
        self.version_id = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        self.calls.append({"sql": sql, "params": params})
        if "SELECT version_id, content_hash FROM documents" in sql:
            self._next_row = {"version_id": self.version_id, "content_hash": ""}
        elif "INSERT INTO documents" in sql:
            self.version_id = params[14] if params and len(params) > 14 else self.version_id
            self._next_row = {"id": 1}
        elif "INSERT INTO document_chunks" in sql:
            self._next_row = {"id": 10}
        else:
            self._next_row = None
        return self

    def fetchone(self):
        return self._next_row

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        self.executed.append({"sql": sql, "params": params})
        return self

    def cursor(self, *args, **kwargs):
        return self.cursor_obj


class FakeConnectionManager:
    def __init__(self, cursor):
        self.cursor = cursor

    def connection(self):
        return FakeConnection(self.cursor)

    def close(self):
        return None


def test_split_markdown_document_keeps_section_path_and_metadata():
    from app.knowledge.vector_store import split_markdown_document

    content = """
# 成都旅游信息

成都适合城市休闲和亲子游。

## 热门景点

成都大熊猫繁育研究基地适合亲子游，建议上午入园。宽窄巷子适合城市漫步。

## 交通提示

地铁覆盖主要景区，节假日建议错峰出行。
"""

    chunks = split_markdown_document(
        title="成都旅游信息",
        content=content,
        metadata={"province": "四川", "city": "成都", "data_type": "景点信息"},
        chunk_size=80,
        overlap=10,
    )

    assert chunks
    assert any(chunk.section == "成都旅游信息 > 热门景点" for chunk in chunks)
    assert all(chunk.metadata["city"] == "成都" for chunk in chunks)
    assert all(chunk.title == "成都旅游信息" for chunk in chunks)
    assert "大熊猫" in " ".join(chunk.content for chunk in chunks)


def test_build_rag_context_deduplicates_chunks_and_keeps_sources():
    from app.knowledge.vector_store import TravelChunkSearchResult, build_rag_context

    first = TravelChunkSearchResult(
        chunk_id="chunk-1",
        title="成都旅游信息",
        section="热门景点",
        content="成都大熊猫繁育研究基地适合亲子游。",
        source_name="四川省文化和旅游厅",
        source_url="https://example.com/chengdu",
        publish_date=date(2026, 6, 1),
        score=0.87,
    )
    duplicate = first
    second = TravelChunkSearchResult(
        chunk_id="chunk-2",
        title="成都交通提示",
        section="交通",
        content="地铁可到达主要景区，节假日建议错峰。",
        source_name="成都文旅",
        source_url=None,
        publish_date=None,
        score=0.81,
    )

    context = build_rag_context([first, duplicate, second], max_chars=500)

    assert context.count("[来源1]") == 1
    assert "四川省文化和旅游厅" in context
    assert "https://example.com/chengdu" in context
    assert "成都文旅" in context


def test_vector_store_ingest_document_saves_document_and_chunks():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=FakeEmbeddingService(),
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    result = store.ingest_document(
        title="成都旅游信息",
        content="# 热门景点\n\n成都大熊猫繁育研究基地适合亲子游。",
        source_name="四川省文化和旅游厅",
        source_url="https://example.com/chengdu",
        source_type="web",
        province="四川",
        city="成都",
        data_type="景点信息",
        publish_date=date(2026, 6, 1),
        metadata={"theme": ["亲子游"]},
    )

    sql_text = "\n".join(call["sql"] for call in cursor.calls)
    assert result["chunks_added"] >= 1
    assert "INSERT INTO documents" in sql_text
    assert "INSERT INTO document_chunks" in sql_text
    assert store.embeddings.texts


def test_vector_store_uses_stable_doc_identity_for_source_updates():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=FakeEmbeddingService(),
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    first = store.ingest_document(
        title="Chengdu Travel Notice",
        content="Original opening hours.",
        source_name="official",
        source_url="https://example.com/chengdu",
        source_type="web",
    )
    second = store.ingest_document(
        title="Chengdu Travel Notice",
        content="Updated opening hours and ticket rules.",
        source_name="official",
        source_url="https://example.com/chengdu",
        source_type="web",
    )

    assert first["doc_id"] == second["doc_id"]


def test_vector_store_chunk_metadata_tracks_update_contract():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=FakeEmbeddingService(),
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    store.ingest_document(
        title="Chengdu Travel",
        content="# Attractions\n\nPanda Base works well for families.",
        source_name="official",
        source_url="https://example.com/chengdu",
        source_type="web",
    )

    chunk_call = next(call for call in cursor.calls if "INSERT INTO document_chunks" in call["sql"])
    chunk_metadata = jsonb_payload(chunk_call["params"][-1])

    assert chunk_metadata["content_hash"].startswith("sha256:")
    assert chunk_metadata["version_id"] == 1
    assert chunk_metadata["chunk_strategy"] == "recursive_markdown_v1"
    assert chunk_metadata["chunk_size"] == 700
    assert chunk_metadata["chunk_overlap"] == 100
    assert chunk_metadata["embedding_dimension"] == 4
    assert chunk_metadata["embedding_model"]
    assert chunk_metadata["is_deleted"] is False


def test_vector_store_ingest_document_batches_chunk_embeddings():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    embeddings = BatchEmbeddingService()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=embeddings,
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    long_section = "Chengdu family travel information. " * 80
    result = store.ingest_document(
        title="Chengdu Travel",
        content=f"# Attractions\n\n{long_section}\n\n# Transport\n\n{long_section}",
        source_name="test",
    )

    assert result["chunks_added"] >= 2
    assert len(embeddings.document_batches) == 1
    assert len(embeddings.document_batches[0]) == result["chunks_added"]


def test_vector_store_search_casts_nullable_filter_parameters():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=FakeEmbeddingService(),
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    store.search_chunks(query="Chengdu family attractions", top_k=5)

    search_sql = next(call["sql"] for call in cursor.calls if "FROM document_chunks" in call["sql"])
    assert "%s::text IS NULL OR c.city = %s::text" in search_sql
    assert "%s::text IS NULL OR c.province = %s::text" in search_sql
    assert "%s::text IS NULL OR c.data_type = %s::text" in search_sql
    assert "%s::date IS NULL OR c.publish_date >= %s::date" in search_sql
    assert "c.is_deleted = false" in search_sql
    assert "d.is_deleted = false" in search_sql


def test_vector_store_keyword_search_uses_bm25_scoring_sql():
    from app.knowledge.vector_store import PostgresTravelVectorStore

    cursor = FakeCursor()
    store = PostgresTravelVectorStore(
        "postgresql://example/test",
        embeddings=FakeEmbeddingService(),
        connection_manager=FakeConnectionManager(cursor),
    )
    store._schema_ready = True

    store.keyword_search("Chengdu Panda Base family travel", k=5)

    keyword_sql = next(call["sql"] for call in cursor.calls if "bm25_score" in call["sql"])
    assert "query_terms AS" in keyword_sql
    assert "idf AS" in keyword_sql
    assert "bm25_score" in keyword_sql
    assert "%s::text[]" in keyword_sql
    assert "c.is_deleted = false" in keyword_sql
    assert "d.is_deleted = false" in keyword_sql


def jsonb_payload(value):
    return getattr(value, "obj", value)


def test_api_ingests_and_searches_travel_documents(monkeypatch):
    _set_admin_role(monkeypatch)
    class FakeStore:
        def __init__(self):
            self.ingested = None
            self.search = None

        def ingest_document(self, **kwargs):
            self.ingested = kwargs
            return {"doc_id": "doc-1", "chunks_added": 2}

        def search_chunks(self, **kwargs):
            self.search = kwargs
            return [
                {
                    "chunk_id": "chunk-1",
                    "title": "成都旅游信息",
                    "section": "热门景点",
                    "content": "成都大熊猫繁育研究基地适合亲子游。",
                    "source_name": "四川省文化和旅游厅",
                    "source_url": "https://example.com/chengdu",
                    "publish_date": date(2026, 6, 1),
                    "score": 0.87,
                }
            ]

    store = FakeStore()
    resources = main_module.get_app_resources()
    original_store = main_module.travel_vector_store
    original_resource_store = resources.travel_vector_store
    main_module.travel_vector_store = store
    resources.travel_vector_store = store
    try:
        client = TestClient(app)
        ingest_response = client.post(
            "/api/knowledge/documents",
            headers=_user_headers(),
            json={
                "title": "成都旅游信息",
                "content": "文档正文",
                "source_name": "四川省文化和旅游厅",
                "source_url": "https://example.com/chengdu",
                "province": "四川",
                "city": "成都",
                "data_type": "景点信息",
                "publish_date": "2026-06-01",
                "metadata": {"theme": ["亲子游"]},
            },
        )
        search_response = client.post(
            "/api/knowledge/search",
            headers=_user_headers(),
            json={
                "query": "成都有哪些适合亲子游的景点？",
                "province": "四川",
                "city": "成都",
                "data_type": "景点信息",
                "top_k": 5,
            },
        )
    finally:
        main_module.travel_vector_store = original_store
        resources.travel_vector_store = original_resource_store

    assert ingest_response.status_code == 200
    assert ingest_response.json()["data"]["chunks_added"] == 2
    assert store.ingested["city"] == "成都"
    assert search_response.status_code == 200
    assert search_response.json()["data"]["query"] == "成都有哪些适合亲子游的景点？"
    assert search_response.json()["data"]["results"][0]["source_name"] == "四川省文化和旅游厅"
    assert store.search["city"] == "成都"


def test_api_ingests_travel_document_from_url(monkeypatch):
    _set_admin_role(monkeypatch)
    class FakeStore:
        def __init__(self):
            self.ingested = None

        def ingest_document(self, **kwargs):
            self.ingested = kwargs
            return {"doc_id": "doc-url-1", "chunks_added": 1}

    calls = []

    class FakeFetcher:
        def fetch(self, url):
            calls.append(url)
            text = "<html><head><title>成都亲子游公告</title></head><body><h1>热门景点</h1><p>熊猫基地适合亲子游。</p></body></html>"
            return SafeFetchResult(text=text, content=text.encode(), content_type="text/html")

    store = FakeStore()
    resources = main_module.get_app_resources()
    original_store = main_module.travel_vector_store
    original_resource_store = resources.travel_vector_store
    original_fetcher = main_module.safe_url_fetcher
    main_module.travel_vector_store = store
    resources.travel_vector_store = store
    main_module.safe_url_fetcher = FakeFetcher()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/knowledge/documents/from-url",
            headers=_user_headers(),
            json={
                "source_url": "https://example.com/chengdu",
                "source_name": "四川省文化和旅游厅",
                "province": "四川",
                "city": "成都",
                "data_type": "景点信息",
            },
        )
    finally:
        main_module.travel_vector_store = original_store
        resources.travel_vector_store = original_resource_store
        main_module.safe_url_fetcher = original_fetcher

    assert response.status_code == 200
    assert response.json()["data"]["chunks_added"] == 1
    assert calls[0] == "https://example.com/chengdu"
    assert store.ingested["title"] == "成都亲子游公告"
    assert "熊猫基地适合亲子游" in store.ingested["content"]
    assert store.ingested["source_type"] == "web"


def test_api_auto_ingest_uses_llm_to_extract_metadata(monkeypatch):
    _set_admin_role(monkeypatch)
    class FakeStore:
        def __init__(self):
            self.ingested = None

        def ingest_document(self, **kwargs):
            self.ingested = kwargs
            return {"doc_id": "doc-auto-1", "chunks_added": 3}

    class FakeMessage:
        content = json.dumps(
            {
                "title": "Chengdu Family Travel Guide",
                "source_name": "uploaded-guide.md",
                "source_type": "upload",
                "publish_date": "2026-06-01",
                "province": "Sichuan",
                "city": "Chengdu",
                "scenic_spot": "Panda Base",
                "data_type": "景点信息",
                "metadata": {"theme": ["family"], "keywords": ["panda"]},
            }
        )

    class FakeLLM:
        def invoke(self, prompt):
            assert "Chengdu Panda Base" in prompt
            return FakeMessage()

    store = FakeStore()
    resources = main_module.get_app_resources()
    original_store = main_module.travel_vector_store
    original_resource_store = resources.travel_vector_store
    main_module.travel_vector_store = store
    resources.travel_vector_store = store
    monkeypatch.setattr(main_module, "create_llm", lambda: FakeLLM())
    try:
        client = TestClient(app)
        response = client.post(
            "/api/knowledge/documents/auto",
            headers=_user_headers(),
            json={
                "file_name": "uploaded-guide.md",
                "content": "# Chengdu Panda Base\n\nFamily visitors should book morning tickets.",
            },
        )
    finally:
        main_module.travel_vector_store = original_store
        resources.travel_vector_store = original_resource_store

    assert response.status_code == 200
    assert response.json()["data"]["chunks_added"] == 3
    assert store.ingested["title"] == "Chengdu Family Travel Guide"
    assert store.ingested["city"] == "Chengdu"
    assert store.ingested["data_type"] == "景点信息"
    assert store.ingested["metadata"]["inference"] == "llm"


def test_api_url_ingest_accepts_only_url(monkeypatch):
    _set_admin_role(monkeypatch)
    class FakeStore:
        def __init__(self):
            self.ingested = None

        def ingest_document(self, **kwargs):
            self.ingested = kwargs
            return {"doc_id": "doc-url-only", "chunks_added": 1}

    class FakeFetcher:
        def fetch(self, _url):
            text = "<html><head><title>Chengdu Notice</title></head><body><p>Chengdu travel notice.</p></body></html>"
            return SafeFetchResult(text=text, content=text.encode(), content_type="text/html")

    store = FakeStore()
    resources = main_module.get_app_resources()
    original_store = main_module.travel_vector_store
    original_resource_store = resources.travel_vector_store
    original_fetcher = main_module.safe_url_fetcher
    main_module.travel_vector_store = store
    resources.travel_vector_store = store
    main_module.safe_url_fetcher = FakeFetcher()
    monkeypatch.setattr(main_module, "create_llm", lambda: None)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/knowledge/documents/from-url",
            headers=_user_headers(),
            json={"source_url": "https://example.com/chengdu"},
        )
    finally:
        main_module.travel_vector_store = original_store
        resources.travel_vector_store = original_resource_store
        main_module.safe_url_fetcher = original_fetcher

    assert response.status_code == 200
    assert store.ingested["source_url"] == "https://example.com/chengdu"
    assert store.ingested["source_type"] == "web"


def test_api_auto_ingest_job_completes(monkeypatch):
    _set_admin_role(monkeypatch)
    class FakeStore:
        def __init__(self):
            self.ingested = None

        def ingest_document(self, **kwargs):
            self.ingested = kwargs
            return {"doc_id": "doc-job-1", "chunks_added": 2}

    store = FakeStore()
    resources = main_module.get_app_resources()
    original_store = main_module.travel_vector_store
    original_resource_store = resources.travel_vector_store
    original_resource_job_store = resources.knowledge_job_store
    main_module.travel_vector_store = store
    resources.travel_vector_store = store
    monkeypatch.setattr(main_module, "create_llm", lambda: None)
    fake_job_store = RedisKnowledgeJobStore(FakeKnowledgeJobRedis(), ttl_seconds=60)
    monkeypatch.setattr(main_module, "knowledge_job_store", fake_job_store)
    resources.knowledge_job_store = fake_job_store
    try:
        client = TestClient(app)
        create_response = client.post(
            "/api/knowledge/documents/auto/jobs",
            headers=_user_headers(),
            json={
                "file_name": "chengdu.md",
                "content": "# Chengdu\n\nFamily travel guide.",
            },
        )
        assert create_response.status_code == 200
        job_id = create_response.json()["data"]["job_id"]
        status_response = client.get(f"/api/knowledge/documents/jobs/{job_id}", headers=_user_headers())
    finally:
        main_module.travel_vector_store = original_store
        resources.travel_vector_store = original_resource_store
        resources.knowledge_job_store = original_resource_job_store

    assert status_response.status_code == 200
    payload = status_response.json()["data"]
    assert payload["status"] == "completed"
    assert payload["result"]["doc_id"] == "doc-job-1"
    assert payload["result"]["chunks_added"] == 2
