from __future__ import annotations

import logging
import re
from typing import Any, Callable

from app.domain.models import TravelQAResponse


DEFAULT_DATASET_NAME = "travel_test"
DEFAULT_EXPERIMENT_PREFIX = "travel_test experiment"


RULE_SEPARATORS = ("或", "/", "|", "、", ",", "，")
RULE_STOPWORDS = (
    "建议",
    "提醒",
    "声称",
    "编造",
    "给出",
    "忽略",
    "保证",
    "具体",
    "未经核实",
    "相关",
)
RULE_SYNONYMS = {
    "复核": ("复核", "确认", "核对", "查看", "查询"),
    "确认": ("确认", "复核", "核对", "查看", "查询"),
    "无需": ("无需", "不需要", "不用", "免预约", "无需预约"),
    "不需要": ("不需要", "无需", "不用", "免预约", "无需预约"),
    "早出发": ("早出发", "清晨", "早上", "赶早", "早到", "上午"),
    "西湖分散点位": ("西湖分散点位", "西湖西线", "茅家埠", "浴鹄湾", "曲院风荷", "杨公堤", "分散点位"),
}
WEB_SEARCH_TRIGGERS = (
    "联网",
    "实时",
    "最新",
    "明天",
    "今天",
    "天气",
    "预报",
    "开放时间",
    "营业时间",
    "闭园",
    "闭馆",
    "预约",
    "票务",
    "余票",
    "公告",
    "限流",
)
OFFICIAL_SOURCE_HINTS = (".gov.cn", "mct.gov.cn", "zwfw", "museum", "官方", "文旅", "景区", "博物馆", "official")
MAP_SOURCE_HINTS = ("amap.com", "google.com/maps", "map", "地图")
GUIDE_SOURCE_HINTS = ("wikivoyage.org", "wikipedia.org", "opentripmap", "guide", "指南")
COMMUNITY_SOURCE_HINTS = ("mafengwo", "ctrip", "trip.com", "xiaohongshu", "dianping", "qunar", "游记", "社区")
PRECISE_CLAIM_PATTERN = re.compile(r"\d+(?:[:：.]\d+)?\s*(?:点|时|元|天|日|月|年|小时|分钟|%|公里|千米|米|人|次|号|周|层)")


def must_include_score(run: Any, example: Any) -> dict[str, Any]:
    answer = answer_from_run(run)
    rules = metadata_rules(example, "must_include")
    missing = [rule for rule in rules if not rule_matches_answer(rule, answer)]
    score = 1.0 if not rules else (len(rules) - len(missing)) / len(rules)
    covered = len(rules) - len(missing)
    if missing:
        return {
            "score": score,
            "comment": f"covered {covered}/{len(rules)} must_include rules; missing: {', '.join(missing)}",
        }
    return {"score": score, "comment": f"covered {covered}/{len(rules)} must_include rules"}


def avoid_score(run: Any, example: Any) -> dict[str, Any]:
    answer = answer_from_run(run)
    rules = metadata_rules(example, "avoid")
    violated = [rule for rule in rules if rule_matches_answer(rule, answer)]
    score = 0 if violated else 1
    if violated:
        return {
            "score": score,
            "comment": f"violated {len(violated)}/{len(rules)} avoid rules: {', '.join(violated)}",
        }
    return {"score": score, "comment": "no avoid rules violated"}


def rag_retrieval_coverage_score(run: Any, example: Any) -> dict[str, Any]:
    outputs = outputs_from_run(run)
    sources = sources_from_outputs(outputs)
    expected_keywords = metadata_rules(example, "expected_source_keywords") or metadata_rules(example, "expected_source_terms")
    if not expected_keywords:
        retrieved_count = int(outputs.get("retrieved_count") or len(sources) or 0)
        score = 1.0 if retrieved_count > 0 else 0.0
        return {
            "score": score,
            "comment": f"no expected source keywords configured; retrieved_count={retrieved_count}",
        }

    corpus = normalize_rule_text(" ".join(source_text(source) for source in sources))
    missing = [keyword for keyword in expected_keywords if not rule_matches_answer(keyword, corpus)]
    covered = len(expected_keywords) - len(missing)
    score = covered / len(expected_keywords)
    if missing:
        return {
            "score": score,
            "comment": f"covered {covered}/{len(expected_keywords)} expected source keywords; missing: {', '.join(missing)}",
        }
    return {"score": score, "comment": f"covered {covered}/{len(expected_keywords)} expected source keywords"}


def source_quality_score(run: Any, example: Any) -> dict[str, Any]:
    sources = sources_from_outputs(outputs_from_run(run))
    if not sources:
        return {"score": 0.0, "comment": "no sources returned"}
    scored = [(source_label(source), classify_source_quality(source)) for source in sources]
    score = sum(item[1] for item in scored) / len(scored)
    details = ", ".join(f"{label}:{value:.2f}" for label, value in scored[:5])
    return {"score": score, "comment": f"average source quality {score:.2f}; {details}"}


def web_search_score(run: Any, example: Any) -> dict[str, Any]:
    outputs = outputs_from_run(run)
    used_web_search = bool(outputs.get("used_web_search"))
    expected = requires_web_search(example)
    if expected and used_web_search:
        return {"score": 1.0, "comment": "expected web search and web search was used"}
    if expected and not used_web_search:
        return {"score": 0.0, "comment": "expected web search but web search was not used"}
    if used_web_search:
        return {"score": 0.5, "comment": "unnecessary web search used for a non-realtime example"}
    return {"score": 1.0, "comment": "web search usage matched expectation"}


def answer_faithfulness_score(run: Any, example: Any) -> dict[str, Any]:
    outputs = outputs_from_run(run)
    answer = str(outputs.get("answer") or "")
    sources = sources_from_outputs(outputs)
    if not answer.strip():
        return {"score": 0.0, "comment": "empty answer"}
    if not sources:
        return {"score": 0.0, "comment": "no sources available to support the answer"}

    source_corpus = " ".join(source_text(source) for source in sources)
    unsupported_claims = unsupported_precise_claims(answer, source_corpus)
    if unsupported_claims:
        return {
            "score": 0.0,
            "comment": f"unsupported precise claims: {', '.join(unsupported_claims[:5])}",
        }

    overlap = evidence_overlap(answer, source_corpus)
    score = min(1.0, overlap / 8)
    if score >= 0.75:
        return {"score": 1.0, "comment": f"answer appears supported by sources; evidence_overlap={overlap}"}
    return {"score": score, "comment": f"weak source support; evidence_overlap={overlap}"}


def answer_from_run(run: Any) -> str:
    outputs = outputs_from_run(run)
    if isinstance(outputs, dict):
        return str(outputs.get("answer") or outputs.get("response") or outputs.get("output") or "")
    return str(getattr(outputs, "answer", outputs) or "")


def outputs_from_run(run: Any) -> dict[str, Any]:
    outputs = value_get(run, "outputs", {}) or {}
    return outputs if isinstance(outputs, dict) else {}


def inputs_from_example(example: Any) -> dict[str, Any]:
    inputs = value_get(example, "inputs", {}) or {}
    return inputs if isinstance(inputs, dict) else {}


def metadata_rules(example: Any, key: str) -> list[str]:
    metadata = value_get(example, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(key) or []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def value_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def sources_from_outputs(outputs: dict[str, Any]) -> list[Any]:
    sources = outputs.get("sources") or []
    return sources if isinstance(sources, list) else []


def source_text(source: Any) -> str:
    return " ".join(
        str(value_get(source, key, "") or "")
        for key in ("title", "summary", "source", "url", "source_url", "content")
    )


def source_label(source: Any) -> str:
    return str(value_get(source, "title", "") or value_get(source, "source", "") or value_get(source, "url", "") or "source")


def classify_source_quality(source: Any) -> float:
    haystack = source_text(source).lower()
    source_name = str(value_get(source, "source", "") or "").lower()
    if "official" in source_name or any(hint.lower() in haystack for hint in OFFICIAL_SOURCE_HINTS):
        return 1.0
    if any(hint.lower() in haystack for hint in MAP_SOURCE_HINTS):
        return 0.85
    if any(hint.lower() in haystack for hint in GUIDE_SOURCE_HINTS):
        return 0.75
    if any(hint.lower() in haystack for hint in COMMUNITY_SOURCE_HINTS):
        return 0.35
    if source_name == "rss":
        return 0.55
    try:
        return max(0.0, min(0.7, float(value_get(source, "score", 0.5) or 0.5)))
    except (TypeError, ValueError):
        return 0.5


def requires_web_search(example: Any) -> bool:
    metadata = value_get(example, "metadata", {}) or {}
    if isinstance(metadata, dict) and "requires_realtime_check" in metadata:
        return bool(metadata["requires_realtime_check"])
    question = str(inputs_from_example(example).get("question") or inputs_from_example(example).get("query") or "")
    return any(trigger in question for trigger in WEB_SEARCH_TRIGGERS)


def unsupported_precise_claims(answer: str, source_corpus: str) -> list[str]:
    normalized_sources = normalize_rule_text(source_corpus)
    claims: list[str] = []
    for claim in PRECISE_CLAIM_PATTERN.findall(answer):
        normalized_claim = normalize_rule_text(claim)
        if normalized_claim and normalized_claim not in normalized_sources and claim not in claims:
            claims.append(claim)
    return claims


def evidence_overlap(answer: str, source_corpus: str) -> int:
    answer_terms = evidence_terms(answer)
    source_terms = evidence_terms(source_corpus)
    return len(answer_terms & source_terms)


def evidence_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", text):
        normalized = normalize_rule_text(match)
        if len(normalized) < 2:
            continue
        if len(normalized) <= 6:
            terms.add(normalized)
            continue
        for index in range(0, len(normalized) - 1):
            for size in range(2, min(6, len(normalized) - index) + 1):
                terms.add(normalized[index : index + size])
    return terms


def rule_matches_answer(rule: str, answer: str) -> bool:
    normalized_answer = normalize_rule_text(answer)
    normalized_rule = normalize_rule_text(rule)
    if not normalized_rule:
        return True
    if normalized_rule in normalized_answer:
        return True
    if any(segment_matches_answer(segment, normalized_answer) for segment in split_rule_segments(rule)):
        return True
    return any(synonym in normalized_answer for synonym in equivalent_phrases(normalized_rule))


def split_rule_segments(rule: str) -> list[str]:
    segments = [rule]
    for separator in RULE_SEPARATORS:
        next_segments: list[str] = []
        for segment in segments:
            next_segments.extend(segment.split(separator))
        segments = next_segments
    return [segment for segment in (normalize_rule_text(segment) for segment in segments) if segment]


def segment_matches_answer(segment: str, normalized_answer: str) -> bool:
    if segment in normalized_answer:
        return True
    return any(synonym in normalized_answer for synonym in equivalent_phrases(segment))


def equivalent_phrases(text: str) -> list[str]:
    phrases = {text}
    for source, replacements in RULE_SYNONYMS.items():
        if source not in text:
            continue
        for replacement in replacements:
            phrases.add(text.replace(source, replacement))
    return [phrase for phrase in phrases if phrase]


def normalize_rule_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    for word in RULE_STOPWORDS:
        normalized = normalized.replace(word, "")
    for char in (" ", "\n", "\t", "。", "；", ";", "：", ":", "！", "!", "？", "?", "\"", "'", "“", "”"):
        normalized = normalized.replace(char, "")
    return normalized


def quiet_langsmith_eval_loggers(level: int = logging.WARNING) -> None:
    for logger_name in (
        "langsmith",
        "langsmith.client",
        "langsmith._internal._background_thread",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logging.getLogger(logger_name).setLevel(level)


def dataset_has_examples(client: Any, dataset_name: str) -> bool:
    return next(client.list_examples(dataset_name=dataset_name, limit=1), None) is not None


def create_travel_qa_target(qa_agent: Any, default_top_k: int = 5) -> Callable[[dict], dict]:
    def target(inputs: dict) -> dict:
        question = question_from_inputs(inputs)
        top_k = top_k_from_inputs(inputs, default_top_k)
        conversation_history = conversation_history_from_inputs(inputs)
        response = qa_agent.ask(
            question,
            top_k=top_k,
            conversation_history=conversation_history,
        )
        return travel_qa_response_to_outputs(response)

    return target


def create_default_travel_qa_target() -> Callable[[dict], dict]:
    from app.main import get_app_resources

    resources = get_app_resources()
    return create_travel_qa_target(resources.qa_agent)


def question_from_inputs(inputs: dict) -> str:
    for key in ("question", "locale", "query", "input"):
        value = inputs.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    raise ValueError("LangSmith example inputs must include question, locale, query, or input.")


def top_k_from_inputs(inputs: dict, default_top_k: int) -> int:
    try:
        return int(inputs.get("top_k") or default_top_k)
    except (TypeError, ValueError):
        return default_top_k


def conversation_history_from_inputs(inputs: dict) -> list[dict[str, str]]:
    value = inputs.get("conversation_history") or inputs.get("history") or []
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            history.append({"role": role, "content": content})
    return history


def travel_qa_response_to_outputs(response: Any) -> dict:
    data = getattr(response, "data", None)
    if data is not None:
        return travel_qa_response_to_outputs(data)
    if isinstance(response, TravelQAResponse):
        return {
            "answer": response.answer,
            "sources": [source.model_dump(mode="json") for source in response.sources],
            "retrieved_count": response.retrieved_count,
            "generation_mode": response.generation_mode,
            "used_web_search": response.used_web_search,
        }
    if isinstance(response, dict):
        payload = response.get("data") if isinstance(response.get("data"), dict) else response
        return {
            "answer": str(payload.get("answer") or ""),
            "sources": payload.get("sources") or [],
            "retrieved_count": int(payload.get("retrieved_count") or 0),
            "generation_mode": str(payload.get("generation_mode") or "fallback"),
            "used_web_search": bool(payload.get("used_web_search")),
        }
    return {
        "answer": str(getattr(response, "answer", response) or ""),
        "sources": list(getattr(response, "sources", []) or []),
        "retrieved_count": int(getattr(response, "retrieved_count", 0) or 0),
        "generation_mode": str(getattr(response, "generation_mode", "fallback") or "fallback"),
        "used_web_search": bool(getattr(response, "used_web_search", False)),
    }
