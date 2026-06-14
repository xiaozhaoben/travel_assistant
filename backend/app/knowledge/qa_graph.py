from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.models import TravelQAResponse
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore
from app.researching.research import WebSearchMCPClient

logger = logging.getLogger(__name__)


class TravelQAState(TypedDict, total=False):
    question: str
    top_k: int
    conversation_history: list[dict[str, str]]
    needs_realtime: bool
    realtime_docs: list[KnowledgeDocument]
    vector_docs: list[KnowledgeDocument]
    docs: list[KnowledgeDocument]
    context: str
    answer: str
    generation_mode: Literal["llm", "fallback"]
    response: TravelQAResponse


AnswerWithLLM = Callable[[str, str], str]
AnswerWithLLMStream = Callable[[str, str], Iterable[str]]


class TravelQAGraphRunner:
    name = "TravelQAGraphRunner"

    def __init__(
        self,
        vector_store: PostgresTravelVectorStore | None,
        llm: Any | None = None,
        web_client: WebSearchMCPClient | None = None,
        answer_with_llm: AnswerWithLLM | None = None,
        answer_with_llm_stream: AnswerWithLLMStream | None = None,
    ):
        self.vector_store = vector_store
        self.llm = llm
        self.web_client = web_client or WebSearchMCPClient()
        self.answer_with_llm = answer_with_llm
        self.answer_with_llm_stream = answer_with_llm_stream
        self.graph = self._build_graph()

    def ask(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> TravelQAResponse:
        final_state = self.graph.invoke(
            {
                "question": question,
                "top_k": top_k,
                "conversation_history": conversation_history or [],
            }
        )
        return final_state["response"]

    def stream(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
    ):
        state = self._prepare_answer_state(question, top_k, conversation_history or [])
        docs = state.get("docs", [])
        answer_parts: list[str] = []

        if self.llm is not None and self.answer_with_llm_stream is not None and state.get("context"):
            for chunk in self.answer_with_llm_stream(question, state["context"]):
                if not chunk:
                    continue
                answer_parts.append(chunk)
                yield {"event": "answer_delta", "data": {"content": chunk}}
            answer = "".join(answer_parts).strip()
            if answer:
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
        state.update(self._classify_question(state))
        if self._route_after_classify(state) == "retrieve_realtime":
            state.update(self._retrieve_realtime(state))
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
        return TravelQAResponse(
            answer=answer,
            sources=[source_from_document(doc) for doc in docs],
            retrieved_count=len(docs),
            generation_mode=generation_mode,
        )

    def _build_graph(self):
        graph = StateGraph(TravelQAState)
        graph.add_node("classify_question", self._classify_question)
        graph.add_node("retrieve_realtime", self._retrieve_realtime)
        graph.add_node("retrieve_vector", self._retrieve_vector)
        graph.add_node("merge_and_rank", self._merge_and_rank)
        graph.add_node("answer_question", self._answer_question)
        graph.add_node("build_response", self._build_response)
        graph.add_edge(START, "classify_question")
        graph.add_conditional_edges(
            "classify_question",
            self._route_after_classify,
            {
                "retrieve_realtime": "retrieve_realtime",
                "retrieve_vector": "retrieve_vector",
            },
        )
        graph.add_edge("retrieve_realtime", "retrieve_vector")
        graph.add_edge("retrieve_vector", "merge_and_rank")
        graph.add_edge("merge_and_rank", "answer_question")
        graph.add_edge("answer_question", "build_response")
        graph.add_edge("build_response", END)
        return graph.compile()

    def _classify_question(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import should_search_realtime

        return {"needs_realtime": should_search_realtime(state["question"])}

    def _route_after_classify(self, state: TravelQAState) -> Literal["retrieve_realtime", "retrieve_vector"]:
        return "retrieve_realtime" if state.get("needs_realtime") else "retrieve_vector"

    def _retrieve_realtime(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import build_realtime_queries, dedupe_documents, document_from_web_result, document_rank

        if self.web_client is None or not self.web_client.available:
            return {"realtime_docs": []}

        docs: list[KnowledgeDocument] = []
        for query in build_realtime_queries(state["question"]):
            try:
                results = self.web_client.search(query)
            except Exception as exc:
                logger.warning("Travel QA realtime search failed: %s", exc)
                continue
            for item in results:
                doc = document_from_web_result(item)
                if doc is not None:
                    docs.append(doc)
        top_k = max(1, min(int(state.get("top_k", 5)), 6))
        ranked = sorted(dedupe_documents(docs), key=document_rank, reverse=True)
        return {"realtime_docs": ranked[:top_k]}

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
            state.get("realtime_docs", []),
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

    def _answer_question(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import fallback_answer

        answer = ""
        context = state.get("context", "")
        if self.llm is not None and self.answer_with_llm is not None:
            answer = self.answer_with_llm(state["question"], context)
        if answer:
            return {"answer": answer, "generation_mode": "llm"}
        return {
            "answer": fallback_answer(state["question"], state.get("docs", [])),
            "generation_mode": "fallback",
        }

    def _build_response(self, state: TravelQAState) -> dict[str, Any]:
        from app.knowledge.qa_agent import source_from_document

        docs = state.get("docs", [])
        return {
            "response": TravelQAResponse(
                answer=state["answer"],
                sources=[source_from_document(doc) for doc in docs],
                retrieved_count=len(docs),
                generation_mode=state.get("generation_mode", "fallback"),
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
