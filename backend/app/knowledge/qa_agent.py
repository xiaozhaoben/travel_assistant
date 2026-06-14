from __future__ import annotations

import logging
from typing import Any

try:
    from langchain.agents import create_agent
except Exception:  # pragma: no cover - supports fallback mode with partial LangChain installs
    create_agent = None

try:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import PromptTemplate
except Exception:  # pragma: no cover - optional until dependencies are installed
    StrOutputParser = None
    PromptTemplate = None

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.knowledge.prompts import TRAVEL_QA_SYSTEM_PROMPT, TRAVEL_RAG_PROMPT
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore, stable_hash, summarize_text
from app.researching.research import WebSearchMCPClient

logger = logging.getLogger(__name__)

TIME_SENSITIVE_TERMS = ("预约", "开放", "闭馆", "限流", "公告", "节假日", "端午", "春节", "五一", "十一", "交通", "管制", "天气")
OFFICIAL_SOURCE_HINTS = (
    ".gov.cn",
    "mct.gov.cn",
    "zwfw",
    "museum",
    "博物馆",
    "文旅",
    "景区",
    "official",
)
GUIDE_SOURCE_HINTS = ("wikivoyage.org", "wikipedia.org", "opentripmap")
MAP_SOURCE_HINTS = ("amap.com", "google.com/maps", "foursquare.com")
COMMUNITY_SOURCE_HINTS = ("mafengwo", "ctrip", "trip.com", "xiaohongshu", "dianping", "qunar")


class TravelQuestionAnsweringAgent:
    name = "TravelQuestionAnsweringAgent"

    def __init__(
        self,
        vector_store: PostgresTravelVectorStore | None,
        llm: Any | None = None,
        web_client: WebSearchMCPClient | None = None,
    ):
        self.vector_store = vector_store
        self.llm = llm
        self.web_client = web_client or WebSearchMCPClient()
        self.langchain_agent = self._safe_create_agent()
        self.chain = self._build_chain()
        from app.knowledge import qa_graph

        self.graph_runner = qa_graph.TravelQAGraphRunner(
            vector_store,
            llm=llm,
            web_client=self.web_client,
            answer_with_llm=self._answer_with_llm,
            answer_with_llm_stream=self._answer_with_llm_stream,
        )

    def ask(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> TravelQAResponse:
        return self.graph_runner.ask(question, top_k=top_k, conversation_history=conversation_history)

    def stream(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
    ):
        yield from self.graph_runner.stream(question, top_k=top_k, conversation_history=conversation_history)

    def _retrieve(self, question: str, top_k: int) -> list[KnowledgeDocument]:
        if self.vector_store is None:
            return []
        try:
            return self.vector_store.similarity_search(question, k=top_k)
        except Exception as exc:
            logger.warning("Travel QA retrieval failed: %s", exc)
            return []

    def _retrieve_realtime(self, question: str, top_k: int) -> list[KnowledgeDocument]:
        if self.web_client is None or not self.web_client.available:
            return []
        if not should_search_realtime(question):
            return []

        docs: list[KnowledgeDocument] = []
        for query in build_realtime_queries(question):
            try:
                results = self.web_client.search(query)
            except Exception as exc:
                logger.warning("Travel QA realtime search failed: %s", exc)
                continue
            for item in results:
                doc = document_from_web_result(item)
                if doc is not None:
                    docs.append(doc)
        ranked = sorted(dedupe_documents(docs), key=document_rank, reverse=True)
        return ranked[: max(1, min(top_k, 6))]

    def _answer_with_llm(self, question: str, context: str) -> str:
        if not context:
            return ""
        prompt_payload = {"input": question, "context": context}
        rendered_prompt = TRAVEL_RAG_PROMPT.format(**prompt_payload)
        if self.langchain_agent is not None:
            try:
                response = self.langchain_agent.invoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": rendered_prompt,
                            }
                        ]
                    }
                )
                answer = extract_agent_content(response).strip()
                if answer:
                    return answer
            except Exception as exc:
                logger.warning("Travel QA LangChain agent answer failed, trying fallback chain: %s", exc)
        try:
            if self.chain is not None:
                return str(self.chain.invoke(prompt_payload)).strip()
            response = self.llm.invoke(rendered_prompt)
            return str(getattr(response, "content", response)).strip()
        except Exception as exc:
            logger.warning("Travel QA LLM answer failed, using fallback: %s", exc)
            return ""

    def _answer_with_llm_stream(self, question: str, context: str):
        if not context:
            return
        prompt_payload = {"input": question, "context": context}
        rendered_prompt = TRAVEL_RAG_PROMPT.format(**prompt_payload)
        if self.langchain_agent is not None and hasattr(self.langchain_agent, "stream"):
            try:
                try:
                    chunks = self.langchain_agent.stream(
                        {"messages": [{"role": "user", "content": rendered_prompt}]},
                        stream_mode="messages",
                    )
                except TypeError:
                    chunks = self.langchain_agent.stream({"messages": [{"role": "user", "content": rendered_prompt}]})
                for chunk in chunks:
                    content = extract_stream_content(chunk)
                    if content:
                        yield content
                return
            except Exception as exc:
                logger.warning("Travel QA LangChain agent stream failed, trying fallback stream: %s", exc)
        try:
            if self.chain is not None and hasattr(self.chain, "stream"):
                for chunk in self.chain.stream(prompt_payload):
                    content = str(getattr(chunk, "content", chunk))
                    if content:
                        yield content
                return
            if self.llm is not None and hasattr(self.llm, "stream"):
                for chunk in self.llm.stream(rendered_prompt):
                    content = str(getattr(chunk, "content", chunk))
                    if content:
                        yield content
                return
            answer = self._answer_with_llm(question, context)
            for index in range(0, len(answer), 12):
                yield answer[index : index + 12]
        except Exception as exc:
            logger.warning("Travel QA LLM stream failed, using fallback: %s", exc)

    def _safe_create_agent(self):
        if self.llm is None or create_agent is None:
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


def should_search_realtime(question: str) -> bool:
    return bool(question.strip()) and any(term in question for term in TIME_SENSITIVE_TERMS)


def build_realtime_queries(question: str) -> list[str]:
    question = question.strip()
    queries = [
        f"{question} 官方 预约 开放时间 交通 公告 文旅 景区 博物馆",
        f"{question} 文旅局 官方 游客服务 最新公告",
    ]
    if needs_international_sources(question):
        queries.append(f"{question} Wikivoyage official travel guide opening hours transport")
    return list(dict.fromkeys(queries))


def needs_international_sources(question: str) -> bool:
    if any(term in question for term in ("国外", "境外", "日本", "东京", "大阪", "京都", "韩国", "首尔", "泰国", "曼谷", "新加坡", "欧洲", "美国")):
        return True
    return bool(any("a" <= char.lower() <= "z" for char in question))


def document_from_web_result(item: dict[str, Any]) -> KnowledgeDocument | None:
    title = str(item.get("title") or item.get("name") or "实时旅行资料").strip()
    url = str(item.get("url") or item.get("link") or "").strip() or None
    content = str(item.get("content") or item.get("summary") or item.get("snippet") or item.get("description") or "").strip()
    if not content:
        return None
    source_name, score = classify_web_source(url or "", title)
    return KnowledgeDocument(
        id=stable_hash(url or title, content),
        title=title[:240],
        content=content,
        summary=summarize_text(content),
        source_url=url,
        source_name=source_name,
        published_at=None,
        score=score,
    )


def classify_web_source(url: str, title: str) -> tuple[str, float]:
    haystack = f"{url} {title}".lower()
    if any(hint.lower() in haystack for hint in OFFICIAL_SOURCE_HINTS):
        return "web-official", 0.97
    if any(hint in haystack for hint in MAP_SOURCE_HINTS):
        return "web-map", 0.9
    if any(hint in haystack for hint in GUIDE_SOURCE_HINTS):
        return "web-guide", 0.88
    if any(hint in haystack for hint in COMMUNITY_SOURCE_HINTS):
        return "web-community", 0.72
    return "web-search", 0.8


def merge_documents(*groups: list[KnowledgeDocument], limit: int) -> list[KnowledgeDocument]:
    return sorted(dedupe_documents([doc for group in groups for doc in group]), key=document_rank, reverse=True)[:limit]


def dedupe_documents(docs: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    seen: set[str] = set()
    unique: list[KnowledgeDocument] = []
    for doc in docs:
        key = (doc.source_url or doc.title or doc.id).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(doc)
    return unique


def document_rank(doc: KnowledgeDocument) -> float:
    source_bonus = {
        "web-official": 1.0,
        "web-map": 0.86,
        "web-guide": 0.82,
        "web-search": 0.72,
        "web-community": 0.48,
        "rss": 0.42,
    }.get(doc.source_name, 0.5)
    return source_bonus + float(doc.score or 0.0)


def fallback_answer(question: str, docs: list[KnowledgeDocument]) -> str:
    if not docs:
        return (
            "当前旅行知识库还没有检索到足够资料，暂时无法给出可靠结论。"
            "建议先执行资讯入库，或补充目的地、出行日期、关注点后再问。"
        )
    highlights = "\n".join(f"- [{source_label(doc.source_name)}] {doc.summary or doc.content[:160]}" for doc in docs[:3])
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


def source_label(source_name: str) -> str:
    labels = {
        "web-official": "官方/高可信",
        "web-map": "地图资料",
        "web-guide": "旅行指南",
        "web-community": "社区经验",
        "web-search": "实时搜索",
        "rss": "知识库",
    }
    return labels.get(source_name, source_name)


def extract_agent_content(response: Any) -> str:
    if isinstance(response, dict) and response.get("messages"):
        message = response["messages"][-1]
        return str(getattr(message, "content", message))
    return str(getattr(response, "content", response))


def extract_stream_content(chunk: Any) -> str:
    if isinstance(chunk, tuple) and chunk:
        return extract_stream_content(chunk[0])
    if isinstance(chunk, dict):
        if chunk.get("messages"):
            return extract_agent_content({"messages": chunk["messages"]})
        for value in chunk.values():
            if isinstance(value, dict) and value.get("messages"):
                return extract_agent_content({"messages": value["messages"]})
        return str(chunk.get("content") or "")
    return str(getattr(chunk, "content", chunk))

