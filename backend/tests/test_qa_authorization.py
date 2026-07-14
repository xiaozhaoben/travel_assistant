from __future__ import annotations

from contextlib import contextmanager
from fastapi.testclient import TestClient
import pytest
from threading import Event, Thread
from types import SimpleNamespace

import app.main as main_module
from app.auth.principal import create_principal_token, decode_principal_token
from app.domain.models import TravelQAResponse
from app.main import app, settings
import app.storage.qa_store as qa_store_module
from app.storage.qa_store import InMemoryQAConversationStore, PostgresQAConversationStore, QAConversationNotFound


class FakeQAAgent:
    def __init__(self):
        self.calls = []

    def ask(self, question, top_k=5, conversation_history=None, config=None):
        self.calls.append((question, config))
        return TravelQAResponse(answer=f"answer:{question}")

    def stream(self, question, top_k=5, conversation_history=None, config=None):
        self.calls.append((question, config))
        yield {"event": "answer_delta", "data": {"content": "answer"}}
        yield {"event": "done", "data": TravelQAResponse(answer="answer")}


def _anonymous_token(client: TestClient) -> str:
    response = client.post("/api/auth/anonymous")
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


def _user_token(subject: str = "user-1") -> str:
    return create_principal_token(
        subject=subject,
        principal_type="user",
        username="tester",
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.jwt_expire_minutes,
    )


@pytest.fixture
def isolated_qa(monkeypatch):
    store = InMemoryQAConversationStore()
    agent = FakeQAAgent()
    original_store = main_module.qa_store
    original_agent = main_module.qa_agent
    original_resources = getattr(app.state, "resources", None)
    monkeypatch.setattr(main_module, "get_app_resources", lambda: SimpleNamespace(qa_store=store, qa_agent=agent))
    monkeypatch.setattr(main_module, "qa_store", store)
    monkeypatch.setattr(main_module, "qa_agent", agent)
    if original_resources is not None:
        original_resources.qa_store = store
        original_resources.qa_agent = agent
    yield store, agent
    monkeypatch.setattr(main_module, "qa_store", original_store)
    monkeypatch.setattr(main_module, "qa_agent", original_agent)
    if original_resources is not None:
        original_resources.qa_store = original_store
        original_resources.qa_agent = original_agent


def test_in_memory_qa_store_rejects_owner_mismatch_for_read_and_write():
    store = InMemoryQAConversationStore()
    conversation = store.get_or_create_conversation(
        user_id="user-a",
        anonymous_id=None,
        title="owned",
    )

    with pytest.raises(QAConversationNotFound):
        store.get_conversation(conversation["id"], user_id="user-b", anonymous_id=None)
    with pytest.raises(QAConversationNotFound):
        store.get_recent_messages(conversation["id"], user_id="user-b", anonymous_id=None)
    with pytest.raises(QAConversationNotFound):
        store.append_message(
            conversation["id"], "user", "forged", user_id="user-b", anonymous_id=None
        )


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    [
        ("get_or_create_conversation", (), {}),
        ("get_recent_messages", ("conversation-1",), {}),
        ("append_message", ("conversation-1", "user", "message"), {}),
        ("append_exchange", ("conversation-1", "question", "answer"), {}),
        ("list_conversations", (), {}),
        ("get_conversation", ("conversation-1",), {}),
        ("get_or_create_conversation", (), {"user_id": "user-1", "anonymous_id": "anon-1"}),
        ("get_recent_messages", ("conversation-1",), {"user_id": "user-1", "anonymous_id": "anon-1"}),
        (
            "append_message",
            ("conversation-1", "user", "message"),
            {"user_id": "user-1", "anonymous_id": "anon-1"},
        ),
        (
            "append_exchange",
            ("conversation-1", "question", "answer"),
            {"user_id": "user-1", "anonymous_id": "anon-1"},
        ),
        ("list_conversations", (), {"user_id": "user-1", "anonymous_id": "anon-1"}),
        ("get_conversation", ("conversation-1",), {"user_id": "user-1", "anonymous_id": "anon-1"}),
    ],
)
def test_qa_store_requires_exactly_one_owner(method_name, args, kwargs):
    store = InMemoryQAConversationStore()

    with pytest.raises((TypeError, ValueError)):
        getattr(store, method_name)(*args, **kwargs)


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self._one = None
        self._all = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.statements.append((normalized, tuple(params)))
        if normalized.startswith("SELECT id::text"):
            self._one = {
                "id": "conversation-1",
                "user_id": params[1] if "user_id = %s" in normalized else None,
                "anonymous_id": params[1] if "anonymous_id = %s" in normalized else None,
                "title": "owned",
                "created_at": "2026-07-14T00:00:00Z",
                "updated_at": "2026-07-14T00:00:00Z",
            }
        elif normalized.startswith("INSERT INTO travel_qa_messages"):
            self._one = {
                "id": "message-1",
                "conversation_id": "conversation-1",
                "role": "user",
                "content": "hello",
                "sources_payload": [],
                "retrieved_count": 0,
                "generation_mode": None,
                "used_web_search": False,
                "created_at": "2026-07-14T00:00:00Z",
            }
        return self

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class RecordingConnection:
    def __init__(self, manager):
        self.manager = manager
        self.statements = []

    def cursor(self, **_kwargs):
        return RecordingCursor(self)


class RecordingConnectionManager:
    def __init__(self):
        self.connections = []

    @contextmanager
    def connection(self):
        connection = RecordingConnection(self)
        self.connections.append(connection)
        yield connection

    def close(self):
        return None


@pytest.mark.parametrize(
    ("owner_kwargs", "expected_sql", "unexpected_sql", "owner_id"),
    [
        ({"user_id": "user-1", "anonymous_id": None}, "user_id = %s AND anonymous_id IS NULL", "anonymous_id = %s", "user-1"),
        ({"user_id": None, "anonymous_id": "anon-1"}, "anonymous_id = %s AND user_id IS NULL", "user_id = %s", "anon-1"),
    ],
)
def test_postgres_append_locks_owned_conversation_and_writes_in_same_connection(
    monkeypatch, owner_kwargs, expected_sql, unexpected_sql, owner_id
):
    manager = RecordingConnectionManager()
    store = PostgresQAConversationStore("postgresql://unused", connection_manager=manager)
    store._schema_ready = True
    monkeypatch.setattr(qa_store_module, "Jsonb", lambda value: value)

    store.append_message("conversation-1", "user", "hello", **owner_kwargs)

    assert len(manager.connections) == 1
    statements = manager.connections[0].statements
    owner_sql, owner_params = statements[0]
    assert "FOR UPDATE" in owner_sql
    assert expected_sql in owner_sql
    assert unexpected_sql not in owner_sql
    assert owner_params == ("conversation-1", owner_id)
    assert statements[1][0].startswith("INSERT INTO travel_qa_messages")
    assert statements[2][0].startswith("UPDATE travel_qa_conversations")


@pytest.mark.parametrize(
    ("method_name", "expected_params"),
    [
        ("get_or_create_conversation", ("conversation-1", "user-1")),
        ("get_recent_messages", ("conversation-1", "user-1")),
        ("list_conversations", ("user-1", 50)),
        ("get_conversation", ("conversation-1", "user-1")),
    ],
)
def test_postgres_reads_always_use_mutually_exclusive_owner_predicate(method_name, expected_params):
    manager = RecordingConnectionManager()
    store = PostgresQAConversationStore("postgresql://unused", connection_manager=manager)
    store._schema_ready = True
    owner_kwargs = {"user_id": "user-1", "anonymous_id": None}

    if method_name == "get_or_create_conversation":
        store.get_or_create_conversation("conversation-1", **owner_kwargs)
    elif method_name == "get_recent_messages":
        store.get_recent_messages("conversation-1", **owner_kwargs)
    elif method_name == "list_conversations":
        store.list_conversations(**owner_kwargs)
    else:
        store.get_conversation("conversation-1", **owner_kwargs)

    conversation_sql = next(
        (sql, params)
        for connection in manager.connections
        for sql, params in connection.statements
        if "FROM travel_qa_conversations" in sql
    )
    assert "user_id = %s AND anonymous_id IS NULL" in conversation_sql[0]
    assert "anonymous_id = %s" not in conversation_sql[0]
    assert conversation_sql[1] == expected_params


def test_postgres_merge_counts_messages_only_for_conversations_claimed_atomically():
    class MergeCursor(RecordingCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            self._one = (0, 0)
            return self

    class MergeConnection(RecordingConnection):
        def cursor(self, **_kwargs):
            return MergeCursor(self)

    class MergeManager(RecordingConnectionManager):
        @contextmanager
        def connection(self):
            connection = MergeConnection(self)
            self.connections.append(connection)
            yield connection

    manager = MergeManager()
    store = PostgresQAConversationStore("postgresql://unused", connection_manager=manager)
    store._schema_ready = True

    assert store.merge_anonymous("anon-1", "user-1") == (0, 0)
    assert len(manager.connections[0].statements) == 1
    sql, params = manager.connections[0].statements[0]
    assert "WITH claimed AS" in sql
    assert "UPDATE travel_qa_conversations" in sql
    assert "JOIN claimed" in sql
    assert params == ("user-1", "anon-1")


def test_postgres_append_exchange_rolls_back_when_assistant_insert_fails(monkeypatch):
    class FailingExchangeCursor(RecordingCursor):
        def __init__(self, connection):
            super().__init__(connection)
            self.insert_count = 0

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("INSERT INTO travel_qa_messages"):
                self.insert_count += 1
                if self.insert_count == 2:
                    raise RuntimeError("assistant insert failed")
            return super().execute(sql, params)

    class FailingExchangeConnection(RecordingConnection):
        def __init__(self, manager):
            super().__init__(manager)
            self.rolled_back = False
            self.committed = False

        def cursor(self, **_kwargs):
            return FailingExchangeCursor(self)

    class TransactionManager(RecordingConnectionManager):
        @contextmanager
        def connection(self):
            connection = FailingExchangeConnection(self)
            self.connections.append(connection)
            try:
                yield connection
            except Exception:
                connection.rolled_back = True
                raise
            else:
                connection.committed = True

    manager = TransactionManager()
    store = PostgresQAConversationStore("postgresql://unused", connection_manager=manager)
    store._schema_ready = True
    monkeypatch.setattr(qa_store_module, "Jsonb", lambda value: value)

    with pytest.raises(RuntimeError, match="assistant insert failed"):
        store.append_exchange(
            "conversation-1",
            "question",
            "answer",
            user_id="user-1",
            anonymous_id=None,
        )

    connection = manager.connections[0]
    assert connection.rolled_back is True
    assert connection.committed is False
    assert len(manager.connections) == 1
    assert "FOR UPDATE" in connection.statements[0][0]


def test_in_memory_append_exchange_does_not_leave_user_message_when_assistant_payload_fails():
    class BrokenSource:
        def model_dump(self, mode):
            raise RuntimeError("assistant payload failed")

    store = InMemoryQAConversationStore()
    conversation = store.get_or_create_conversation(
        user_id="user-1", anonymous_id=None, title="atomic"
    )

    with pytest.raises(RuntimeError, match="assistant payload failed"):
        store.append_exchange(
            conversation["id"],
            "question",
            "answer",
            sources=[BrokenSource()],
            user_id="user-1",
            anonymous_id=None,
        )

    assert store.get_recent_messages(
        conversation["id"], user_id="user-1", anonymous_id=None
    ) == []


def test_in_memory_merge_waits_for_atomic_exchange_and_counts_both_messages():
    payload_started = Event()
    release_payload = Event()
    merge_started = Event()
    merge_finished = Event()
    merge_result = []

    class BlockingSource:
        def model_dump(self, mode):
            payload_started.set()
            assert release_payload.wait(timeout=2)
            return {"title": "source", "url": "https://example.com"}

    store = InMemoryQAConversationStore()
    conversation = store.get_or_create_conversation(
        user_id=None, anonymous_id="anon-1", title="race"
    )

    exchange_thread = Thread(
        target=store.append_exchange,
        args=(conversation["id"], "question", "answer"),
        kwargs={
            "sources": [BlockingSource()],
            "user_id": None,
            "anonymous_id": "anon-1",
        },
    )

    def merge():
        merge_started.set()
        merge_result.append(store.merge_anonymous("anon-1", "user-1"))
        merge_finished.set()

    merge_thread = Thread(target=merge)
    exchange_thread.start()
    assert payload_started.wait(timeout=2)
    merge_thread.start()
    assert merge_started.wait(timeout=2)
    try:
        assert merge_finished.wait(timeout=0.1) is False
    finally:
        release_payload.set()
        exchange_thread.join(timeout=2)
        merge_thread.join(timeout=2)

    assert merge_result == [(1, 2)]


def test_qa_conversations_are_isolated_and_client_identity_is_ignored(isolated_qa):
    _store, agent = isolated_qa
    with TestClient(app) as client:
        token_a = _anonymous_token(client)
        token_b = _anonymous_token(client)
        created = client.post(
            "/api/qa/ask",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"question": "question", "user_id": "forged-user", "anonymous_id": "forged-anon"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["data"]["conversation_id"]

        listed = client.get(
            "/api/qa/conversations?user_id=forged-user&anonymous_id=forged-anon",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert listed.status_code == 200
        assert listed.json()["data"] == []

        detail = client.get(
            f"/api/qa/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert detail.status_code == 404
        assert detail.json()["code"] == "QA_CONVERSATION_NOT_FOUND"
        assert len(agent.calls) == 1


def test_stream_rejects_conversation_idor_before_generation(isolated_qa):
    _store, agent = isolated_qa
    with TestClient(app) as client:
        token_a = _anonymous_token(client)
        token_b = _anonymous_token(client)
        created = client.post(
            "/api/qa/ask",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"question": "question"},
        )
        conversation_id = created.json()["data"]["conversation_id"]
        before = len(agent.calls)
        response = client.post(
            "/api/qa/ask/stream",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"question": "forged", "conversation_id": conversation_id},
        )
        assert response.status_code == 404
        assert response.json()["code"] == "QA_CONVERSATION_NOT_FOUND"
        assert len(agent.calls) == before


def test_merge_requires_anonymous_header_and_is_idempotent(isolated_qa):
    store, _agent = isolated_qa
    with TestClient(app) as client:
        anonymous_token = _anonymous_token(client)
        other_anonymous_token = _anonymous_token(client)
        anonymous_subject = decode_principal_token(
            anonymous_token, settings.jwt_secret_key, settings.jwt_algorithm
        ).subject
        other_anonymous_subject = decode_principal_token(
            other_anonymous_token, settings.jwt_secret_key, settings.jwt_algorithm
        ).subject
        created = client.post(
            "/api/qa/ask",
            headers={"Authorization": f"Bearer {anonymous_token}"},
            json={"question": "question"},
        )
        assert created.status_code == 200
        other_created = client.post(
            "/api/qa/ask",
            headers={"Authorization": f"Bearer {other_anonymous_token}"},
            json={"question": "other question"},
        )
        assert other_created.status_code == 200
        user_token = _user_token()
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Anonymous-Token": anonymous_token,
        }
        merged = client.post("/api/auth/merge-anonymous", headers=headers)
        assert merged.status_code == 200
        assert merged.json()["data"] == {
            "merged_conversations": 1,
            "merged_messages": 2,
            "anonymous_id": anonymous_subject,
        }
        assert len(store.list_conversations(user_id="user-1", anonymous_id=None)) == 1
        assert len(
            store.list_conversations(user_id=None, anonymous_id=other_anonymous_subject)
        ) == 1
        other_detail = store.get_conversation(
            other_created.json()["data"]["conversation_id"],
            user_id=None,
            anonymous_id=other_anonymous_subject,
        )
        assert [message.content for message in other_detail.messages] == [
            "other question",
            "answer:other question",
        ]
        repeated = client.post("/api/auth/merge-anonymous", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["data"]["merged_conversations"] == 0
        assert repeated.json()["data"]["merged_messages"] == 0

        missing_header = client.post(
            "/api/auth/merge-anonymous",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert missing_header.status_code == 401
        assert missing_header.json()["code"] == "AUTH_ANONYMOUS_TOKEN_REQUIRED"


def test_merge_rejects_anonymous_or_non_anonymous_header(isolated_qa):
    _store, _agent = isolated_qa
    with TestClient(app) as client:
        anonymous_token = _anonymous_token(client)
        response = client.post(
            "/api/auth/merge-anonymous",
            headers={
                "Authorization": f"Bearer {anonymous_token}",
                "X-Anonymous-Token": anonymous_token,
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_USER_REQUIRED"

        user_token = _user_token("user-2")
        response = client.post(
            "/api/auth/merge-anonymous",
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Anonymous-Token": user_token,
            },
        )
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_ANONYMOUS_TOKEN_INVALID"
