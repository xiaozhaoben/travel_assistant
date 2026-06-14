from __future__ import annotations

import logging
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.models import TravelQAResponse
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore
from app.researching.research import WebSearchMCPClient

logger = logging.getLogger(__name__)


class TravelQAState(TypedDict, total=False):
    question: str
    top_k: int
    needs_realtime: bool
    realtime_docs: list[KnowledgeDocument]
    vector_docs: list[KnowledgeDocument]
    docs: list[KnowledgeDocument]
    context: str
    answer: str
    generation_mode: Literal["llm", "fallback"]
    response: TravelQAResponse


AnswerWithLLM = Callable[[str, str], str]


class TravelQAGraphRunner:
    name = "TravelQAGraphRunner"

    def __init__(
        self,
        vector_store: PostgresTravelVectorStore | None,
        llm: Any | None = None,
        web_client: WebSearchMCPClient | None = None,
        answer_with_llm: AnswerWithLLM | None = None,
    ):
        self.vector_store = vector_store
        self.llm = llm
        self.web_client = web_client or WebSearchMCPClient()
        self.answer_with_llm = answer_with_llm
        self.graph = self._build_graph()

    def ask(self, question: str, top_k: int = 5) -> TravelQAResponse:
        final_state = self.graph.invoke({"question": question, "top_k": top_k})
        return final_state["response"]

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
        return {"docs": docs, "context": format_documents(docs)}

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
