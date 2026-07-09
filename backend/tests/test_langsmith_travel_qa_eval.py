import json
import os
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.knowledge.langsmith_eval import (
    DEFAULT_DATASET_NAME,
    DEFAULT_EXPERIMENT_PREFIX,
    answer_faithfulness_score,
    avoid_score,
    create_travel_qa_target,
    dataset_has_examples,
    must_include_score,
    rag_retrieval_coverage_score,
    source_quality_score,
    web_search_score,
    quiet_langsmith_eval_loggers,
)


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


def test_golden_set_examples_include_rag_evaluation_metadata():
    dataset_path = Path(__file__).resolve().parents[1] / "evals" / "travel_qa_golden_set.json"
    examples = json.loads(dataset_path.read_text(encoding="utf-8"))

    assert examples
    for example in examples:
        metadata = example["metadata"]
        assert metadata.get("expected_source_keywords"), metadata["example_id"]
        assert isinstance(metadata.get("requires_realtime_check"), bool), metadata["example_id"]


def test_must_include_score_passes_when_answer_covers_required_rules():
    run = SimpleNamespace(outputs={"answer": "建议通过官方渠道确认预约，优先地铁出行。"})
    example = SimpleNamespace(metadata={"must_include": ["官方渠道复核预约", "地铁或交通管制提醒"]})

    result = must_include_score(run, example)

    assert result["score"] == 1
    assert "covered 2/2" in result["comment"]


def test_must_include_score_reports_missing_rules_from_dict_inputs():
    run = {"outputs": {"answer": "建议提前出发。"}}
    example = {"metadata": {"must_include": ["南京博物院/总统府/夫子庙或钟山"]}}

    result = must_include_score(run, example)

    assert result["score"] == 0
    assert "missing" in result["comment"]
    assert "南京博物院/总统府/夫子庙或钟山" in result["comment"]


def test_must_include_score_returns_partial_credit_for_covered_rules():
    run = {
        "outputs": {
            "answer": "建议清晨6点到西湖西线，优先茅家埠、浴鹄湾等分散点位，交通规则出发前再确认。"
        }
    }
    example = {"metadata": {"must_include": ["早出发", "西湖分散点位", "交通管控"]}}

    result = must_include_score(run, example)

    assert result["score"] == pytest.approx(2 / 3)
    assert "covered 2/3" in result["comment"]
    assert "missing: 交通管控" in result["comment"]


def test_avoid_score_fails_when_answer_contains_forbidden_rules():
    run = SimpleNamespace(outputs={"answer": "现场一定能进馆，不需要预约。"})
    example = SimpleNamespace(metadata={"avoid": ["保证一定能现场进馆", "声称无需预约"]})

    result = avoid_score(run, example)

    assert result["score"] == 0
    assert "violated" in result["comment"]


def test_avoid_score_passes_when_answer_avoids_forbidden_rules():
    run = {"outputs": {"answer": "建议提前查看官方预约规则，不要临时处理。"}}
    example = {"metadata": {"avoid": ["保证一定能现场进馆", "声称无需预约"]}}

    result = avoid_score(run, example)

    assert result["score"] == 1
    assert "no avoid rules violated" in result["comment"]


def test_rag_retrieval_coverage_scores_expected_source_keywords():
    run = {
        "outputs": {
            "sources": [
                {"title": "南京博物院预约公告", "summary": "实名预约和入馆证件说明", "source": "web-official"},
                {"title": "夫子庙交通提示", "summary": "节假日地铁和交通管制", "source": "web-official"},
            ]
        }
    }
    example = {"metadata": {"expected_source_keywords": ["南京博物院", "交通管制", "总统府"]}}

    result = rag_retrieval_coverage_score(run, example)

    assert result["score"] == pytest.approx(2 / 3)
    assert "covered 2/3" in result["comment"]
    assert "总统府" in result["comment"]


def test_source_quality_scores_official_sources_higher_than_community_sources():
    official_run = {"outputs": {"sources": [{"title": "公告", "url": "https://example.gov.cn/notice", "source": "web-search"}]}}
    community_run = {"outputs": {"sources": [{"title": "游记", "url": "https://mafengwo.cn/note/1", "source": "web-community"}]}}

    official = source_quality_score(official_run, {})
    community = source_quality_score(community_run, {})

    assert official["score"] == pytest.approx(1.0)
    assert community["score"] < official["score"]


def test_web_search_score_requires_web_for_realtime_examples():
    run = {"outputs": {"used_web_search": False}}
    example = {
        "inputs": {"question": "帮我确认一下某某古镇明天会不会临时闭园？"},
        "metadata": {"requires_realtime_check": True},
    }

    result = web_search_score(run, example)

    assert result["score"] == 0
    assert "expected web search" in result["comment"]


def test_web_search_score_penalizes_unnecessary_web_search_lightly():
    run = {"outputs": {"used_web_search": True}}
    example = {"inputs": {"question": "桂林阳朔第一次去，漓江和遇龙河怎么选？"}}

    result = web_search_score(run, example)

    assert result["score"] == pytest.approx(0.5)
    assert "unnecessary web search" in result["comment"]


def test_answer_faithfulness_passes_when_answer_is_supported_by_sources():
    run = {
        "outputs": {
            "answer": "南京博物院需要实名预约，出行前以官方公告为准。",
            "sources": [{"title": "南京博物院公告", "summary": "南京博物院实行实名预约，请以官方公告为准。"}],
        }
    }

    result = answer_faithfulness_score(run, {})

    assert result["score"] == 1
    assert "supported" in result["comment"]


def test_answer_faithfulness_fails_on_precise_claims_missing_from_sources():
    run = {
        "outputs": {
            "answer": "南京博物院每天0点放票，门票120元。",
            "sources": [{"title": "南京博物院公告", "summary": "南京博物院实行实名预约，请以官方公告为准。"}],
        }
    }

    result = answer_faithfulness_score(run, {})

    assert result["score"] == 0
    assert "unsupported precise claims" in result["comment"]


def test_quiet_langsmith_eval_loggers_suppresses_third_party_debug_logs():
    quiet_langsmith_eval_loggers()

    assert logging.getLogger("langsmith").level == logging.WARNING
    assert logging.getLogger("langsmith.client").level == logging.WARNING
    assert logging.getLogger("langsmith._internal._background_thread").level == logging.WARNING
    assert logging.getLogger("urllib3.connectionpool").level == logging.WARNING


def test_dataset_has_examples_checks_for_at_least_one_example():
    class FakeClient:
        def __init__(self, examples):
            self.examples = examples
            self.calls = []

        def list_examples(self, **kwargs):
            self.calls.append(kwargs)
            return iter(self.examples)

    populated = FakeClient([object()])
    empty = FakeClient([])

    assert dataset_has_examples(populated, "travel_eval") is True
    assert dataset_has_examples(empty, "travel_eval") is False
    assert populated.calls == [{"dataset_name": "travel_eval", "limit": 1}]


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

    assert outputs == {
        "answer": "answer:nanjing holiday tips",
        "sources": [],
        "retrieved_count": 0,
        "generation_mode": "fallback",
        "used_web_search": False,
    }
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

    assert outputs["answer"] == "answer:shanghai family travel"
    assert outputs["sources"] == []
    assert outputs["retrieved_count"] == 0
    assert outputs["generation_mode"] == "fallback"
    assert outputs["used_web_search"] is False
    assert agent.calls[0]["question"] == "shanghai family travel"
    assert agent.calls[0]["top_k"] == 5


def test_langsmith_target_includes_retrieval_and_source_metadata_in_outputs():
    class RichQAAgent:
        def ask(self, question, top_k=5, conversation_history=None):
            return TravelQAResponse(
                answer="请以官方公告为准。",
                sources=[
                    TravelKnowledgeSource(
                        title="南京博物院参观预约",
                        url="https://example.test/njmuseum",
                        summary="预约说明",
                        source="web-official",
                        score=0.97,
                    )
                ],
                retrieved_count=1,
                generation_mode="llm",
                used_web_search=True,
            )

    outputs = create_travel_qa_target(RichQAAgent())({"question": "南京博物院怎么预约？"})

    assert outputs == {
        "answer": "请以官方公告为准。",
        "sources": [
            {
                "title": "南京博物院参观预约",
                "url": "https://example.test/njmuseum",
                "summary": "预约说明",
                "source": "web-official",
                "published_at": None,
                "score": 0.97,
            }
        ],
        "retrieved_count": 1,
        "generation_mode": "llm",
        "used_web_search": True,
    }


@pytest.mark.langsmith_eval
def test_langsmith_evaluates_travel_qa_system():
    if os.getenv("RUN_LANGSMITH_EVAL", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("Set RUN_LANGSMITH_EVAL=1 to run the LangSmith integration evaluation.")
    if not os.getenv("LANGSMITH_API_KEY"):
        pytest.skip("Set LANGSMITH_API_KEY to run the LangSmith integration evaluation.")

    from langsmith import Client, evaluate

    from app.knowledge.langsmith_eval import create_default_travel_qa_target

    dataset_name = os.getenv("LANGSMITH_TRAVEL_QA_DATASET", DEFAULT_DATASET_NAME)
    experiment_prefix = os.getenv("LANGSMITH_TRAVEL_QA_EXPERIMENT_PREFIX", DEFAULT_EXPERIMENT_PREFIX)

    quiet_langsmith_eval_loggers()
    client = Client()
    client.read_dataset(dataset_name=dataset_name)
    if not dataset_has_examples(client, dataset_name):
        pytest.skip(f"LangSmith dataset {dataset_name!r} has no examples to evaluate.")

    try:
        results = evaluate(
            create_default_travel_qa_target(),
            data=dataset_name,
            evaluators=[
                must_include_score,
                avoid_score,
                rag_retrieval_coverage_score,
                source_quality_score,
                web_search_score,
                answer_faithfulness_score,
            ],
            experiment_prefix=experiment_prefix,
            client=client,
        )
    finally:
        client.flush()
        client.session.close()

    assert results is not None
