from __future__ import annotations

import logging
from typing import Any

from langchain.agents import create_agent

try:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import PromptTemplate
except Exception:  # pragma: no cover - optional until dependencies are installed
    StrOutputParser = None
    PromptTemplate = None

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.knowledge.prompts import TRAVEL_QA_SYSTEM_PROMPT, TRAVEL_RAG_PROMPT
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore

logger = logging.getLogger(__name__)


class TravelQuestionAnsweringAgent:
    name = "TravelQuestionAnsweringAgent"

    def __init__(self, vector_store: PostgresTravelVectorStore | None, llm: Any | None = None):
        self.vector_store = vector_store
        self.llm = llm
        self.langchain_agent = self._safe_create_agent()
        self.chain = self._build_chain()

    def ask(self, question: str, top_k: int = 5) -> TravelQAResponse:
        docs = self._retrieve(question, top_k)
        context = format_documents(docs)
        answer = self._answer_with_llm(question, context) if self.llm is not None else ""
        generation_mode = "llm" if answer else "fallback"
        if not answer:
            answer = fallback_answer(question, docs)
        return TravelQAResponse(
            answer=answer,
            sources=[source_from_document(doc) for doc in docs],
            retrieved_count=len(docs),
            generation_mode=generation_mode,
        )

    def _retrieve(self, question: str, top_k: int) -> list[KnowledgeDocument]:
        if self.vector_store is None:
            return []
        try:
            return self.vector_store.similarity_search(question, k=top_k)
        except Exception as exc:
            logger.warning("Travel QA retrieval failed: %s", exc)
            return []

    def _answer_with_llm(self, question: str, context: str) -> str:
        if not context:
            return ""
        prompt_payload = {"input": question, "context": context}
        try:
            if self.chain is not None:
                return str(self.chain.invoke(prompt_payload)).strip()
            if self.langchain_agent is not None:
                response = self.langchain_agent.invoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": TRAVEL_RAG_PROMPT.format(**prompt_payload),
                            }
                        ]
                    }
                )
                return extract_agent_content(response).strip()
            response = self.llm.invoke(TRAVEL_RAG_PROMPT.format(**prompt_payload))
            return str(getattr(response, "content", response)).strip()
        except Exception as exc:
            logger.warning("Travel QA LLM answer failed, using fallback: %s", exc)
            return ""

    def _safe_create_agent(self):
        if self.llm is None:
            return None
        try:
            return create_agent(
                model=self.llm,
                tools=[],
                system_prompt=TRAVEL_QA_SYSTEM_PROMPT,
                name="travel_qa_agent",
            )
        except Exception as exc:
            logger.warning("LangChain create_agent failed for travel_qa_agent: %s", exc)
            return None

    def _build_chain(self):
        if self.llm is None or PromptTemplate is None or StrOutputParser is None:
            return None
        try:
            return PromptTemplate.from_template(TRAVEL_RAG_PROMPT) | self.llm | StrOutputParser()
        except Exception:
            return None


def format_documents(docs: list[KnowledgeDocument]) -> str:
    if not docs:
        return ""
    lines = []
    for index, doc in enumerate(docs, 1):
        source = doc.source_url or doc.source_name
        lines.append(
            f"[参考资料{index}] 标题：{doc.title or '旅行资料'}\n"
            f"来源：{source}\n"
            f"内容：{doc.content}\n"
        )
    return "\n".join(lines)


def fallback_answer(question: str, docs: list[KnowledgeDocument]) -> str:
    if not docs:
        return (
            "当前旅行知识库还没有检索到足够资料，暂时无法给出可靠结论。"
            "建议先执行资讯入库，或补充目的地、出行日期、关注点后再问。"
        )
    highlights = "\n".join(f"- {doc.summary or doc.content[:160]}" for doc in docs[:3])
    references = "、".join(doc.title or doc.source_name for doc in docs[:3])
    return f"根据已入库的旅行资料，关于“{question}”可先关注：\n{highlights}\n参考：{references}"


def source_from_document(doc: KnowledgeDocument) -> TravelKnowledgeSource:
    return TravelKnowledgeSource(
        title=doc.title or "旅行资料",
        url=doc.source_url,
        summary=doc.summary or doc.content[:180],
        source=doc.source_name,
        published_at=doc.published_at,
        score=round(float(doc.score or 0.0), 4),
    )


def extract_agent_content(response: Any) -> str:
    if isinstance(response, dict) and response.get("messages"):
        message = response["messages"][-1]
        return str(getattr(message, "content", message))
    return str(getattr(response, "content", response))

