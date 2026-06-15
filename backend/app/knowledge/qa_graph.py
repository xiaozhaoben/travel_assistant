from __future__ import annotations

from typing import Any, Callable, Iterable, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore
from app.researching.research import WebSearchMCPClient


class TravelQAState(TypedDict, total=False):
    question: str
    top_k: int
    conversation_history: list[dict[str, str]]
    vector_docs: list[KnowledgeDocument]
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
        graph.add_node("retrieve_vector", self._retrieve_vector)
        graph.add_node("merge_and_rank", self._merge_and_rank)
        graph.add_node("answer_question", self._answer_question)
        graph.add_node("build_response", self._build_response)
        graph.add_edge(START, "retrieve_vector")
        graph.add_edge("retrieve_vector", "merge_and_rank")
        graph.add_edge("merge_and_rank", "answer_question")
        graph.add_edge("answer_question", "build_response")
        graph.add_edge("build_response", END)
        return graph.compile()

    def _retrieve_vector(self, state: TravelQAState) -> dict[str, Any]:
        if self.vector_store is None:
            return {"vector_docs": []}
        try:
            return {"vector_docs": self.vector_store.similarity_search(state["question"], k=int(state.get("top_k", 5)))}
        except Exception as exc:
            logger.warning("Travel QA retrieval failed: %s", exc)
            return {"vector_docs": []}

    def _merge_and_rank(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import format_documents, merge_documents

        top_k = int(state.get("top_k", 5))
        docs = merge_documents(
            state.get("vector_docs", []),
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
