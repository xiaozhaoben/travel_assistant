import os
import logging

import pytest

from app.domain.models import TravelQAResponse
from app.knowledge.langsmith_eval import create_travel_qa_target, exact_match, quiet_langsmith_eval_loggers


class FakeQAAgent:
    def __init__(self):
        self.calls = []

    def ask(self, question, top_k=5, conversation_history=None):
        self.calls.append(
            {
                "question": question,
                "top_k": top_k,
                "conversation_history": conversation_history,
            }
        )
        return TravelQAResponse(answer=f"answer:{question}")


def test_exact_match_compares_outputs_to_reference_outputs():
    assert exact_match({"answer": "ok"}, {"answer": "ok"}) is True
    assert exact_match({"answer": "ok"}, {"answer": "no"}) is False


def test_quiet_langsmith_eval_loggers_suppresses_third_party_debug_logs():
    quiet_langsmith_eval_loggers()

    assert logging.getLogger("langsmith").level == logging.WARNING
    assert logging.getLogger("langsmith.client").level == logging.WARNING
    assert logging.getLogger("langsmith._internal._background_thread").level == logging.WARNING
    assert logging.getLogger("urllib3.connectionpool").level == logging.WARNING


def test_langsmith_target_calls_travel_qa_agent_with_question_inputs():
    agent = FakeQAAgent()
    target = create_travel_qa_target(agent)

    outputs = target(
        {
            "question": "nanjing holiday tips",
            "top_k": 3,
            "conversation_history": [{"role": "user", "content": "I want to visit Nanjing"}],
        }
    )

    assert outputs == {"answer": "answer:nanjing holiday tips"}
    assert agent.calls == [
        {
            "question": "nanjing holiday tips",
            "top_k": 3,
            "conversation_history": [{"role": "user", "content": "I want to visit Nanjing"}],
        }
    ]


def test_langsmith_target_accepts_locale_field_from_existing_dataset_examples():
    agent = FakeQAAgent()
    target = create_travel_qa_target(agent)

    outputs = target({"locale": "shanghai family travel"})

    assert outputs == {"answer": "answer:shanghai family travel"}
    assert agent.calls[0]["question"] == "shanghai family travel"
    assert agent.calls[0]["top_k"] == 5


@pytest.mark.langsmith_eval
def test_langsmith_evaluates_travel_qa_system():
    if os.getenv("RUN_LANGSMITH_EVAL", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("Set RUN_LANGSMITH_EVAL=1 to run the LangSmith integration evaluation.")
    if not os.getenv("LANGSMITH_API_KEY"):
        pytest.skip("Set LANGSMITH_API_KEY to run the LangSmith integration evaluation.")

    from langsmith import Client, evaluate

    from app.knowledge.langsmith_eval import create_default_travel_qa_target

    dataset_name = os.getenv("LANGSMITH_TRAVEL_QA_DATASET", "travel_test")
    experiment_prefix = os.getenv("LANGSMITH_TRAVEL_QA_EXPERIMENT_PREFIX", "travel_test experiment")

    quiet_langsmith_eval_loggers()
    client = Client()
    client.read_dataset(dataset_name=dataset_name)

    results = evaluate(
        create_default_travel_qa_target(),
        data=dataset_name,
        evaluators=[exact_match],
        experiment_prefix=experiment_prefix,
        client=client,
    )
    client.flush()
    client.session.close()

    assert results is not None
