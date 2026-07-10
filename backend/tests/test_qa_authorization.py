from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from types import SimpleNamespace

import app.main as main_module
from app.auth.principal import create_principal_token
from app.domain.models import TravelQAResponse
from app.main import app, settings
from app.storage.qa_store import InMemoryQAConversationStore, QAConversationNotFound


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
        conversation_id="conversation-1",
        user_id="user-a",
        title="owned",
    )

    with pytest.raises(QAConversationNotFound):
        store.get_conversation(conversation["id"], user_id="user-b")
    with pytest.raises(QAConversationNotFound):
        store.get_recent_messages(conversation["id"], user_id="user-b")
    with pytest.raises(QAConversationNotFound):
        store.append_message(conversation["id"], "user", "forged", user_id="user-b")


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
    _store, _agent = isolated_qa
    with TestClient(app) as client:
        anonymous_token = _anonymous_token(client)
        created = client.post(
            "/api/qa/ask",
            headers={"Authorization": f"Bearer {anonymous_token}"},
            json={"question": "question"},
        )
        assert created.status_code == 200
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
            "anonymous_id": merged.json()["data"]["anonymous_id"],
        }
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
