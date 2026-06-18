from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.knowledge.prompts import render_travel_query_expansion_prompt
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore, lexical_query_terms
from app.researching.research import WebSearchMCPClient

logger = logging.getLogger(__name__)


class TravelQAState(TypedDict, total=False):
    question: str
    top_k: int
    conversation_history: list[dict[str, str]]
    query_variants: list[str]
    vector_docs: list[KnowledgeDocument]
    keyword_docs: list[KnowledgeDocument]
    docs: list[KnowledgeDocument]
    web_sources: list[TravelKnowledgeSource]
    used_web_search: bool
    context: str
    answer: str
    generation_mode: Literal["llm", "fallback"]
    response: TravelQAResponse


AnswerWithLLM = Callable[..., str]
AnswerWithLLMStream = Callable[..., Iterable[str]]


class TravelQAGraphRunner:
    name = "TravelQAGraphRunner"

    def __init__(
        self,
        vector_store: PostgresTravelVectorStore | None,
        llm: Any | None = None,
        web_client: WebSearchMCPClient | None = None,
        answer_with_llm: AnswerWithLLM | None = None,
        answer_with_llm_stream: AnswerWithLLMStream | None = None,
        checkpointer: Any | None = None,
    ):
        self.vector_store = vector_store
        self.llm = llm
        self.web_client = web_client or WebSearchMCPClient()
        self.answer_with_llm = answer_with_llm
        self.answer_with_llm_stream = answer_with_llm_stream
        self.checkpointer = checkpointer
        self.graph = self._build_graph()

    def ask(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> TravelQAResponse:
        final_state = self.graph.invoke(
            {
                "question": question,
                "top_k": top_k,
                "conversation_history": conversation_history or [],
            },
            config,
        )
        return final_state["response"]

    def stream(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
        config: dict[str, Any] | None = None,
    ):
        state = self._prepare_answer_state(question, top_k, conversation_history or [])
        docs = state.get("docs", [])
        answer_parts: list[str] = []
        web_sources: list[TravelKnowledgeSource] = []
        used_web_search = False

        if self.llm is not None and self.answer_with_llm_stream is not None:
            for chunk in call_answer_stream(self.answer_with_llm_stream, question, state.get("context", ""), config):
                metadata = qa_stream_metadata(chunk)
                if metadata is not None:
                    web_sources = merge_sources(web_sources, metadata["sources"])
                    used_web_search = used_web_search or metadata["used_web_search"]
                    continue
                if not chunk:
                    continue
                content = str(chunk)
                answer_parts.append(content)
                yield {"event": "answer_delta", "data": {"content": content}}
            answer = "".join(answer_parts).strip()
            if answer:
                state["web_sources"] = web_sources
                state["used_web_search"] = used_web_search
                yield {"event": "done", "data": self._response_from_state(state, answer, "llm")}
                return

        from app.knowledge.qa_agent import fallback_answer

        fallback = fallback_answer(question, docs)
        for chunk in chunk_text(fallback):
            yield {"event": "answer_delta", "data": {"content": chunk}}
        yield {"event": "done", "data": self._response_from_state(state, fallback, "fallback")}

    def _prepare_answer_state(
        self,
        question: str,
        top_k: int,
        conversation_history: list[dict[str, str]],
    ) -> TravelQAState:
        state: TravelQAState = {
            "question": question,
            "top_k": top_k,
            "conversation_history": conversation_history,
        }
        state.update(self._expand_query(state))
        state.update(self._retrieve_vector(state))
        state.update(self._merge_and_rank(state))
        return state

    def _response_from_state(
        self,
        state: TravelQAState,
        answer: str,
        generation_mode: Literal["llm", "fallback"],
    ) -> TravelQAResponse:
        from app.knowledge.qa_agent import source_from_document

        docs = state.get("docs", [])
        doc_sources = [source_from_document(doc) for doc in docs]
        web_sources = state.get("web_sources", [])
        sources = merge_sources(web_sources, doc_sources) if state.get("used_web_search") else merge_sources(doc_sources, web_sources)
        return TravelQAResponse(
            answer=answer,
            sources=sources,
            retrieved_count=len(sources),
            generation_mode=generation_mode,
            used_web_search=bool(state.get("used_web_search")),
        )

    def _build_graph(self):
        graph = StateGraph(TravelQAState)
        graph.add_node("expand_query", self._expand_query)
        graph.add_node("retrieve_vector", self._retrieve_vector)
        graph.add_node("merge_and_rank", self._merge_and_rank)
        graph.add_node("answer_question", self._answer_question)
        graph.add_node("build_response", self._build_response)
        graph.add_edge(START, "expand_query")
        graph.add_edge("expand_query", "retrieve_vector")
        graph.add_edge("retrieve_vector", "merge_and_rank")
        graph.add_edge("merge_and_rank", "answer_question")
        graph.add_edge("answer_question", "build_response")
        graph.add_edge("build_response", END)
        return graph.compile()

    def _expand_query(self, state: TravelQAState) -> dict[str, Any]:
        question = state["question"]
        variants = expand_question_variants(
            question,
            state.get("conversation_history", []),
            llm=self.llm,
            max_variants=max(3, int(state.get("top_k", 5))),
        )
        return {"query_variants": variants}

    def _retrieve_vector(self, state: TravelQAState) -> dict[str, Any]:
        if self.vector_store is None:
            return {"vector_docs": [], "keyword_docs": []}
        queries = state.get("query_variants") or [state["question"]]
        fetch_k = max(int(state.get("top_k", 5)) + 5, 8)
        vector_docs: list[KnowledgeDocument] = []
        keyword_docs: list[KnowledgeDocument] = []
        try:
            for query in queries:
                vector_docs.extend(self.vector_store.similarity_search(query, k=fetch_k))
                keyword_search = getattr(self.vector_store, "keyword_search", None)
                if callable(keyword_search):
                    keyword_docs.extend(keyword_search(query, k=fetch_k))
        except Exception as exc:
            logger.warning("Travel QA retrieval failed: %s", exc)
            return {"vector_docs": dedupe_knowledge_documents(vector_docs), "keyword_docs": dedupe_knowledge_documents(keyword_docs)}
        return {"vector_docs": dedupe_knowledge_documents(vector_docs), "keyword_docs": dedupe_knowledge_documents(keyword_docs)}

    def _merge_and_rank(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import format_documents

        top_k = int(state.get("top_k", 5))
        docs = rerank_retrieved_documents(
            state["question"],
            state.get("query_variants") or [state["question"]],
            state.get("vector_docs", []),
            state.get("keyword_docs", []),
            limit=max(1, top_k + 3),
        )
        context_parts = []
        history_context = format_conversation_history(state.get("conversation_history", []))
        if history_context:
            context_parts.append(history_context)
        document_context = format_documents(docs)
        if document_context:
            context_parts.append(document_context)
        return {"docs": docs, "context": "\n\n".join(context_parts)}

    def _answer_question(self, state: TravelQAState, config: RunnableConfig) -> dict[str, Any]:
        from app.knowledge.qa_agent import fallback_answer

        answer = ""
        context = state.get("context", "")
        if self.llm is not None and self.answer_with_llm is not None:
            result = normalize_answer_result(call_answer(self.answer_with_llm, state["question"], context, config))
            answer = result["answer"]
            if answer:
                return {
                    "answer": answer,
                    "generation_mode": "llm",
                    "web_sources": result["sources"],
                    "used_web_search": result["used_web_search"],
                }
        return {
            "answer": fallback_answer(state["question"], state.get("docs", [])),
            "generation_mode": "fallback",
        }

    def _build_response(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import source_from_document

        docs = state.get("docs", [])
        doc_sources = [source_from_document(doc) for doc in docs]
        web_sources = state.get("web_sources", [])
        sources = merge_sources(web_sources, doc_sources) if state.get("used_web_search") else merge_sources(doc_sources, web_sources)
        return {
            "response": TravelQAResponse(
                answer=state["answer"],
                sources=sources,
                retrieved_count=len(sources),
                generation_mode=state.get("generation_mode", "fallback"),
                used_web_search=bool(state.get("used_web_search")),
            )
        }


def expand_question_variants(
    question: str,
    conversation_history: list[dict[str, str]],
    *,
    llm: Any | None = None,
    max_variants: int = 5,
) -> list[str]:
    variants = [question.strip()]
    history_focus = latest_user_focus(conversation_history)
    if history_focus and (is_ambiguous_question(question) or len(question.strip()) <= 20):
        variants.append(f"{history_focus} {question}".strip())
    if should_use_llm_query_expansion(question) and llm is not None:
        prompt = render_travel_query_expansion_prompt(question, conversation_history)
        try:
            response = llm.invoke(prompt)
            variants.extend(parse_query_expansion(str(getattr(response, "content", response))))
        except Exception as exc:
            logger.warning("Travel QA query expansion failed, using heuristic variants: %s", exc)
    variants.extend(heuristic_hypothetical_questions(question, history_focus))
    return unique_nonempty_texts(variants, max_items=max_variants)


def parse_query_expansion(text: str) -> list[str]:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", text, flags=re.S)
        if not match:
            return []
        value = json.loads(match.group(0))
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, dict):
        return []
    items: list[str] = []
    for key in ("queries", "hypothetical_questions", "questions"):
        values = value.get(key)
        if isinstance(values, list):
            items.extend(str(item) for item in values)
    return items


def heuristic_hypothetical_questions(question: str, history_focus: str = "") -> list[str]:
    base = f"{history_focus} {question}".strip() if history_focus and is_ambiguous_question(question) else question
    variants = []
    if any(word in question for word in ("预约", "门票", "开放", "闭馆")):
        variants.append(f"{base} 官方公告 预约 门票 开放时间")
    if any(word in question for word in ("交通", "怎么去", "路线", "地铁", "高铁")):
        variants.append(f"{base} 交通 地铁 公交 高铁 景区直达")
    if any(word in question for word in ("亲子", "孩子", "儿童", "家庭")):
        variants.append(f"{base} 亲子游 儿童 家庭 景点 推荐")
    if any(word in question for word in ("政策", "公告", "限制", "限流")):
        variants.append(f"{base} 文旅政策 公告 限流 通知")
    return variants


def latest_user_focus(conversation_history: list[dict[str, str]]) -> str:
    for item in reversed(conversation_history):
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            return content[:120]
    return ""


def is_ambiguous_question(question: str) -> bool:
    text = question.strip()
    if len(text) <= 12:
        return True
    return any(marker in text for marker in ("那", "这个", "那里", "它", "他们", "怎么预约", "怎么去", "开放吗"))


def should_use_llm_query_expansion(question: str) -> bool:
    return is_ambiguous_question(question) or is_complex_question(question)


def is_complex_question(question: str) -> bool:
    text = question.strip()
    if len(text) >= 28:
        return True
    return sum(1 for marker in ("和", "以及", "同时", "并且", "预算", "交通", "住宿", "政策", "门票") if marker in text) >= 2


def unique_nonempty_texts(values: list[str], max_items: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def rerank_retrieved_documents(
    question: str,
    query_variants: list[str],
    *groups: list[KnowledgeDocument],
    limit: int,
) -> list[KnowledgeDocument]:
    docs = dedupe_knowledge_documents([doc for group in groups for doc in group])
    if not docs:
        return []
    terms = lexical_query_terms(" ".join([question, *query_variants]), limit=32)
    complex_query = is_complex_question(question)
    scored = [score_retrieved_document(doc, terms, complex_query) for doc in docs]
    return sorted(scored, key=lambda doc: float(doc.score or 0.0), reverse=True)[:limit]


def score_retrieved_document(doc: KnowledgeDocument, terms: list[str], complex_query: bool) -> KnowledgeDocument:
    haystack = f"{doc.title} {doc.summary} {doc.content}".lower()
    matched = [term for term in terms if term.lower() in haystack]
    coverage = len(set(matched)) / max(len(set(terms)), 1)
    title_hits = sum(1 for term in set(matched) if term.lower() in (doc.title or "").lower())
    source_bonus = {"rss": 0.08, "web-official": 0.16, "web-map": 0.12, "web-guide": 0.1}.get(doc.source_name, 0.06)
    base = min(float(doc.score or 0.0), 1.0)
    coverage_weight = 2.2 if complex_query else 1.15
    score = base * 0.45 + coverage * coverage_weight + min(title_hits * 0.12, 0.36) + source_bonus
    return replace(doc, score=round(score, 6))


def dedupe_knowledge_documents(docs: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    by_key: dict[str, KnowledgeDocument] = {}
    for doc in docs:
        key = (doc.source_url or doc.title or doc.id).strip().lower()
        if not key:
            continue
        current = by_key.get(key)
        if current is None or float(doc.score or 0.0) > float(current.score or 0.0):
            by_key[key] = doc
    return list(by_key.values())


def format_conversation_history(messages: list[dict[str, str]], limit: int = 8) -> str:
    if not messages:
        return ""
    labels = {"user": "用户", "assistant": "助手"}
    lines = ["[最近对话]"]
    for item in messages[-limit:]:
        role = labels.get(str(item.get("role") or ""), str(item.get("role") or "消息"))
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}：{content[:800]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def chunk_text(text: str, size: int = 12):
    for index in range(0, len(text), size):
        yield text[index : index + size]


def call_answer(answer_with_llm: AnswerWithLLM, question: str, context: str, config: RunnableConfig | dict[str, Any] | None) -> str:
    try:
        return answer_with_llm(question, context, config=config)
    except TypeError:
        return answer_with_llm(question, context)


def call_answer_stream(answer_with_llm_stream: AnswerWithLLMStream, question: str, context: str, config: RunnableConfig | dict[str, Any] | None):
    try:
        yield from answer_with_llm_stream(question, context, config=config)
    except TypeError:
        yield from answer_with_llm_stream(question, context)


def normalize_answer_result(value: Any) -> dict[str, Any]:
    if isinstance(value, TravelQAResponse):
        return {
            "answer": value.answer,
            "sources": list(value.sources),
            "used_web_search": bool(value.used_web_search),
        }
    if isinstance(value, dict):
        return {
            "answer": str(value.get("answer") or ""),
            "sources": normalize_sources(value.get("sources") or []),
            "used_web_search": bool(value.get("used_web_search")),
        }
    return {"answer": str(value or ""), "sources": [], "used_web_search": False}


def qa_stream_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("type") != "qa_metadata":
        return None
    return {
        "sources": normalize_sources(value.get("sources") or []),
        "used_web_search": bool(value.get("used_web_search")),
    }


def normalize_sources(values: list[Any]) -> list[TravelKnowledgeSource]:
    sources: list[TravelKnowledgeSource] = []
    for value in values:
        if isinstance(value, TravelKnowledgeSource):
            sources.append(value)
            continue
        try:
            sources.append(TravelKnowledgeSource.model_validate(value))
        except Exception:
            continue
    return merge_sources(sources)


def merge_sources(*groups: list[TravelKnowledgeSource]) -> list[TravelKnowledgeSource]:
    seen: set[str] = set()
    merged: list[TravelKnowledgeSource] = []
    for source in [item for group in groups for item in group]:
        key = (source.url or source.title or source.source).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(source)
    return merged
