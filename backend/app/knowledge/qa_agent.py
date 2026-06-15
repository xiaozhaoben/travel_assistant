from __future__ import annotations

import json
import logging
from typing import Any

try:
    from langgraph.prebuilt import create_react_agent
except Exception:  # pragma: no cover - supports fallback mode with partial LangGraph installs
    create_react_agent = None

try:
    from langgraph.prebuilt.chat_agent_executor import AgentState
    from typing_extensions import NotRequired

    class TravelQAAgentState(AgentState, total=False):
        context: NotRequired[dict[str, Any]]

except Exception:  # pragma: no cover - optional until dependencies are installed
    TravelQAAgentState = None

try:
    import typing as _typing
    from typing_extensions import NotRequired as _NotRequired, Required as _Required

    if not hasattr(_typing, "NotRequired"):
        _typing.NotRequired = _NotRequired
    if not hasattr(_typing, "Required"):
        _typing.Required = _Required
    from langmem.short_term import SummarizationNode
except Exception:  # pragma: no cover - optional until dependencies are installed
    SummarizationNode = None

try:
    from langchain_tavily import TavilySearch
except Exception:  # pragma: no cover - optional until dependency is installed
    TavilySearch = None

try:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import PromptTemplate
except Exception:  # pragma: no cover - optional until dependencies are installed
    StrOutputParser = None
    PromptTemplate = None

from app.domain.models import TravelKnowledgeSource, TravelQAResponse
from app.core.config import get_settings
from app.knowledge.prompts import TRAVEL_QA_SYSTEM_PROMPT, TRAVEL_RAG_PROMPT
from app.knowledge.vector_store import KnowledgeDocument, PostgresTravelVectorStore, stable_hash, summarize_text
from app.researching.research import WebSearchMCPClient

logger = logging.getLogger(__name__)

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
        checkpointer: Any | None = None,
    ):
        self.vector_store = vector_store
        self.llm = llm
        self.web_client = web_client or WebSearchMCPClient()
        self.checkpointer = checkpointer
        self.tools = self._build_tools()
        self.langgraph_agent = self._safe_create_react_agent()
        self.langchain_agent = self.langgraph_agent
        self.chain = self._build_chain()
        from app.knowledge import qa_graph

        self.graph_runner = qa_graph.TravelQAGraphRunner(
            vector_store,
            llm=llm,
            web_client=self.web_client,
            answer_with_llm=self._answer_with_llm,
            answer_with_llm_stream=self._answer_with_llm_stream,
            checkpointer=checkpointer,
        )

    def ask(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> TravelQAResponse:
        return self.graph_runner.ask(question, top_k=top_k, conversation_history=conversation_history, config=config)

    def stream(
        self,
        question: str,
        top_k: int = 5,
        conversation_history: list[dict[str, str]] | None = None,
        config: dict[str, Any] | None = None,
    ):
        yield from self.graph_runner.stream(
            question,
            top_k=top_k,
            conversation_history=conversation_history,
            config=config,
        )

    def _retrieve(self, question: str, top_k: int) -> list[KnowledgeDocument]:
        if self.vector_store is None:
            return []
        try:
            return self.vector_store.similarity_search(question, k=top_k)
        except Exception as exc:
            logger.warning("Travel QA retrieval failed: %s", exc)
            return []

    def _answer_with_llm(self, question: str, context: str, config: dict[str, Any] | None = None) -> str:
        if not context and self.langchain_agent is None:
            return ""
        if not context and not self.tools:
            return ""
        forced_sources, forced_used_web_search = self._forced_web_search(question)
        prompt_payload = {
            "input": question,
            "context": qa_context_for_prompt(
                append_web_sources_to_context(context, forced_sources),
                force_web_search=should_force_web_search(question),
            ),
        }
        rendered_prompt = TRAVEL_RAG_PROMPT.format(**prompt_payload)
        if self.langchain_agent is not None:
            try:
                agent_input = {"messages": [{"role": "user", "content": rendered_prompt}]}
                response = self.langchain_agent.invoke(agent_input, config) if config else self.langchain_agent.invoke(agent_input)
                answer = extract_agent_content(response).strip()
                if answer:
                    sources, used_web_search = extract_tavily_sources(response)
                    return {
                        "answer": answer,
                        "sources": merge_knowledge_sources(forced_sources, sources),
                        "used_web_search": forced_used_web_search or used_web_search,
                    }
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

    def _answer_with_llm_stream(self, question: str, context: str, config: dict[str, Any] | None = None):
        if not context and self.langchain_agent is None:
            return
        if not context and not self.tools:
            return
        forced_sources, forced_used_web_search = self._forced_web_search(question)
        prompt_payload = {
            "input": question,
            "context": qa_context_for_prompt(
                append_web_sources_to_context(context, forced_sources),
                force_web_search=should_force_web_search(question),
            ),
        }
        rendered_prompt = TRAVEL_RAG_PROMPT.format(**prompt_payload)
        if self.langchain_agent is not None and hasattr(self.langchain_agent, "stream"):
            try:
                agent_input = {"messages": [{"role": "user", "content": rendered_prompt}]}
                try:
                    chunks = self.langchain_agent.stream(agent_input, config, stream_mode="messages") if config else self.langchain_agent.stream(agent_input, stream_mode="messages")
                except TypeError:
                    chunks = self.langchain_agent.stream(agent_input, config) if config else self.langchain_agent.stream(agent_input)
                web_sources: list[TravelKnowledgeSource] = list(forced_sources)
                used_web_search = forced_used_web_search
                for chunk in chunks:
                    chunk_sources, chunk_used_web_search = extract_tavily_sources(chunk)
                    if chunk_sources:
                        web_sources = merge_knowledge_sources(web_sources, chunk_sources)
                    used_web_search = used_web_search or chunk_used_web_search
                    content = extract_stream_content(chunk)
                    if content:
                        yield content
                if used_web_search or web_sources:
                    yield {"type": "qa_metadata", "sources": web_sources, "used_web_search": used_web_search}
                return
            except Exception as exc:
                logger.warning("Travel QA LangChain agent stream failed, trying fallback stream: %s", exc)
        try:
            if self.chain is not None and hasattr(self.chain, "stream"):
                if not context:
                    return
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

    def _forced_web_search(self, question: str) -> tuple[list[TravelKnowledgeSource], bool]:
        if not should_force_web_search(question):
            return [], False
        tavily_tool = next((tool for tool in self.tools if "tavily" in str(getattr(tool, "name", "")).lower()), None)
        if tavily_tool is None:
            return [], False
        try:
            try:
                payload = tavily_tool.invoke({"query": question})
            except TypeError:
                payload = tavily_tool.invoke(question)
        except Exception as exc:
            logger.warning("Forced Tavily search failed for travel QA: %s", exc)
            return [], True
        return sources_from_tavily_payload(payload), True

    def _safe_create_react_agent(self):
        if self.llm is None or create_react_agent is None:
            return None
        try:
            kwargs: dict[str, Any] = {
                "model": self.llm,
                "tools": self.tools,
                "prompt": TRAVEL_QA_SYSTEM_PROMPT,
                "name": "travel_qa_agent",
            }
            summary_hook = self._build_summary_hook()
            if summary_hook is not None:
                kwargs["pre_model_hook"] = summary_hook
                if TravelQAAgentState is not None:
                    kwargs["state_schema"] = TravelQAAgentState
            if self.checkpointer is not None:
                kwargs["checkpointer"] = self.checkpointer
            return create_react_agent(**kwargs)
        except Exception as exc:
            logger.warning("LangGraph create_react_agent failed for travel_qa_agent: %s", exc)
            return None

    def _build_tools(self) -> list[Any]:
        tools: list[Any] = []
        settings = get_settings()
        if settings.disable_external_api or not settings.tavily_api_key or TavilySearch is None:
            return tools
        try:
            tools.append(
                TavilySearch(
                    max_results=settings.tavily_max_results,
                    search_depth=settings.tavily_search_depth,
                    include_answer=True,
                    include_raw_content=False,
                )
            )
        except Exception as exc:
            logger.warning("Tavily search tool unavailable for travel QA: %s", exc)
        return tools

    def _build_summary_hook(self):
        if self.llm is None or SummarizationNode is None:
            return None
        try:
            summarization_node = SummarizationNode(
                model=self.llm,
                max_tokens=4096,
                max_tokens_before_summary=3072,
                max_summary_tokens=512,
                output_messages_key="summarized_messages",
            )
            return ReactAgentSummarizationHook(summarization_node)
        except Exception as exc:
            logger.warning("Travel QA summarization hook unavailable: %s", exc)
            return None

    def _build_chain(self):
        if self.llm is None or PromptTemplate is None or StrOutputParser is None:
            return None
        try:
            return PromptTemplate.from_template(TRAVEL_RAG_PROMPT) | self.llm | StrOutputParser()
        except Exception:
            return None


class ReactAgentSummarizationHook:
    def __init__(self, summarization_node: Any):
        self.summarization_node = summarization_node

    def __call__(self, state: Any) -> dict[str, Any]:
        state_messages = state_get(state, "messages", [])
        try:
            if hasattr(self.summarization_node, "invoke"):
                update = self.summarization_node.invoke(state)
            else:
                update = self.summarization_node(state)
        except Exception as exc:
            logger.warning("Travel QA context summarization failed, using raw messages: %s", exc)
            return {"llm_input_messages": state_messages}

        summarized_messages = state_get(update, "summarized_messages", None) or state_get(update, "messages", None) or state_messages
        result: dict[str, Any] = {"llm_input_messages": summarized_messages}
        context = state_get(update, "context", None)
        if context is not None:
            result["context"] = context
        return result


def state_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def qa_context_for_prompt(context: str, force_web_search: bool = False) -> str:
    text = context.strip()
    if force_web_search:
        instruction = (
            "【联网要求】用户明确要求联网或问题涉及时效信息，必须先调用联网搜索工具"
            "查询官方/高可信资料；不要仅根据下方参考资料或历史对话回答。"
        )
        if text:
            return f"{instruction}\n\n{text}"
        return f"{instruction}\n\n知识库和实时资料暂未检索到结果。"
    if text:
        return text
    return "知识库和实时资料暂未检索到结果。请先使用联网搜索工具查询官方/高可信资料，再基于搜索结果回答。"


def append_web_sources_to_context(context: str, sources: list[TravelKnowledgeSource]) -> str:
    web_context = format_web_sources_for_prompt(sources)
    text = context.strip()
    if text and web_context:
        return f"{text}\n\n{web_context}"
    return web_context or text


def format_web_sources_for_prompt(sources: list[TravelKnowledgeSource]) -> str:
    if not sources:
        return ""
    lines = ["[联网搜索结果]"]
    for index, source in enumerate(sources, 1):
        lines.append(
            f"[联网资料{index}] 标题：{source.title}\n"
            f"来源：{source.url or source.source}\n"
            f"内容：{source.summary}\n"
        )
    return "\n".join(lines)


def should_force_web_search(question: str) -> bool:
    text = question.strip().lower()
    if not text:
        return False
    triggers = (
        "联网",
        "网上查",
        "网上查询",
        "实时",
        "最新",
        "查一下",
        "查询一下",
        "搜索一下",
        "天气",
        "预报",
        "开放时间",
        "营业时间",
        "闭馆",
        "预约",
        "票务",
        "余票",
        "公告",
        "限流",
    )
    return any(trigger in text for trigger in triggers)


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


def extract_tavily_sources(value: Any) -> tuple[list[TravelKnowledgeSource], bool]:
    sources: list[TravelKnowledgeSource] = []
    used_web_search = False
    for message in extract_messages(value):
        if not is_tool_message(message):
            continue
        payloads = tool_payloads(message)
        if is_tavily_tool_message(message) or any(payload_has_results(payload) for payload in payloads):
            used_web_search = True
        for payload in payloads:
            for item in result_items(payload):
                source = source_from_web_result(item)
                if source is not None:
                    sources.append(source)
    return merge_knowledge_sources(sources), used_web_search


def sources_from_tavily_payload(payload: Any) -> list[TravelKnowledgeSource]:
    sources: list[TravelKnowledgeSource] = []
    for parsed in parse_payload(payload):
        for item in result_items(parsed):
            source = source_from_web_result(item)
            if source is not None:
                sources.append(source)
    return merge_knowledge_sources(sources)


def extract_messages(value: Any) -> list[Any]:
    if isinstance(value, tuple) and value:
        return extract_messages(value[0])
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            return messages
        nested: list[Any] = []
        for item in value.values():
            if isinstance(item, dict) and isinstance(item.get("messages"), list):
                nested.extend(item["messages"])
        if nested:
            return nested
    return [value]


def is_tool_message(message: Any) -> bool:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "").lower()
        return role == "tool" or bool(message.get("tool_call_id"))
    message_type = str(getattr(message, "type", "") or "").lower()
    return message_type == "tool" or bool(getattr(message, "tool_call_id", None))


def is_tavily_tool_message(message: Any) -> bool:
    if isinstance(message, dict):
        name = str(message.get("name") or "").lower()
    else:
        name = str(getattr(message, "name", "") or "").lower()
    return "tavily" in name


def tool_payloads(message: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(message, dict):
        values.extend([message.get("content"), message.get("artifact")])
    else:
        values.extend([getattr(message, "content", None), getattr(message, "artifact", None)])
    payloads: list[Any] = []
    for value in values:
        payloads.extend(parse_payload(value))
    return payloads


def parse_payload(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (dict, list)):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            return []
    return []


def payload_has_results(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("results"), list)


def result_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        if any(key in payload for key in ("url", "link", "content", "summary", "snippet")):
            return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def source_from_web_result(item: dict[str, Any]) -> TravelKnowledgeSource | None:
    doc = document_from_web_result(item)
    if doc is None:
        return None
    return source_from_document(doc)


def merge_knowledge_sources(*groups: list[TravelKnowledgeSource]) -> list[TravelKnowledgeSource]:
    seen: set[str] = set()
    merged: list[TravelKnowledgeSource] = []
    for source in [item for group in groups for item in group]:
        key = (source.url or source.title or source.source).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(source)
    return merged


def extract_agent_content(response: Any) -> str:
    messages = [message for message in extract_messages(response) if not is_tool_message(message)]
    if messages:
        message = messages[-1]
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(getattr(message, "content", message))
    return str(getattr(response, "content", response))


def extract_stream_content(chunk: Any) -> str:
    if isinstance(chunk, tuple) and chunk:
        return extract_stream_content(chunk[0])
    if is_tool_message(chunk):
        return ""
    if isinstance(chunk, dict):
        if chunk.get("messages"):
            return extract_agent_content({"messages": chunk["messages"]})
        for value in chunk.values():
            if isinstance(value, dict) and value.get("messages"):
                return extract_agent_content({"messages": value["messages"]})
        return str(chunk.get("content") or "")
    return str(getattr(chunk, "content", chunk))

