import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _called_by_unittest_loader() -> bool:
    frame = sys._getframe()
    while frame is not None:
        filename = frame.f_code.co_filename.replace("\\", "/")
        if filename.endswith("/unittest/loader.py"):
            return True
        frame = frame.f_back
    return False

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.core.config import get_settings
from app.domain.models import Attraction, Hotel, Location, Meal, TravelKnowledgeSource, TravelQAResponse, TravelRequirement, TripPlanRequest, WeatherInfo
from app.integrations.services import AmapMCPClient, AmapStdioMCPToolCaller, BudgetCalculator, TravelRequirementParser, UnsplashMCPClient
from app.prompts.agent_prompts import AgentPrompts
from app.storage.plan_log import PlanLogRecorder
from app.workflows.agents import AttractionSearchAgent, HotelAgent, PlannerAgent, TravelAgentOrchestrator, WeatherQueryAgent


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeToolMessage:
    type = "tool"
    name = "tavily_search"
    tool_call_id = "tool-call-1"

    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeMessage(self.content)


class SequentialFakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        content = self.contents.pop(0) if self.contents else "{}"
        return FakeMessage(content)


class FakeLangChainAgent:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def invoke(self, state):
        self.calls.append(state)
        return {"messages": [FakeMessage(self.content)]}

    def stream(self, state, **kwargs):
        self.calls.append(state)
        yield (FakeMessage(self.content), {"kwargs": kwargs})


class FakeHttpResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeHttpResponse(self.responses.pop(0))


class FakeMCPCaller:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call_tool(self, tool_name, arguments):
        self.calls.append({"tool_name": tool_name, "arguments": arguments})
        return self.responses.pop(0)


class FakeWebSearchCaller:
    def __init__(self):
        self.calls = []

    def call_tool(self, tool_name, arguments):
        self.calls.append({"tool_name": tool_name, "arguments": arguments})
        return {
            "results": [
                {
                    "title": "北京故宫预约攻略",
                    "url": "https://example.com/gugong",
                    "content": "故宫博物院需要提前预约，上午人流较多，适合安排半日游。",
                }
            ]
        }


class FakeTravelVectorStore:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.saved = []
        self.vector_queries = []
        self.keyword_queries = []

    def add_text(self, **kwargs):
        self.saved.append(kwargs)
        return 1

    def similarity_search(self, query, k=5):
        self.vector_queries.append(query)
        return self.docs[:k]

    def keyword_search(self, query, k=5):
        self.keyword_queries.append(query)
        return self.docs[:k]

    def health(self):
        return {"enabled": True, "ok": True, "pgvector_enabled": True, "table_ready": True}


def test_destination_research_service_uses_web_mcp_and_returns_snippets(tmp_path):
    from app.researching.research import DestinationResearchService, WebSearchMCPClient

    caller = FakeWebSearchCaller()
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    service = DestinationResearchService(web_client=web, cache_path=tmp_path / "research_cache.json")

    snippets = service.research("北京", ["历史文化"], days=3)

    assert caller.calls
    assert caller.calls[0]["tool_name"] == "web_search"
    assert "北京" in caller.calls[0]["arguments"]["query"]
    assert snippets[0].title == "北京故宫预约攻略"
    assert snippets[0].source == "web"
    assert "预约" in snippets[0].keywords


def test_web_search_client_normalizes_tavily_tool_name_alias():
    from app.researching.research import WebSearchMCPClient

    caller = FakeWebSearchCaller()
    web = WebSearchMCPClient(tool_name="tavily-search", mcp_caller=caller)

    web.search("Zhuhai Museum official website", {"max_results": 5})

    assert caller.calls[0]["tool_name"] == "tavily_search"


def test_mcp_cleanup_error_detection_handles_nested_broken_resource_error():
    import anyio
    from exceptiongroup import ExceptionGroup

    from app.integrations.mcp_utils import is_broken_resource_cleanup_error

    error = ExceptionGroup("unhandled errors in a TaskGroup", [anyio.BrokenResourceError()])

    assert is_broken_resource_cleanup_error(error)
    assert not is_broken_resource_cleanup_error(RuntimeError("Unknown tool"))


def test_mcp_cleanup_error_detection_handles_deeply_nested_taskgroup_error():
    import anyio
    from exceptiongroup import ExceptionGroup

    from app.integrations.mcp_utils import is_broken_resource_cleanup_error

    error = ExceptionGroup(
        "outer",
        [
            ExceptionGroup(
                "inner",
                [anyio.BrokenResourceError()],
            )
        ],
    )

    assert is_broken_resource_cleanup_error(error)


def test_mcp_stdio_cleanup_wait_uses_short_delay():
    import anyio

    from app.integrations.mcp_utils import MCP_STDIO_CLEANUP_DELAY_SECONDS, wait_for_stdio_transport_cleanup

    calls = []

    class FakeAnyio:
        @staticmethod
        async def sleep(delay):
            calls.append(delay)

    anyio.run(wait_for_stdio_transport_cleanup, FakeAnyio)

    assert calls == [MCP_STDIO_CLEANUP_DELAY_SECONDS]
    assert 0 < MCP_STDIO_CLEANUP_DELAY_SECONDS <= 0.25


def test_news_ingestion_agent_parses_rss_and_saves_to_vector_store(monkeypatch):
    from app.knowledge import news_agent

    feedparser_stub = SimpleNamespace(
        parse=lambda url: SimpleNamespace(
            entries=[
                {
                    "title": "南京端午预约提醒",
                    "description": "<p>热门景区建议提前预约，夜游夫子庙适合错峰。</p>",
                    "summary": "地铁客流较大，建议预留换乘时间。",
                    "link": "https://example.test/nanjing",
                    "published": "Thu, 21 May 2026 09:00:00 +0800",
                }
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "feedparser", feedparser_stub)
    monkeypatch.setattr(news_agent, "parse_feed", lambda parser, url: parser.parse(url))
    store = FakeTravelVectorStore()

    result = news_agent.TravelNewsIngestionAgent(store).fetch_travel_feeds(["https://feeds.example.test/travel"])

    assert result["total_seen"] == 1
    assert result["total_added"] == 1
    assert store.saved[0]["source_url"] == "https://example.test/nanjing"
    assert store.saved[0]["title"] == "南京端午预约提醒"
    assert "<p>" not in store.saved[0]["content"]
    assert "提前预约" in store.saved[0]["content"]


def test_travel_qa_agent_answers_from_retrieved_vector_documents():
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    doc = KnowledgeDocument(
        id="doc-1",
        title="南京端午预约提醒",
        content="南京端午期间热门景区建议提前预约，夫子庙夜游适合晚间错峰。",
        summary="热门景区建议提前预约，夫子庙夜游适合晚间错峰。",
        source_url="https://example.test/nanjing",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    agent = TravelQuestionAnsweringAgent(FakeTravelVectorStore([doc]), llm=None, web_client=WebSearchMCPClient(command=""))

    result = agent.ask("端午去南京要注意什么？")

    assert result.generation_mode == "fallback"
    assert result.retrieved_count == 1
    assert "提前预约" in result.answer
    assert result.sources[0].title == "南京端午预约提醒"


def test_travel_qa_agent_does_not_use_realtime_search_node_for_time_sensitive_questions():
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    class RealtimeCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            raise AssertionError("realtime MCP node should not run in the simplified QA graph")

    doc = KnowledgeDocument(
        id="doc-1",
        title="南京端午预约提醒",
        content="南京端午期间热门景区建议提前预约，夫子庙夜游适合晚间错峰。",
        summary="热门景区建议提前预约，夫子庙夜游适合晚间错峰。",
        source_url="https://example.test/nanjing",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    caller = RealtimeCaller()
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    agent = TravelQuestionAnsweringAgent(FakeTravelVectorStore([doc]), llm=None, web_client=web)

    result = agent.ask("端午去南京三天有哪些预约和交通注意事项？")

    assert caller.calls == []
    assert result.retrieved_count == 1
    assert result.sources[0].source == "rss"
    assert "提前预约" in result.answer


def test_travel_qa_graph_uses_vector_documents_without_realtime_node():
    if _called_by_unittest_loader():
        return unittest.FunctionTestCase(_assert_travel_qa_graph_uses_vector_documents_without_realtime_node)
    _assert_travel_qa_graph_uses_vector_documents_without_realtime_node()


def _assert_travel_qa_graph_uses_vector_documents_without_realtime_node():
    from app.knowledge.qa_graph import TravelQAGraphRunner
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    class RealtimeCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            raise AssertionError("realtime MCP node should not run in the simplified QA graph")

    vector_doc = KnowledgeDocument(
        id="doc-1",
        title="南京夜游提醒",
        content="夫子庙夜游适合晚间错峰，秦淮河沿线周末人流较多。",
        summary="夫子庙夜游适合晚间错峰。",
        source_url="https://example.test/nanjing-night",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    caller = RealtimeCaller()
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    runner = TravelQAGraphRunner(FakeTravelVectorStore([vector_doc]), llm=None, web_client=web)

    result = runner.ask("端午去南京三天有哪些预约和交通注意事项？", top_k=5)

    assert caller.calls == []
    assert result.retrieved_count == 1
    assert result.sources[0].source == "rss"
    assert "夫子庙" in result.answer


def test_travel_qa_graph_includes_conversation_history_in_context():
    from app.knowledge.qa_graph import TravelQAGraphRunner
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    captured = {}

    def answer_with_llm(question, context):
        captured["question"] = question
        captured["context"] = context
        return "可以继续安排南京的预约和夜游。"

    doc = KnowledgeDocument(
        id="doc-1",
        title="南京预约资料",
        content="南京博物院建议提前预约。",
        summary="南京博物院建议提前预约。",
        source_url="https://example.test/nanjing",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    runner = TravelQAGraphRunner(
        FakeTravelVectorStore([doc]),
        llm=FakeLLM("unused"),
        web_client=WebSearchMCPClient(command=""),
        answer_with_llm=answer_with_llm,
    )

    result = runner.ask(
        "那博物馆怎么预约？",
        conversation_history=[
            {"role": "user", "content": "端午去南京三天怎么安排？"},
            {"role": "assistant", "content": "建议重点关注夫子庙夜游和南京博物院预约。"},
        ],
    )

    assert result.generation_mode == "llm"
    assert "端午去南京三天" in captured["context"]
    assert "南京博物院预约" in captured["context"]
    assert "南京预约资料" in captured["context"]


def test_travel_qa_graph_expands_ambiguous_question_for_hybrid_retrieval():
    from app.knowledge.qa_graph import TravelQAGraphRunner
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    keyword_doc = KnowledgeDocument(
        id="doc-keyword",
        title="南京博物院预约说明",
        content="南京博物院需要在官方小程序提前预约，节假日建议尽早选择入馆时段。",
        summary="南京博物院需要提前预约。",
        source_url="https://example.test/nanjing-museum",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.35,
    )

    class HybridStore(FakeTravelVectorStore):
        def similarity_search(self, query, k=5):
            self.vector_queries.append(query)
            return []

        def keyword_search(self, query, k=5):
            self.keyword_queries.append(query)
            if "南京博物院" in query:
                return [keyword_doc]
            return []

    store = HybridStore()
    runner = TravelQAGraphRunner(store, llm=None, web_client=WebSearchMCPClient(command=""))

    result = runner.ask(
        "那怎么预约？",
        conversation_history=[{"role": "user", "content": "我想去南京博物院"}],
    )

    assert any("南京博物院" in query for query in store.keyword_queries)
    assert result.retrieved_count == 1
    assert result.sources[0].title == "南京博物院预约说明"


def test_travel_qa_graph_reranks_complex_question_results():
    from app.knowledge.qa_graph import TravelQAGraphRunner
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    generic_doc = KnowledgeDocument(
        id="generic",
        title="成都概览",
        content="成都适合城市休闲游，美食和街区体验丰富。",
        summary="成都适合城市休闲游。",
        source_url="https://example.test/chengdu-overview",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.95,
    )
    matched_doc = KnowledgeDocument(
        id="matched",
        title="成都亲子景点和交通",
        content="成都亲子游可以安排熊猫基地、自然博物馆，地铁和景区直通车适合串联景点交通。",
        summary="成都亲子景点和交通建议。",
        source_url="https://example.test/chengdu-family-traffic",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.35,
    )

    store = FakeTravelVectorStore([generic_doc, matched_doc])
    runner = TravelQAGraphRunner(store, llm=None, web_client=WebSearchMCPClient(command=""))

    state = runner._prepare_answer_state("成都亲子景点和交通怎么安排？预算有限", top_k=2, conversation_history=[])

    assert state["docs"][0].id == "matched"


def test_travel_qa_agent_delegates_to_langgraph_runner():
    if _called_by_unittest_loader():
        return unittest.FunctionTestCase(_assert_travel_qa_agent_delegates_to_langgraph_runner)
    _assert_travel_qa_agent_delegates_to_langgraph_runner()


def _assert_travel_qa_agent_delegates_to_langgraph_runner():
    from app.knowledge import qa_graph
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    class FakeGraphRunner:
        constructions = []

        def __init__(
            self,
            vector_store,
            llm=None,
            web_client=None,
            answer_with_llm=None,
            answer_with_llm_stream=None,
            checkpointer=None,
        ):
            self.calls = []
            FakeGraphRunner.constructions.append(
                {
                    "runner": self,
                    "vector_store": vector_store,
                    "llm": llm,
                    "web_client": web_client,
                    "answer_with_llm": answer_with_llm,
                    "answer_with_llm_stream": answer_with_llm_stream,
                    "checkpointer": checkpointer,
                }
            )

        def ask(self, question, top_k=5, conversation_history=None, config=None):
            self.calls.append(
                {"question": question, "top_k": top_k, "conversation_history": conversation_history, "config": config}
            )
            return TravelQAResponse(answer="graph answer", sources=[], retrieved_count=0, generation_mode="fallback")

    vector_store = FakeTravelVectorStore()
    web_client = WebSearchMCPClient(command="")

    original_runner = qa_graph.TravelQAGraphRunner
    qa_graph.TravelQAGraphRunner = FakeGraphRunner
    try:
        agent = TravelQuestionAnsweringAgent(vector_store, llm=None, web_client=web_client)
        result = agent.ask("南京怎么预约热门景点？", top_k=3)
    finally:
        qa_graph.TravelQAGraphRunner = original_runner

    assert result.answer == "graph answer"
    assert FakeGraphRunner.constructions[0]["vector_store"] is vector_store
    assert FakeGraphRunner.constructions[0]["web_client"] is web_client
    assert FakeGraphRunner.constructions[0]["answer_with_llm_stream"] is not None
    assert FakeGraphRunner.constructions[0]["runner"].calls == [
        {"question": "南京怎么预约热门景点？", "top_k": 3, "conversation_history": None, "config": None}
    ]


def test_travel_qa_agent_builds_react_agent_with_checkpointer_and_summary_hook(monkeypatch):
    from app.knowledge import qa_agent as qa_agent_module
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    captured = {}

    class FakeSummaryNode:
        def __init__(self, **kwargs):
            captured["summary_kwargs"] = kwargs

    runtime = object()

    def fake_create_react_agent(**kwargs):
        captured["react_kwargs"] = kwargs
        return runtime

    monkeypatch.setattr(qa_agent_module, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(qa_agent_module, "SummarizationNode", FakeSummaryNode)

    checkpointer = object()
    agent = TravelQuestionAnsweringAgent(
        FakeTravelVectorStore(),
        llm=FakeLLM("unused"),
        web_client=WebSearchMCPClient(command=""),
        checkpointer=checkpointer,
    )

    assert agent.langgraph_agent is runtime
    assert captured["react_kwargs"]["model"] is agent.llm
    assert captured["react_kwargs"]["tools"] == []
    assert captured["react_kwargs"]["checkpointer"] is checkpointer
    assert captured["react_kwargs"]["pre_model_hook"] is not None
    assert captured["summary_kwargs"]["model"] is agent.llm
    assert captured["summary_kwargs"]["output_messages_key"] == "summarized_messages"


def test_travel_qa_agent_prefers_create_react_agent_runtime(monkeypatch):
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    class QAAgentRuntime:
        def __init__(self):
            self.calls = []

        def invoke(self, state, config=None):
            self.calls.append({"state": state, "config": config})
            return {"messages": [FakeMessage("请优先查看官方渠道并提前预约。")]}

    doc = KnowledgeDocument(
        id="doc-1",
        title="北京预约提醒",
        content="热门场馆需要提前预约。",
        summary="热门场馆需要提前预约。",
        source_url="https://example.test/beijing",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    runtime = QAAgentRuntime()
    monkeypatch.setattr("app.knowledge.qa_agent.create_react_agent", lambda *args, **kwargs: runtime)
    agent = TravelQuestionAnsweringAgent(FakeTravelVectorStore([doc]), llm=FakeLLM("chain should not be first"), web_client=WebSearchMCPClient(command=""))
    agent.chain = SimpleNamespace(invoke=lambda payload: (_ for _ in ()).throw(AssertionError("chain should be fallback only")))

    result = agent.ask("北京热门场馆怎么预约？", config={"configurable": {"thread_id": "qa-thread-1"}})

    assert runtime.calls
    assert runtime.calls[0]["config"]["configurable"]["thread_id"] == "qa-thread-1"
    assert result.generation_mode == "llm"
    assert "提前预约" in result.answer


def test_travel_qa_agent_adds_tavily_tool_when_configured(monkeypatch):
    from app.knowledge import qa_agent as qa_agent_module
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    captured = {}

    class FakeTavilySearch:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.name = "tavily_search"

        def invoke(self, payload):
            self.calls.append(payload)
            return {
                "results": [
                    {
                        "title": "珠海天气官方预报",
                        "url": "https://example.gov.cn/zhuhai-weather",
                        "content": "珠海18号多云，有阵雨概率，请关注气象台预警。",
                        "score": 0.92,
                        }
                    ]
                }

    def fake_create_react_agent(**kwargs):
        captured["react_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(qa_agent_module, "TavilySearch", FakeTavilySearch)
    monkeypatch.setattr(qa_agent_module, "create_react_agent", fake_create_react_agent)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    monkeypatch.setenv("TAVILY_MAX_RESULTS", "7")

    TravelQuestionAnsweringAgent(
        FakeTravelVectorStore(),
        llm=FakeLLM("unused"),
        web_client=WebSearchMCPClient(command=""),
    )

    tools = captured["react_kwargs"]["tools"]
    assert len(tools) == 1
    assert tools[0].name == "tavily_search"
    assert tools[0].kwargs["max_results"] == 7


def test_travel_qa_agent_adds_amap_tools_when_configured(monkeypatch):
    from app.knowledge import qa_agent as qa_agent_module
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    captured = {}

    class FakeAmapClient:
        def search_pois(self, **kwargs):
            raise AssertionError("tool construction should not call amap search")

        def get_weather(self, **kwargs):
            raise AssertionError("tool construction should not call amap weather")

    def fake_create_react_agent(**kwargs):
        captured["react_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(qa_agent_module, "create_react_agent", fake_create_react_agent)

    TravelQuestionAnsweringAgent(
        FakeTravelVectorStore(),
        llm=FakeLLM("unused"),
        web_client=WebSearchMCPClient(command=""),
        amap_client=FakeAmapClient(),
    )

    tool_names = {tool.name for tool in captured["react_kwargs"]["tools"]}
    assert "query_weather" in tool_names
    assert "search_attractions" in tool_names


def test_travel_qa_agent_adds_rollinggo_hotel_tool_when_configured(monkeypatch):
    from app.knowledge import qa_agent as qa_agent_module
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    captured = {}

    class FakeRollingGoClient:
        available = True

        def search_hotels(self, *args, **kwargs):
            raise AssertionError("tool construction should not call RollingGo search")

    def fake_create_react_agent(**kwargs):
        captured["react_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(qa_agent_module, "create_react_agent", fake_create_react_agent)

    TravelQuestionAnsweringAgent(
        FakeTravelVectorStore(),
        llm=FakeLLM("unused"),
        web_client=WebSearchMCPClient(command=""),
        hotel_client=FakeRollingGoClient(),
    )

    tool_names = {tool.name for tool in captured["react_kwargs"]["tools"]}
    assert "search_hotels" in tool_names


def test_travel_qa_agent_uses_react_agent_when_knowledge_context_is_empty(monkeypatch):
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    class FakeTavilySearch:
        name = "tavily_search"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []

        def invoke(self, payload):
            self.calls.append(payload)
            return {
                "results": [
                    {
                        "title": "珠海天气官方预报",
                        "url": "https://example.gov.cn/zhuhai-weather",
                        "content": "珠海18号多云，有阵雨概率，请关注气象台预警。",
                        "score": 0.92,
                    }
                ]
            }

    class QAAgentRuntime:
        def __init__(self):
            self.calls = []

        def invoke(self, state, config=None):
            self.calls.append({"state": state, "config": config})
            return {
                "messages": [
                    FakeToolMessage(
                        {
                            "query": "广州夜游 官方 预约",
                            "results": [
                                {
                                    "title": "广州珠江夜游官方预约",
                                    "url": "https://example.gov.cn/pearl-river-night",
                                    "content": "珠江夜游可通过官方渠道预约，建议关注实时班次。",
                                    "score": 0.93,
                                }
                            ],
                        }
                    ),
                    FakeMessage("已联网查询到：广州夜游建议关注珠江夜游官方预约。"),
                ]
            }

    runtime = QAAgentRuntime()
    monkeypatch.setattr("app.knowledge.qa_agent.TavilySearch", FakeTavilySearch)
    monkeypatch.setattr("app.knowledge.qa_agent.create_react_agent", lambda *args, **kwargs: runtime)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    agent = TravelQuestionAnsweringAgent(
        FakeTravelVectorStore([]),
        llm=FakeLLM("chain should not be first"),
        web_client=WebSearchMCPClient(command=""),
    )
    agent.chain = SimpleNamespace(invoke=lambda payload: (_ for _ in ()).throw(AssertionError("chain should be fallback only")))

    result = agent.ask("广州夜游现在怎么预约？", config={"configurable": {"thread_id": "qa-empty-context"}})

    assert runtime.calls
    assert "联网搜索结果" in runtime.calls[0]["state"]["messages"][0]["content"]
    assert runtime.calls[0]["config"]["configurable"]["thread_id"] == "qa-empty-context"
    assert result.generation_mode == "llm"
    assert result.used_web_search is True
    assert result.retrieved_count >= 1
    assert result.sources[0].source == "web-official"
    assert "珠江夜游" in result.answer


def test_travel_qa_agent_prompts_web_search_when_user_explicitly_requests_it(monkeypatch):
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.knowledge.vector_store import KnowledgeDocument
    from app.researching.research import WebSearchMCPClient

    class FakeTavilySearch:
        name = "tavily_search"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []

        def invoke(self, payload):
            self.calls.append(payload)
            return {
                "results": [
                    {
                        "title": "珠海天气官方预报",
                        "url": "https://example.gov.cn/zhuhai-weather",
                        "content": "珠海18号多云，有阵雨概率，请关注气象台预警。",
                        "score": 0.92,
                    }
                ]
            }

    class QAAgentRuntime:
        def __init__(self):
            self.calls = []

        def invoke(self, state, config=None):
            self.calls.append({"state": state, "config": config})
            return {"messages": [FakeMessage("珠海18号天气建议以气象台最新预报为准。")]}

    doc = KnowledgeDocument(
        id="doc-1",
        title="无关铁路公告",
        content="铁路部门近期加开部分旅客列车，与珠海天气无关。",
        summary="铁路加开公告。",
        source_url="https://example.test/railway",
        source_name="rss",
        published_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        score=0.91,
    )
    runtime = QAAgentRuntime()
    monkeypatch.setattr("app.knowledge.qa_agent.TavilySearch", FakeTavilySearch)
    monkeypatch.setattr("app.knowledge.qa_agent.create_react_agent", lambda *args, **kwargs: runtime)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    agent = TravelQuestionAnsweringAgent(
        FakeTravelVectorStore([doc]),
        llm=FakeLLM("chain should not be first"),
        web_client=WebSearchMCPClient(command=""),
    )

    result = agent.ask("请联网查询一下珠海18号的天气情况")

    prompt = runtime.calls[0]["state"]["messages"][0]["content"]
    assert "用户明确要求联网或问题涉及时效信息" in prompt
    assert "必须先获取联网搜索结果" in prompt
    assert "不要在最终回答中暴露工具名" in prompt
    assert result.used_web_search is True
    assert result.sources[0].title == "珠海天气官方预报"


def test_travel_qa_agent_stream_includes_tavily_sources_from_tool_messages(monkeypatch):
    from app.knowledge.qa_agent import TravelQuestionAnsweringAgent
    from app.researching.research import WebSearchMCPClient

    class FakeTavilySearch:
        name = "tavily_search"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class QAAgentRuntime:
        def stream(self, state, config=None, stream_mode=None):
            yield (
                FakeToolMessage(
                    json.dumps(
                        {
                            "results": [
                                {
                                    "title": "广州塔官方购票",
                                    "url": "https://example.gov.cn/canton-tower",
                                    "content": "广州塔夜间登塔需通过官方渠道购票预约。",
                                    "score": 0.91,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                ),
                {},
            )
            yield (FakeMessage("广州夜景建议优先预约广州塔和珠江夜游。"), {})

    monkeypatch.setattr("app.knowledge.qa_agent.TavilySearch", FakeTavilySearch)
    monkeypatch.setattr("app.knowledge.qa_agent.create_react_agent", lambda *args, **kwargs: QAAgentRuntime())
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    agent = TravelQuestionAnsweringAgent(
        FakeTravelVectorStore([]),
        llm=FakeLLM("chain should not be first"),
        web_client=WebSearchMCPClient(command=""),
    )

    events = list(agent.stream("广州夜景怎么预约？", config={"configurable": {"thread_id": "qa-stream-web"}}))

    deltas = [event["data"]["content"] for event in events if event["event"] == "answer_delta"]
    done = next(event["data"] for event in events if event["event"] == "done")
    assert deltas == ["广州夜景建议优先预约广州塔和珠江夜游。"]
    assert done.used_web_search is True
    assert done.retrieved_count == 1
    assert done.sources[0].title == "广州塔官方购票"
    assert done.sources[0].source == "web-official"


def test_settings_support_reference_env_names(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ID", "qwen3.6-plus")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174")
    monkeypatch.setenv("AMAP_API_KEY", "amap-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("TAVILY_MAX_RESULTS", "8")

    settings = get_settings()

    assert settings.llm_model_id == "qwen3.6-plus"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.llm_timeout == 45
    assert settings.llm_connect_timeout == 7
    assert settings.llm_max_retries == 2
    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:5174"]
    assert settings.amap_api_key == "amap-key"
    assert settings.tavily_api_key == "tavily-key"
    assert settings.tavily_max_results == 8
    assert settings.has_llm_credentials is True


def test_settings_normalizes_cors_origins_to_browser_origins(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://xiaozhaoben.github.io/travel_assistant/,https://xiao-zhao.top/api",
    )

    settings = get_settings()

    assert settings.cors_origins == ["https://xiaozhaoben.github.io", "https://xiao-zhao.top"]


def test_backend_paths_stay_at_backend_root_after_package_split():
    from app.core.config import BACKEND_DIR, ENV_PATH
    from app.researching.research import DestinationResearchService

    assert BACKEND_DIR.name == "backend"
    assert (BACKEND_DIR / "app").is_dir()
    assert ENV_PATH == BACKEND_DIR / ".env"
    assert DestinationResearchService().cache_path.parent == BACKEND_DIR / "runtime"


def test_settings_builds_database_url_from_postgres_parts(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.test")
    monkeypatch.setenv("POSTGRES_PORT", "15432")
    monkeypatch.setenv("POSTGRES_DB", "travel")
    monkeypatch.setenv("POSTGRES_USER", "travel")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    settings = get_settings()

    assert settings.database_url == "postgresql://travel:secret@db.example.test:15432/travel"


def test_database_connection_manager_uses_pool_and_closes_it():
    from app.storage.db import DatabaseConnectionManager

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

    class FakePool:
        instances = []

        def __init__(self, conninfo, min_size, max_size, open):
            self.conninfo = conninfo
            self.min_size = min_size
            self.max_size = max_size
            self.open = open
            self.connection_calls = 0
            self.closed = False
            FakePool.instances.append(self)

        def connection(self):
            self.connection_calls += 1
            return FakeConnection()

        def close(self):
            self.closed = True

    manager = DatabaseConnectionManager(
        "postgresql://example/db",
        pool_factory=FakePool,
        min_size=1,
        max_size=3,
    )

    with manager.connection() as conn:
        assert isinstance(conn, FakeConnection)
    manager.close()

    assert FakePool.instances[0].conninfo == "postgresql://example/db"
    assert FakePool.instances[0].connection_calls == 1
    assert FakePool.instances[0].closed is True


def test_settings_support_provider_disable_flags(monkeypatch):
    monkeypatch.setenv("DISABLE_LLM", "true")
    monkeypatch.setenv("DISABLE_EXTERNAL_API", "1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("AMAP_API_KEY", "amap-key")
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "unsplash-key")
    monkeypatch.setenv("PEXELS_API_KEY", "pexels-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay-key")

    settings = get_settings()
    orchestrator = TravelAgentOrchestrator()

    assert settings.disable_llm is True
    assert settings.disable_external_api is True
    assert orchestrator.planner.llm is None
    assert orchestrator.amap.api_key == ""
    assert orchestrator.unsplash.access_key == ""
    assert orchestrator.unsplash.pexels_api_key == ""
    assert orchestrator.unsplash.pixabay_api_key == ""
    assert orchestrator.unsplash.enable_open_sources is False


def test_llm_settings_support_dashscope_thinking_toggle(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    monkeypatch.setenv("MCP_TIMEOUT_SECONDS", "12.5")

    settings = get_settings()

    assert settings.llm_enable_thinking is False
    assert settings.mcp_timeout_seconds == 12.5


def test_create_llm_passes_dashscope_thinking_toggle(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL_ID", "qwen3.6-plus")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")

    from app.core.llm_service import create_llm

    llm = create_llm()

    assert llm is not None
    assert llm.extra_body == {"enable_thinking": False}


def test_create_llm_uses_configurable_timeouts_and_retries(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")

    from app.core.llm_service import create_llm

    llm = create_llm()

    assert llm is not None
    assert llm.max_retries == 2
    assert llm.request_timeout.connect == 7
    assert llm.request_timeout.read == 45


def test_parser_extracts_city_days_preferences_and_budget():
    parser = TravelRequirementParser()

    requirement = parser.parse("我想去北京玩 3 天，喜欢历史文化，预算中等")

    assert requirement.city == "北京"
    assert requirement.days == 3
    assert requirement.preferences == ["历史文化"]
    assert requirement.budget_level == "中等"


def test_local_fallback_uses_destination_city_coordinates_for_zhuhai():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)

    result = orchestrator.plan(TripPlanRequest(prompt="我想去珠海玩 3 天，喜欢历史文化，预算中等"))
    plan = result.selected_plan
    attractions = [attraction for day in plan.days for attraction in day.attractions]

    assert plan.city == "珠海"
    assert 113.0 <= plan.map_center.longitude <= 114.5
    assert 21.5 <= plan.map_center.latitude <= 23.0
    assert attractions
    assert all(113.0 <= attraction.location.longitude <= 114.5 for attraction in attractions)
    assert all(21.5 <= attraction.location.latitude <= 23.0 for attraction in attractions)


def test_local_fallback_recommends_real_zhuhai_attractions():
    amap = AmapMCPClient(api_key="")

    pois = amap.search_pois("珠海", ["历史文化"], limit=6)
    names = [poi.name for poi in pois]

    assert "珠海博物馆" in names
    assert "唐家古镇" in names
    assert "梅溪牌坊旅游区" in names
    assert all("珠海城市公园" != name for name in names)
    assert all("珠海观景地" != name for name in names)


def test_local_fallback_recommends_real_guangzhou_attractions():
    amap = AmapMCPClient(api_key="")

    pois = amap.search_pois("广州", ["历史文化"], limit=6)
    names = [poi.name for poi in pois]

    assert "陈家祠" in names
    assert "沙面岛" in names
    assert "越秀公园" in names
    assert all("广州历史街区" != name for name in names)
    assert all("广州城市公园" != name for name in names)
    assert all("广州特色街区" != name for name in names)


def test_recommendation_service_prioritizes_real_attractions_over_generic_names():
    from app.integrations.services import AttractionRecommendationService

    generic = Attraction(
        id="generic",
        name="广州历史文化景点",
        category="旅游景点",
        address="广州市越秀区",
        location=Location(longitude=113.2600, latitude=23.1300),
        visit_duration_minutes=90,
        description="模板化地点",
        ticket_price=0,
        rating=4.8,
    )
    real = Attraction(
        id="real",
        name="陈家祠",
        category="历史文化;博物馆",
        address="广州市荔湾区中山七路",
        location=Location(longitude=113.2466, latitude=23.1317),
        visit_duration_minutes=120,
        description="岭南祠堂建筑代表",
        ticket_price=10,
        rating=4.6,
    )

    ranked = AttractionRecommendationService().rank(
        [generic, real],
        city="广州",
        preferences=["历史文化"],
        limit=2,
    )

    assert [item.name for item in ranked] == ["陈家祠", "广州历史文化景点"]


def test_recommendation_service_filters_avoid_places_and_boosts_must_visit():
    from app.integrations.services import AttractionRecommendationService

    chen = Attraction(
        id="chen",
        name="陈家祠",
        category="历史文化",
        address="广州市荔湾区",
        location=Location(longitude=113.2466, latitude=23.1317),
        visit_duration_minutes=120,
        description="岭南建筑",
        ticket_price=10,
        rating=4.5,
    )
    sha = Attraction(
        id="sha",
        name="沙面岛",
        category="历史文化;街区",
        address="广州市荔湾区",
        location=Location(longitude=113.2384, latitude=23.1092),
        visit_duration_minutes=120,
        description="近代建筑街区",
        ticket_price=0,
        rating=4.4,
    )
    avoided = Attraction(
        id="avoid",
        name="广州塔",
        category="城市地标",
        address="广州市海珠区",
        location=Location(longitude=113.3307, latitude=23.1066),
        visit_duration_minutes=120,
        description="城市地标",
        ticket_price=150,
        rating=4.9,
    )

    ranked = AttractionRecommendationService().rank(
        [chen, sha, avoided],
        city="广州",
        preferences=["历史文化"],
        limit=3,
        must_visit=["沙面岛"],
        avoid_places=["广州塔"],
    )

    assert [item.name for item in ranked] == ["沙面岛", "陈家祠"]


def test_orchestrator_creates_langchain_agents_with_prompt_class(monkeypatch):
    calls = []

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        calls.append(
            {
                "model": model,
                "tools": tools or [],
                "system_prompt": system_prompt,
                "name": name,
                "middleware": kwargs.get("middleware") or [],
            }
        )
        return object()

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)

    TravelAgentOrchestrator(llm=FakeLLM("{}"), disable_external_api=True)

    assert [call["name"] for call in calls] == [
        "attraction_search_agent",
        "weather_query_agent",
        "hotel_agent",
        "planner_agent",
    ]
    assert [call["system_prompt"] for call in calls] == [
        AgentPrompts.ATTRACTION_SEARCH,
        AgentPrompts.WEATHER_QUERY,
        AgentPrompts.HOTEL,
        AgentPrompts.PLANNER,
    ]
    tools_by_agent = {call["name"]: {tool.name for tool in call["tools"]} for call in calls}
    assert tools_by_agent["attraction_search_agent"] == {"search_attractions"}
    assert tools_by_agent["weather_query_agent"] == {"query_weather"}
    assert tools_by_agent["hotel_agent"] == {"search_hotels"}
    assert tools_by_agent["planner_agent"] == {"search_meals"}
    middleware_by_agent = {
        call["name"]: {getattr(item, "__name__", type(item).__name__) for item in call["middleware"]}
        for call in calls
    }
    assert middleware_by_agent["attraction_search_agent"] == {"monitor_tool", "log_before_model"}
    assert middleware_by_agent["weather_query_agent"] == {"monitor_tool", "log_before_model"}
    assert middleware_by_agent["hotel_agent"] == {"monitor_tool", "log_before_model"}
    assert middleware_by_agent["planner_agent"] == {"monitor_tool", "log_before_model"}


def test_dockerfile_copies_constraints_before_pip_install():
    dockerfile = (os.path.dirname(os.path.dirname(__file__)) + "/Dockerfile")
    with open(dockerfile, encoding="utf-8") as handle:
        content = handle.read()

    assert "COPY constraints.txt" in content
    assert content.index("COPY constraints.txt") < content.index("pip install -r requirements.txt")


def test_orchestrator_configures_structured_response_formats(monkeypatch):
    calls = []

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        calls.append({"name": name, "response_format": kwargs.get("response_format")})
        return object()

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)

    TravelAgentOrchestrator(llm=FakeLLM("{}"), disable_external_api=True)

    formats = {call["name"]: call["response_format"] for call in calls}
    assert formats["attraction_search_agent"].__name__ == "AttractionSearchOutput"
    assert formats["weather_query_agent"].__name__ == "WeatherQueryOutput"
    assert formats["hotel_agent"].__name__ == "HotelSearchOutput"
    assert formats["planner_agent"].__name__ == "PlannerLLMOutput"


def test_extract_agent_content_prefers_structured_response():
    from app.workflows.agents import AttractionSearchOutput, _extract_agent_content

    payload = AttractionSearchOutput(
        attractions=[
            Attraction(
                id="poi-1",
                name="故宫博物院",
                category="历史文化",
                address="北京市东城区景山前街4号",
                location=Location(longitude=116.397, latitude=39.916),
                visit_duration_minutes=240,
                description="明清皇家宫殿建筑群。",
                ticket_price=60,
            )
        ]
    )

    content = _extract_agent_content({"structured_response": payload, "messages": [FakeMessage("ignored")]})

    assert json.loads(content)["attractions"][0]["name"] == "故宫博物院"


def test_tool_middleware_tolerates_missing_runtime_context():
    from app.tools.middleware import log_before_model, monitor_tool

    runtime = SimpleNamespace(context=None)

    assert log_before_model.before_model({"messages": []}, runtime) is None
    assert (
        monitor_tool.wrap_tool_call(
            SimpleNamespace(tool_call={"name": "search_meals", "args": {}}, runtime=runtime),
            lambda request: "ok",
        )
        == "ok"
    )


def test_tool_middleware_tracks_errors_in_runtime_context():
    from app.tools.middleware import MAX_CONSECUTIVE_TOOL_ERRORS, log_before_model, monitor_tool

    runtime = SimpleNamespace(context={})
    request = SimpleNamespace(tool_call={"name": "search_meals", "args": {}, "id": "tool-call-1"}, runtime=runtime)

    for _ in range(MAX_CONSECUTIVE_TOOL_ERRORS - 1):
        try:
            monitor_tool.wrap_tool_call(request, lambda request: (_ for _ in ()).throw(ValueError("boom")))
        except ValueError:
            pass

    assert runtime.context["consecutive_tool_errors"]["search_meals"] == MAX_CONSECUTIVE_TOOL_ERRORS - 1
    tool_message = monitor_tool.wrap_tool_call(request, lambda request: (_ for _ in ()).throw(ValueError("boom")))
    assert "Do NOT retry" in tool_message.content
    assert runtime.context["consecutive_tool_errors"]["search_meals"] == 0

    isolated_runtime = SimpleNamespace(context={})
    assert log_before_model.before_model({"messages": []}, isolated_runtime) is None
    assert isolated_runtime.context.get("consecutive_tool_errors") is None


def test_orchestrator_uses_configured_llm_for_planner_agent(monkeypatch):
    calls = []
    configured_llm = object()

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        calls.append({"name": name, "model": model})
        return object()

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("PLANNER_MODE", "quality")
    monkeypatch.setattr("app.workflows.agents.create_llm", lambda: configured_llm)
    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)

    orchestrator = TravelAgentOrchestrator(disable_external_api=True)

    assert orchestrator.planner.llm is configured_llm
    assert [call["name"] for call in calls] == [
        "attraction_search_agent",
        "weather_query_agent",
        "hotel_agent",
        "planner_agent",
    ]
    assert all(call["model"] is configured_llm for call in calls)


def test_fast_planner_mode_skips_all_configured_llm_agents(monkeypatch):
    calls = []
    configured_llm = object()

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        calls.append({"name": name, "model": model})
        return object()

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("PLANNER_MODE", "fast")
    monkeypatch.setattr("app.workflows.agents.create_llm", lambda: configured_llm)
    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)

    orchestrator = TravelAgentOrchestrator(disable_external_api=True)

    assert orchestrator.attractions.llm is None
    assert orchestrator.weather.langchain_agent is None
    assert orchestrator.hotels.langchain_agent is None
    assert orchestrator.planner.llm is None
    assert calls == []


def test_fast_planner_mode_uses_local_amap_fallback_for_plan_generation(monkeypatch):
    monkeypatch.setenv("PLANNER_MODE", "fast")
    monkeypatch.setenv("AMAP_API_KEY", "test-amap-key")

    orchestrator = TravelAgentOrchestrator(disable_llm=True)

    assert orchestrator.amap.api_key == ""
    assert orchestrator.amap.mcp_caller is None


def test_agent_prompts_hold_tool_usage_instructions():
    assert "search_attractions" in AgentPrompts.ATTRACTION_SEARCH
    assert "query_weather" in AgentPrompts.WEATHER_QUERY
    assert "search_hotels" in AgentPrompts.HOTEL
    assert "search_meals" in AgentPrompts.PLANNER
    all_prompts = " ".join(
        [
            AgentPrompts.ATTRACTION_SEARCH,
            AgentPrompts.WEATHER_QUERY,
            AgentPrompts.HOTEL,
            AgentPrompts.PLANNER,
        ]
    )
    assert "When the" not in all_prompts
    assert "source of truth" not in all_prompts
    assert "Before final JSON" not in all_prompts


def test_agent_prompts_define_json_schema_and_few_shot_examples():
    prompts = [
        AgentPrompts.ATTRACTION_SEARCH,
        AgentPrompts.WEATHER_QUERY,
        AgentPrompts.HOTEL,
        AgentPrompts.PLANNER,
    ]

    for prompt in prompts:
        assert "JSON Schema" in prompt
        assert "Few-Shot" in prompt

    assert '"attractions"' in AgentPrompts.ATTRACTION_SEARCH
    assert '"weather"' in AgentPrompts.WEATHER_QUERY
    assert '"hotels"' in AgentPrompts.HOTEL
    assert '"meals": [' in AgentPrompts.PLANNER
    assert '"hotel": {' in AgentPrompts.PLANNER
    assert '"agent_trace": [' in AgentPrompts.PLANNER
    assert "route_points 必须是数组" in AgentPrompts.PLANNER


def test_tool_middleware_detects_prior_tool_messages():
    from app.tools.middleware import tool_call_names_from_state

    state = {
        "messages": [
            FakeMessage("user request"),
            SimpleNamespace(content="[]", type="tool", name="search_hotels"),
            {"role": "tool", "name": "search_meals", "content": "[]"},
        ]
    }

    assert tool_call_names_from_state(state) == ["search_hotels", "search_meals"]


def test_weather_query_tool_calls_amap_with_start_argument_name():
    from app.tools.amap_tools import create_weather_query_tool

    tool = create_weather_query_tool(AmapMCPClient(api_key=""))

    weather = tool.invoke({"city": "北京", "start_date": "2026-06-01", "days": 2})

    assert len(weather) == 2
    assert weather[0]["date"] == "2026-06-01"


def test_attraction_agent_uses_ai_generated_amap_queries(monkeypatch):
    runtime = FakeLangChainAgent(json.dumps({"queries": ["珠海唐家古镇", "珠海海滨公园"]}, ensure_ascii=False))
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: runtime)

    class FakeAmap:
        def __init__(self):
            self.calls = []

        def search_pois(self, city, keywords, limit=9, **kwargs):
            self.calls.append({"city": city, "keywords": list(keywords), "limit": limit, **kwargs})
            return [
                Attraction(
                    id="poi-ai",
                    name="唐家古镇",
                    category="历史文化",
                    address="珠海市香洲区唐家湾镇",
                    location=Location(longitude=113.593346, latitude=22.359661),
                    visit_duration_minutes=120,
                    description="AI 选择关键词后由高德 MCP 返回的景点。",
                    ticket_price=0,
                )
            ]

    amap = FakeAmap()
    agent = AttractionSearchAgent(amap, UnsplashMCPClient(access_key=""), llm=FakeLLM("{}"))
    requirement = TravelRequirement(
        prompt="我想去珠海玩 3 天，喜欢历史文化，预算中等",
        city="珠海",
        days=3,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 5, 21),
    )

    attractions = agent.run(requirement)

    assert amap.calls[0]["keywords"] == ["珠海唐家古镇", "珠海海滨公园"]
    assert amap.calls[0]["keywords"] != requirement.preferences
    assert amap.calls[0]["ranking_preferences"] == ["历史文化"]
    assert attractions[0].name == "唐家古镇"


def test_attraction_agent_does_not_fetch_images_during_plan_generation():
    class FakeAmap:
        def search_pois(self, city, keywords, limit=9, **kwargs):
            return [
                Attraction(
                    id="poi-1",
                    name="image-skip-attraction",
                    category="history",
                    address="test address",
                    location=Location(longitude=116.397, latitude=39.916),
                    visit_duration_minutes=120,
                    description="test description",
                    ticket_price=0,
                )
            ]

    class ForbiddenImages:
        def image_for(self, query, *args, **kwargs):
            raise AssertionError("image provider should not be called during trip plan generation")

    agent = AttractionSearchAgent(FakeAmap(), ForbiddenImages(), llm=None)
    requirement = TravelRequirement(
        prompt="image skip test",
        city="test city",
        days=1,
        preferences=["history"],
        budget_level="medium",
        start_date=date(2026, 6, 1),
    )

    attractions = agent.run(requirement)

    assert len(attractions) == 1
    assert attractions[0].image_url == ""


def test_attraction_agent_prefers_langchain_tool_result_over_direct_amap(monkeypatch):
    runtime = FakeLangChainAgent(
        json.dumps(
            {
                "attractions": [
                    {
                        "id": "tool-attraction",
                        "name": "陈家祠",
                        "category": "历史文化",
                        "address": "广州市荔湾区中山七路",
                        "location": {"longitude": 113.2466, "latitude": 23.1317},
                        "visit_duration_minutes": 120,
                        "description": "由 search_attractions 工具返回的真实 POI。",
                        "ticket_price": 10,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: runtime)

    class DirectAmapForbidden:
        def search_pois(self, *args, **kwargs):
            raise AssertionError("should prefer LangChain tool result before direct amap.search_pois")

    agent = AttractionSearchAgent(DirectAmapForbidden(), UnsplashMCPClient(access_key=""), llm=FakeLLM("{}"))
    requirement = TravelRequirement(
        prompt="广州 1 天 历史文化 中等预算",
        city="广州",
        days=1,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )

    attractions = agent.run(requirement)

    assert runtime.calls
    assert attractions[0].id == "tool-attraction"
    assert attractions[0].image_url is not None


def test_weather_agent_prefers_langchain_tool_result_over_direct_amap(monkeypatch):
    runtime = FakeLangChainAgent(
        json.dumps(
            {
                "weather": [
                    {
                        "date": "2026-06-01",
                        "day_weather": "晴",
                        "night_weather": "多云",
                        "day_temp": 28,
                        "night_temp": 22,
                        "wind": "东风1-3级",
                        "suggestion": "适合户外游览。",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: runtime)

    class DirectAmapForbidden:
        def get_weather(self, *args, **kwargs):
            raise AssertionError("should prefer LangChain tool result before direct amap.get_weather")

    agent = WeatherQueryAgent(DirectAmapForbidden(), llm=FakeLLM("{}"))
    requirement = TravelRequirement(
        prompt="广州 1 天 历史文化 中等预算",
        city="广州",
        days=1,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )

    weather = agent.run(requirement)

    assert runtime.calls
    assert weather[0].day_weather == "晴"


def test_hotel_agent_prefers_langchain_tool_result_over_direct_amap(monkeypatch):
    runtime = FakeLangChainAgent(
        json.dumps(
            {
                "hotels": [
                    {
                        "id": "tool-hotel",
                        "name": "城央精选酒店",
                        "address": "广州市越秀区",
                        "location": {"longitude": 113.2644, "latitude": 23.1291},
                        "type": "中等型酒店",
                        "rating": 4.6,
                        "nightly_price": 520,
                        "description": "由 search_hotels 工具返回的酒店。",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: runtime)

    class DirectAmapForbidden:
        def search_hotels(self, *args, **kwargs):
            raise AssertionError("should prefer LangChain tool result before direct amap.search_hotels")

    agent = HotelAgent(DirectAmapForbidden(), llm=FakeLLM("{}"))
    requirement = TravelRequirement(
        prompt="广州 1 天 历史文化 中等预算",
        city="广州",
        days=1,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )

    hotels = agent.run(requirement)

    assert runtime.calls
    assert hotels[0].id == "tool-hotel"


def test_attraction_agent_filters_ai_generic_category_queries(monkeypatch):
    class FakeAmap:
        def __init__(self):
            self.calls = []

        def search_pois(self, city, keywords, limit=9, **kwargs):
            self.calls.append({"city": city, "keywords": list(keywords), "limit": limit, **kwargs})
            return []

    runtime = FakeLangChainAgent(
        json.dumps(
            {
                "queries": [
                    "广州历史街区",
                    "广州城市公园",
                    "广州特色街区",
                    "陈家祠",
                    "沙面岛",
                    "南越王博物院",
                ]
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: runtime)
    amap = FakeAmap()
    agent = AttractionSearchAgent(amap, UnsplashMCPClient(access_key=""), llm=FakeLLM("{}"))
    requirement = TravelRequirement(
        prompt="我想去广州玩 3 天，喜欢历史文化，预算中等",
        city="广州",
        days=3,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 5, 21),
    )

    agent.run(requirement)

    assert amap.calls[0]["keywords"] == ["广州陈家祠", "广州沙面岛", "广州南越王博物院"]
    assert amap.calls[0]["ranking_preferences"] == ["历史文化"]


def test_attraction_agent_fallback_queries_prefer_real_seed_pois():
    class FakeAmap(AmapMCPClient):
        def __init__(self):
            super().__init__(api_key="")
            self.calls = []

        def search_pois(self, city, keywords, limit=9, **kwargs):
            self.calls.append({"city": city, "keywords": list(keywords), "limit": limit, **kwargs})
            return []

    amap = FakeAmap()
    agent = AttractionSearchAgent(amap, UnsplashMCPClient(access_key=""), llm=None)
    requirement = TravelRequirement(
        prompt="我想去广州玩 3 天，喜欢历史文化，预算中等",
        city="广州",
        days=3,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 5, 21),
    )

    agent.run(requirement)
    queries = amap.calls[0]["keywords"]

    assert queries[:5] == ["广州陈家祠", "广州沙面岛", "广州越秀公园", "广州南越王博物院", "广州广东省博物馆"]
    assert amap.calls[0]["ranking_preferences"] == ["历史文化"]
    assert "广州历史街区" not in queries
    assert "广州城市公园" not in queries
    assert "广州特色街区" not in queries


def test_amap_client_uses_mcp_for_poi_search_and_detail():
    caller = FakeMCPCaller(
        [
            {
                "pois": [
                    {
                        "id": "B000A",
                        "name": "测试景点",
                        "address": "测试地址",
                        "typecode": "110200",
                    }
                ],
            },
            {
                "id": "B000A",
                "name": "测试景点",
                "type": "风景名胜",
                "address": "测试地址",
                "location": "116.40,39.90",
                "rating": "4.8",
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("北京", ["北京中轴线景点"], limit=1)

    assert caller.calls[0] == {
        "tool_name": "maps_text_search",
        "arguments": {"keywords": "北京中轴线景点", "city": "北京"},
    }
    assert caller.calls[1] == {
        "tool_name": "maps_search_detail",
        "arguments": {"id": "B000A"},
    }
    assert pois[0].name == "测试景点"
    assert pois[0].location.longitude == 116.40
    assert pois[0].ticket_price > 0


def test_amap_client_uses_city_hotel_keywords_for_hotel_search():
    caller = FakeMCPCaller(
        [
            {"pois": []},
            {
                "pois": [
                    {
                        "id": "H0001",
                        "name": "珠海中海铂尔曼酒店",
                        "type": "住宿服务;宾馆酒店;五星级宾馆",
                        "address": "九洲大道西2029号",
                        "location": "113.541746,22.237681",
                        "rating": "4.7",
                    }
                ]
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    hotels = amap.search_hotels("珠海", "中等", limit=1)

    assert hotels[0].name == "珠海中海铂尔曼酒店"
    assert caller.calls[0]["arguments"]["keywords"] == "珠海舒适型酒店"
    assert caller.calls[1]["arguments"]["keywords"] == "珠海酒店"


def test_rollinggo_hotel_client_searches_hotels_with_realtime_price():
    from app.integrations.services import RollingGoHotelMCPClient

    caller = FakeMCPCaller(
        [
            {
                "hotelInformationList": [
                    {
                        "hotelId": 43615,
                        "name": "北京天伦王朝酒店",
                        "address": "王府井大街50号",
                        "latitude": 39.917748,
                        "longitude": 116.412249,
                        "starRating": 5.0,
                        "price": {
                            "hasPrice": True,
                            "currency": "CNY",
                            "lowestPrice": 626.0,
                        },
                        "bookingUrl": "https://rollinggo.cn/hotel/43615",
                        "tags": ["近商圈", "免费 WiFi"],
                    }
                ]
            }
        ]
    )
    client = RollingGoHotelMCPClient(api_key="mcp_test", mcp_caller=caller)

    hotels = client.search_hotels(
        origin_query="北京王府井附近酒店多少钱",
        place="北京王府井",
        check_in_date="2026-06-20",
        stay_nights=1,
        adult_count=2,
        max_price_per_night=800,
        limit=3,
    )

    assert caller.calls[0]["tool_name"] == "searchHotels"
    assert caller.calls[0]["arguments"]["place"] == "北京王府井"
    assert caller.calls[0]["arguments"]["checkInParam"]["checkInDate"] == "2026-06-20"
    assert caller.calls[0]["arguments"]["hotelTags"]["maxPricePerNight"] == 800
    assert hotels[0]["name"] == "北京天伦王朝酒店"
    assert hotels[0]["lowest_price"] == 626
    assert hotels[0]["currency"] == "CNY"
    assert hotels[0]["booking_url"] == "https://rollinggo.cn/hotel/43615"


def test_plan_log_recorder_captures_api_calls_and_redacts_keys():
    http = FakeHttpClient([{"status": "1", "pois": []}])
    amap = AmapMCPClient(api_key="secret-amap-key", mcp_caller=None, http_client=http)

    with PlanLogRecorder() as logs:
        amap._amap_place_text("北京早餐", "北京")

    assert len(logs.entries) == 1
    entry = logs.entries[0]
    assert entry.event_type == "api_call"
    assert entry.component == "amap_http"
    assert entry.operation == "place_text"
    assert entry.request_payload["params"]["key"] == "***REDACTED***"
    assert entry.response_payload == {"status": "1", "pois": []}


def test_plan_log_recorder_captures_llm_input_and_output(monkeypatch):
    monkeypatch.setattr("app.workflows.agents.create_agent", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unsupported model")))

    class EmptyAmap:
        def search_pois(self, city, keywords, limit=9, **kwargs):
            return []

    agent = AttractionSearchAgent(
        EmptyAmap(),
        UnsplashMCPClient(access_key="", pexels_api_key="", pixabay_api_key="", enable_open_sources=False),
        llm=FakeLLM(json.dumps({"queries": ["北京故宫博物院"]}, ensure_ascii=False)),
    )
    requirement = TravelRequirement(
        prompt="我想去北京玩 1 天，喜欢历史文化",
        city="北京",
        days=1,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )

    with PlanLogRecorder() as logs:
        agent.run(requirement)

    llm_logs = [entry for entry in logs.entries if entry.event_type == "llm_call"]
    assert llm_logs
    assert llm_logs[0].component == "AttractionSearchAgent"
    assert "北京" in llm_logs[0].request_payload["prompt"]
    assert "北京故宫博物院" in llm_logs[0].response_payload["content"]


def test_amap_client_skips_detail_when_text_search_has_location():
    caller = FakeMCPCaller(
        [
            {
                "pois": [
                    {
                        "id": "B000B",
                        "name": "已有坐标景点",
                        "address": "测试地址",
                        "type": "风景名胜",
                        "location": "116.41,39.91",
                    }
                ],
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("北京", ["北京公园"], limit=1)

    assert len(caller.calls) == 1
    assert pois[0].name == "已有坐标景点"
    assert pois[0].location.latitude == 39.91


def test_amap_client_retries_poi_search_with_specific_keywords_when_first_query_empty():
    caller = FakeMCPCaller(
        [
            {"pois": []},
            {
                "pois": [
                    {
                        "id": "B02F40Q3B9",
                        "name": "珠海博物馆",
                        "address": "海虹路88号",
                        "typecode": "140100",
                    }
                ],
            },
            {
                "id": "B02F40Q3B9",
                "name": "珠海博物馆",
                "type": "科教文化服务;博物馆;博物馆",
                "address": "海虹路88号",
                "location": "113.576561,22.292980",
                "rating": "4.7",
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("珠海", ["珠海历史文化景点", "珠海博物馆"], limit=1)

    assert caller.calls[0]["arguments"] == {"keywords": "珠海历史文化景点", "city": "珠海"}
    assert caller.calls[1]["arguments"] == {"keywords": "珠海博物馆", "city": "珠海"}
    assert pois[0].name == "珠海博物馆"
    assert pois[0].location.longitude == 113.576561


def test_amap_client_prefixes_city_for_generic_poi_keyword():
    caller = FakeMCPCaller([{"pois": []}])
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    amap.search_pois("珠海", ["历史文化"], limit=1)

    assert caller.calls[0]["arguments"] == {"keywords": "珠海历史文化", "city": "珠海"}


def test_amap_client_ranks_specific_relevant_pois_ahead_of_generic_high_rating_results():
    class RankingMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            return {
                "pois": [
                    {
                        "id": "generic",
                        "name": "广州历史文化景点",
                        "type": "旅游景点",
                        "address": "广州市越秀区",
                        "location": "113.2600,23.1300",
                        "biz_ext": {"rating": "4.9"},
                    },
                    {
                        "id": "chen",
                        "name": "陈家祠",
                        "type": "科教文化服务;博物馆",
                        "address": "广州市荔湾区中山七路",
                        "location": "113.2466,23.1317",
                        "biz_ext": {"rating": "4.6"},
                    },
                ]
            }

    amap = AmapMCPClient(api_key="amap-key", mcp_caller=RankingMCPCaller())

    pois = amap.search_pois("广州", ["广州历史文化景点"], limit=2)

    assert [poi.name for poi in pois] == ["陈家祠", "广州历史文化景点"]


def test_amap_client_uses_original_preferences_for_recommendation_ranking():
    class PreferenceAwareMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            return {
                "pois": [
                    {
                        "id": "tower",
                        "name": "广州塔",
                        "type": "风景名胜;城市地标",
                        "address": "广州市海珠区阅江西路",
                        "location": "113.3307,23.1066",
                        "biz_ext": {"rating": "4.9"},
                    },
                    {
                        "id": "children",
                        "name": "广州儿童公园",
                        "type": "风景名胜;公园广场;公园",
                        "address": "广州市白云区齐心路",
                        "location": "113.2730,23.1840",
                        "biz_ext": {"rating": "4.2"},
                    },
                ]
            }

    amap = AmapMCPClient(api_key="amap-key", mcp_caller=PreferenceAwareMCPCaller())

    pois = amap.search_pois("广州", ["广州亲子景点"], limit=2, ranking_preferences=["亲子"])

    assert [poi.name for poi in pois] == ["广州儿童公园", "广州塔"]


def test_amap_client_batches_many_poi_queries_to_avoid_slow_mcp_startups():
    class ManyResultsMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            query_index = int(arguments["keywords"].rsplit("-", 1)[1])
            return {
                "pois": [
                    {
                        "id": f"poi-{query_index}-{index}",
                        "name": f"测试景点{query_index}-{index}",
                        "type": "风景名胜;风景名胜;风景名胜",
                        "address": "测试地址",
                        "location": f"{116 + query_index * 0.1 + index * 0.01},39.{query_index}{index}",
                    }
                    for index in range(3)
                ]
            }

    caller = ManyResultsMCPCaller()
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("北京", [f"北京景点-{index}" for index in range(12)], limit=9)
    text_search_calls = [call for call in caller.calls if call["tool_name"] == "maps_text_search"]

    assert len(text_search_calls) <= 4
    assert len(pois) == 9


def test_amap_client_diversifies_specific_poi_queries():
    class QueryAwareMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            if tool_name == "maps_search_detail":
                details = {
                    "museum-1": {
                        "id": "museum-1",
                        "name": "珠海博物馆",
                        "type": "科教文化服务;博物馆;博物馆",
                        "address": "海虹路88号",
                        "location": "113.576561,22.292980",
                    },
                    "museum-2": {
                        "id": "museum-2",
                        "name": "珠海规划展览馆",
                        "type": "科教文化服务;展览馆;展览馆",
                        "address": "海虹路88号",
                        "location": "113.576861,22.293380",
                    },
                    "town-1": {
                        "id": "town-1",
                        "name": "唐家古镇",
                        "type": "风景名胜;风景名胜;风景名胜",
                        "address": "唐家湾镇",
                        "location": "113.593346,22.359661",
                    },
                }
                return details[arguments["id"]]
            keyword = arguments["keywords"]
            if keyword == "珠海历史文化景点":
                return {"pois": []}
            if keyword == "珠海博物馆":
                return {
                    "pois": [
                        {"id": "museum-1", "name": "珠海博物馆", "address": "海虹路88号", "typecode": "140100"},
                        {"id": "museum-2", "name": "珠海规划展览馆", "address": "海虹路88号", "typecode": "140100"},
                    ]
                }
            if keyword == "唐家湾古镇":
                return {"pois": [{"id": "town-1", "name": "唐家古镇", "address": "唐家湾镇", "typecode": "110000"}]}
            return {"pois": []}

    caller = QueryAwareMCPCaller()
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    pois = amap.search_pois("珠海", ["珠海历史文化景点", "珠海博物馆", "唐家湾古镇"], limit=2)
    names = [poi.name for poi in pois]

    assert names == ["珠海博物馆", "唐家古镇"]


def test_amap_client_limits_generic_query_and_keeps_later_landmarks():
    class ClusteredMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            keyword = arguments["keywords"]
            if keyword == "北京历史文化景点":
                return {
                    "pois": [
                        {
                            "id": "forbidden-city",
                            "name": "故宫博物院",
                            "type": "风景名胜;风景名胜;世界遗产",
                            "address": "景山前街4号",
                            "location": "116.397029,39.917839",
                        },
                        {
                            "id": "ticket",
                            "name": "故宫博物院检票处",
                            "type": "生活服务;生活服务场所;生活服务场所",
                            "address": "景山前街4号",
                            "location": "116.396952,39.913619",
                        },
                        {
                            "id": "wumen",
                            "name": "故宫博物院-午门",
                            "type": "风景名胜;风景名胜;风景名胜",
                            "address": "故宫博物院内",
                            "location": "116.397228,39.913582",
                        },
                    ]
                }
            if keyword == "北京颐和园":
                return {
                    "pois": [
                        {
                            "id": "summer-palace",
                            "name": "颐和园",
                            "type": "风景名胜;风景名胜;世界遗产",
                            "address": "新建宫门路19号",
                            "location": "116.275525,39.999575",
                        }
                    ]
                }
            if keyword == "北京天坛公园":
                return {
                    "pois": [
                        {
                            "id": "tiantan",
                            "name": "天坛公园",
                            "type": "风景名胜;公园广场;公园",
                            "address": "天坛东里甲1号",
                            "location": "116.410886,39.881949",
                        }
                    ]
                }
            return {"pois": []}

    amap = AmapMCPClient(api_key="amap-key", mcp_caller=ClusteredMCPCaller())

    pois = amap.search_pois("北京", ["北京历史文化景点", "北京颐和园", "北京天坛公园"], limit=3)
    names = [poi.name for poi in pois]

    assert names == ["故宫博物院", "颐和园", "天坛公园"]
    assert "故宫博物院检票处" not in names
    assert "故宫博物院-午门" not in names


def test_amap_client_filters_non_attraction_and_out_of_city_pois():
    class NoisyMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            if tool_name == "maps_search_detail":
                details = {
                    "snack": {
                        "id": "snack",
                        "name": "赵一鸣零食(广东珠海唐家古镇店)",
                        "type": "购物服务;便民商店/便利店;便民商店/便利店",
                        "address": "唐家湾镇",
                        "location": "113.596176,22.357680",
                        "city": "珠海市",
                    },
                    "town": {
                        "id": "town",
                        "name": "唐家古镇",
                        "type": "风景名胜;风景名胜;风景名胜",
                        "address": "唐家湾镇",
                        "location": "113.593346,22.359661",
                        "city": "珠海市",
                    },
                    "other-city": {
                        "id": "other-city",
                        "name": "接霞庄",
                        "type": "风景名胜;风景名胜;风景名胜",
                        "address": "江门市新会区",
                        "location": "113.190807,22.242473",
                        "city": "江门市",
                    },
                }
                return details[arguments["id"]]
            keyword = arguments["keywords"]
            if keyword == "珠海历史文化景点":
                return {"pois": []}
            if keyword == "珠海博物馆":
                return {"pois": []}
            if keyword == "唐家湾古镇":
                return {
                    "pois": [
                        {"id": "snack", "name": "赵一鸣零食(广东珠海唐家古镇店)", "address": "唐家湾镇"},
                        {"id": "town", "name": "唐家古镇", "address": "唐家湾镇"},
                    ]
                }
            if keyword == "梅溪牌坊":
                return {"pois": [{"id": "other-city", "name": "接霞庄", "address": "江门市新会区"}]}
            return {"pois": []}

    amap = AmapMCPClient(api_key="amap-key", mcp_caller=NoisyMCPCaller())

    pois = amap.search_pois("珠海", ["珠海历史文化景点", "珠海博物馆", "唐家湾古镇", "梅溪牌坊"], limit=2)
    names = [poi.name for poi in pois]

    assert names[0] == "唐家古镇"
    assert "赵一鸣零食(广东珠海唐家古镇店)" not in names
    assert "接霞庄" not in names


def test_amap_client_uses_mcp_for_restaurant_meals_near_day_route():
    caller = FakeMCPCaller(
        [
            {
                "pois": [
                    {
                        "id": "breakfast-1",
                        "name": "珠海老字号早茶",
                        "address": "情侣中路1号",
                        "type": "餐饮服务;中餐厅;广东菜",
                        "location": "113.5760,22.2920",
                        "biz_ext": {"rating": "4.7"},
                    }
                ],
            },
            {
                "pois": [
                    {
                        "id": "lunch-1",
                        "name": "海湾素食馆",
                        "address": "海虹路88号",
                        "type": "餐饮服务;中餐厅;素食",
                        "location": "113.5770,22.2930",
                        "biz_ext": {"rating": "4.8"},
                    }
                ],
            },
            {
                "pois": [
                    {
                        "id": "dinner-1",
                        "name": "珠海本地海鲜小馆",
                        "address": "唐家湾镇",
                        "type": "餐饮服务;中餐厅;海鲜",
                        "location": "113.5960,22.3580",
                        "biz_ext": {"rating": "4.6"},
                    }
                ],
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    meals = amap.search_meals(
        city="珠海",
        budget_level="中等",
        food_preferences="素食",
        route_points=[Location(longitude=113.576561, latitude=22.292980)],
    )

    assert [meal.type for meal in meals] == ["breakfast", "lunch", "dinner"]
    assert meals[0].name == "珠海老字号早茶"
    assert meals[0].id == "breakfast-1"
    assert meals[0].category == "餐饮服务;中餐厅;广东菜"
    assert meals[0].rating == 4.7
    assert meals[0].location.longitude == 113.5760
    assert meals[1].name == "海湾素食馆"
    assert "素食" in caller.calls[1]["arguments"]["keywords"]
    assert all(call["tool_name"] == "maps_text_search" for call in caller.calls)


def test_amap_client_prefers_stdio_mcp_over_http_for_restaurant_meals():
    class FakeStdioMCPCaller(AmapStdioMCPToolCaller):
        def __init__(self):
            super().__init__("amap-key")
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            return {
                "pois": [
                    {
                        "id": "mcp-breakfast",
                        "name": "\u73e0\u6d77\u65e9\u8336",
                        "address": "\u60c5\u4fa3\u4e2d\u8def",
                        "type": "\u9910\u996e\u670d\u52a1;\u4e2d\u9910\u5385;\u5e7f\u4e1c\u83dc",
                        "location": "113.5760,22.2920",
                        "cityname": "\u73e0\u6d77\u5e02",
                    }
                ]
            }

    class HttpForbidden:
        def __init__(self):
            self.calls = []

        def get(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError("餐饮搜索应先走高德 MCP，不能先请求 HTTP")

        def close(self):
            return None

    caller = FakeStdioMCPCaller()
    http = HttpForbidden()
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller, http_client=http)

    meals = amap.search_meals("\u73e0\u6d77", "\u4e2d\u7b49")

    assert meals[0].id == "mcp-breakfast"
    assert caller.calls[0]["tool_name"] == "maps_text_search"
    assert http.calls == []


def test_amap_client_falls_back_to_local_city_meals_without_mcp():
    amap = AmapMCPClient(api_key="")

    meals = amap.search_meals("珠海", "中等", food_preferences="素食")

    assert [meal.type for meal in meals] == ["breakfast", "lunch", "dinner"]
    assert meals[0].name == "新海利海鲜餐厅"
    assert meals[1].estimated_cost == 70
    assert meals[0].id == "local-zhuhai-breakfast-xinhaili"


def test_amap_client_falls_back_to_template_meals_for_unknown_city_without_mcp():
    amap = AmapMCPClient(api_key="")

    meals = amap.search_meals("北京", "中等", food_preferences="素食")

    assert [meal.type for meal in meals] == ["breakfast", "lunch", "dinner"]
    assert meals[0].name == "北京胡同早餐"
    assert meals[0].location is None


def test_amap_client_uses_http_restaurant_search_when_mcp_unavailable():
    class MissingMCPCaller:
        def call_tool(self, tool_name, arguments):
            raise FileNotFoundError("uvx")

    client = FakeHttpClient(
        [
            {
                "status": "1",
                "pois": [
                    {
                        "id": "http-breakfast",
                        "name": "\u73e0\u6d77\u8001\u5b57\u53f7\u65e9\u8336",
                        "address": "\u60c5\u4fa3\u4e2d\u8def1\u53f7",
                        "type": "\u9910\u996e\u670d\u52a1;\u4e2d\u9910\u5385;\u5e7f\u4e1c\u83dc",
                        "location": "113.5760,22.2920",
                        "cityname": "\u73e0\u6d77\u5e02",
                        "biz_ext": {"rating": "4.7"},
                    }
                ],
            },
            {
                "status": "1",
                "pois": [
                    {
                        "id": "http-lunch",
                        "name": "\u73e0\u6d77\u6d77\u9c9c\u996d\u5e97",
                        "address": "\u6d77\u8679\u8def8\u53f7",
                        "type": "\u9910\u996e\u670d\u52a1;\u4e2d\u9910\u5385;\u6d77\u9c9c",
                        "location": "113.5770,22.2930",
                        "cityname": "\u73e0\u6d77\u5e02",
                    }
                ],
            },
            {
                "status": "1",
                "pois": [
                    {
                        "id": "http-dinner",
                        "name": "\u5510\u5bb6\u6e7e\u98ce\u5473\u9910\u5385",
                        "address": "\u5510\u5bb6\u6e7e\u9547",
                        "type": "\u9910\u996e\u670d\u52a1;\u4e2d\u9910\u5385;\u672c\u5730\u83dc",
                        "location": "113.5960,22.3580",
                        "cityname": "\u73e0\u6d77\u5e02",
                    }
                ],
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=MissingMCPCaller(), http_client=client)

    meals = amap.search_meals(
        "\u73e0\u6d77",
        "\u4e2d\u7b49",
        route_points=[{"longitude": 113.576561, "latitude": 22.292980}],
    )

    assert [meal.id for meal in meals] == ["http-breakfast", "http-lunch", "http-dinner"]
    assert meals[0].name == "\u73e0\u6d77\u8001\u5b57\u53f7\u65e9\u8336"
    assert meals[0].location.longitude == 113.5760
    assert all(call["url"] == "https://restapi.amap.com/v3/place/text" for call in client.calls)
    assert client.calls[0]["params"]["keywords"] == "\u73e0\u6d77\u65e9\u9910"


def test_amap_http_restaurant_search_waits_and_retries_after_qps_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.integrations.services.sleep", lambda seconds: sleeps.append(seconds))

    client = FakeHttpClient(
        [
            {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
            {
                "status": "1",
                "pois": [
                    {
                        "id": "retry-breakfast",
                        "name": "\u73e0\u6d77\u65e9\u8336\u9910\u5385",
                        "address": "\u60c5\u4fa3\u4e2d\u8def",
                        "type": "\u9910\u996e\u670d\u52a1;\u4e2d\u9910\u5385;\u5e7f\u4e1c\u83dc",
                        "location": "113.5760,22.2920",
                        "cityname": "\u73e0\u6d77\u5e02",
                    }
                ],
            },
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=None, http_client=client)

    data = amap._amap_place_text("\u73e0\u6d77\u65e9\u9910", "\u73e0\u6d77")

    assert data["pois"][0]["id"] == "retry-breakfast"
    assert len(client.calls) == 2
    assert sleeps == [1.0]


def test_amap_http_restaurant_search_does_not_retry_non_qps_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.integrations.services.sleep", lambda seconds: sleeps.append(seconds))
    client = FakeHttpClient([{"status": "0", "info": "USERKEY_PLAT_NOMATCH", "infocode": "10009"}])
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=None, http_client=client)

    try:
        amap._amap_place_text("\u73e0\u6d77\u65e9\u9910", "\u73e0\u6d77")
    except RuntimeError as exc:
        assert "USERKEY_PLAT_NOMATCH" in str(exc)
    else:
        raise AssertionError("expected non-QPS Amap error to be raised")

    assert len(client.calls) == 1
    assert sleeps == []


def test_amap_stdio_mcp_caller_finds_winget_uvx_when_path_is_stale(monkeypatch):
    expected = r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\uvx.exe"
    monkeypatch.setattr("app.integrations.services.shutil.which", lambda command: None)
    monkeypatch.setattr("app.integrations.services.os.getenv", lambda key, default=None: r"C:\Users\Administrator\AppData\Local" if key == "LOCALAPPDATA" else default)
    monkeypatch.setattr("app.integrations.services.os.path.exists", lambda path: path == expected)

    caller = AmapMCPClient(api_key="", mcp_caller=None)
    resolver = AmapStdioMCPToolCaller("key")

    assert resolver._resolve_uvx_command() == expected
    assert caller.mcp_caller is None


def test_amap_client_prefers_restaurants_near_day_route_over_slightly_higher_rating():
    caller = FakeMCPCaller(
        [
            {
                "pois": [
                    {
                        "id": "far-breakfast",
                        "name": "远处高分早茶",
                        "address": "远处商圈",
                        "type": "餐饮服务;中餐厅;广东菜",
                        "location": "113.9000,22.7000",
                        "biz_ext": {"rating": "4.9"},
                    },
                    {
                        "id": "near-breakfast",
                        "name": "路线附近早茶",
                        "address": "海虹路88号",
                        "type": "餐饮服务;中餐厅;广东菜",
                        "location": "113.5768,22.2931",
                        "biz_ext": {"rating": "4.4"},
                    },
                ]
            },
            {"pois": []},
            {"pois": []},
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    meals = amap.search_meals(
        "珠海",
        "中等",
        route_points=[Location(longitude=113.576561, latitude=22.292980)],
    )

    assert meals[0].name == "路线附近早茶"


def test_amap_client_uses_mcp_for_weather():
    caller = FakeMCPCaller(
        [
            {
                "city": "北京市",
                "forecasts": [
                    {
                        "date": "2026-05-20",
                        "dayweather": "晴",
                        "nightweather": "多云",
                        "daytemp": "28",
                        "nighttemp": "18",
                        "daywind": "东北",
                        "daypower": "1-3",
                    }
                ],
            }
        ]
    )
    amap = AmapMCPClient(api_key="amap-key", mcp_caller=caller)

    weather = amap.get_weather("北京", date(2026, 5, 20), 1)

    assert caller.calls[0] == {
        "tool_name": "maps_weather",
        "arguments": {"city": "110000"},
    }
    assert weather[0].date == date(2026, 5, 20)
    assert weather[0].day_weather == "晴"
    assert weather[0].day_temp == 28


def test_image_client_falls_back_to_unsplash_after_open_sources():
    client = FakeHttpClient(
        [
            {"query": {"pages": {}}},
            {"results": []},
            {
                "results": [
                    {
                        "urls": {
                            "regular": "https://images.example/photo.jpg",
                        }
                    }
                ]
            }
        ]
    )
    unsplash = UnsplashMCPClient(access_key="unsplash-key", http_client=client)

    url = unsplash.image_for("北京 故宫")

    assert "commons.wikimedia.org" in client.calls[0]["url"]
    assert "api.openverse.org" in client.calls[1]["url"]
    assert client.calls[2]["url"].endswith("/search/photos")
    assert client.calls[2]["headers"]["Authorization"] == "Client-ID unsplash-key"
    assert url == "https://images.example/photo.jpg"


def test_image_client_uses_web_search_before_open_sources():
    from app.researching.research import WebSearchMCPClient

    caller = FakeMCPCaller(
        [
            {
                "results": [
                    {
                        "title": "Forbidden City official image",
                        "url": "https://example.com/gugong",
                        "image": "https://cdn.example.com/gugong.jpg",
                    }
                ]
            }
        ]
    )
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    client = FakeHttpClient(
        [
            {"query": {"pages": {}}},
            {"results": []},
        ]
    )
    images = UnsplashMCPClient(access_key="", http_client=client, web_search_client=web, enable_llm_selector=False)

    url = images.image_for("Beijing Forbidden City")

    assert caller.calls[0] == {
        "tool_name": "web_search",
        "arguments": {"query": "Beijing Forbidden City landmark photo image"},
    }
    assert client.calls == []
    assert url == "https://cdn.example.com/gugong.jpg"


def test_image_client_requests_and_parses_tavily_image_results():
    from app.researching.research import WebSearchMCPClient

    caller = FakeMCPCaller(
        [
            (
                "Detailed Results:\n\n"
                "Title: Zhuhai Museum travel guide\n"
                "URL: https://example.com/zhuhai-museum\n"
                "Content: Museum overview.\n\n"
                "Images:\n\n"
                "[1] URL: https://cdn.example.com/zhuhai-museum-photo.jpeg\n"
                "   Description: Zhuhai Museum exterior photo.\n"
            )
        ]
    )
    web = WebSearchMCPClient(tool_name="tavily_search", mcp_caller=caller)
    images = UnsplashMCPClient(
        access_key="",
        http_client=FakeHttpClient([]),
        web_search_client=web,
        enable_llm_selector=False,
    )

    url = images.image_for("Zhuhai Museum")

    assert caller.calls[0] == {
        "tool_name": "tavily_search",
        "arguments": {
            "query": "Zhuhai Museum landmark photo image",
            "include_images": True,
            "include_image_descriptions": True,
            "max_results": 5,
        },
    }
    assert url == "https://cdn.example.com/zhuhai-museum-photo.jpeg"


def test_image_client_lets_llm_choose_web_search_image_candidate():
    from app.researching.research import WebSearchMCPClient

    caller = FakeMCPCaller(
        [
            {
                "results": [
                    {
                        "title": "Generic Beijing travel article",
                        "url": "https://example.com/beijing",
                        "image": "https://cdn.example.com/generic-city.jpg",
                        "content": "A broad Beijing travel overview.",
                    },
                    {
                        "title": "Forbidden City official visitor photo",
                        "url": "https://example.com/forbidden-city",
                        "image": "https://cdn.example.com/forbidden-city.jpg",
                        "content": "Official visitor information for the Forbidden City.",
                    },
                ]
            }
        ]
    )
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    llm = FakeLLM('{"image_url": "https://cdn.example.com/forbidden-city.jpg"}')
    images = UnsplashMCPClient(
        access_key="",
        http_client=FakeHttpClient([{"query": {"pages": {}}}, {"results": []}]),
        web_search_client=web,
        llm=llm,
    )

    url = images.image_for("Beijing Forbidden City")

    assert url == "https://cdn.example.com/forbidden-city.jpg"
    assert "Beijing Forbidden City" in llm.calls[0][0][1]
    assert "Generic Beijing travel article" in llm.calls[0][0][1]
    assert images.http_client.calls == []


def test_image_client_falls_back_to_open_sources_when_llm_rejects_web_candidates():
    from app.researching.research import WebSearchMCPClient

    caller = FakeMCPCaller(
        [
            {
                "results": [
                    {
                        "title": "Generic Beijing travel article",
                        "url": "https://example.com/beijing",
                        "image": "https://cdn.example.com/generic-city.jpg",
                    }
                ]
            }
        ]
    )
    web = WebSearchMCPClient(tool_name="web_search", mcp_caller=caller)
    llm = FakeLLM('{"image_url": ""}')
    client = FakeHttpClient(
        [
            {
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/thumb/forbidden-city.jpg",
                                }
                            ],
                        }
                    }
                }
            }
        ]
    )
    images = UnsplashMCPClient(access_key="", http_client=client, web_search_client=web, llm=llm)

    url = images.image_for("Beijing Forbidden City")

    assert "commons.wikimedia.org" in client.calls[0]["url"]
    assert url == "https://upload.wikimedia.org/thumb/forbidden-city.jpg"


def test_image_client_uses_wikimedia_before_keyed_stock_providers():
    client = FakeHttpClient(
        [
            {
                "query": {
                    "pages": [
                        {
                            "title": "File:Forbidden City Beijing.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/thumb/gugong.jpg",
                                }
                            ],
                        }
                    ]
                }
            }
        ]
    )
    images = UnsplashMCPClient(access_key="unsplash-key", pexels_api_key="pexels-key", pixabay_api_key="pixabay-key", http_client=client)

    url = images.image_for("Beijing Forbidden City")

    assert "commons.wikimedia.org" in client.calls[0]["url"]
    assert client.calls[0]["params"]["generator"] == "search"
    assert url == "https://upload.wikimedia.org/thumb/gugong.jpg"


def test_image_client_sends_wikimedia_user_agent_header(monkeypatch):
    monkeypatch.setenv("WIKIMEDIA_USER_AGENT", "travel-assistant-test/1.0 (test@example.com)")
    client = FakeHttpClient(
        [
            {
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/thumb/test.jpg",
                                }
                            ],
                        }
                    }
                }
            }
        ]
    )
    images = UnsplashMCPClient(access_key="", http_client=client)

    images.image_for("Beijing landmark")

    assert client.calls[0]["headers"]["User-Agent"] == "travel-assistant-test/1.0 (test@example.com)"
    assert client.calls[0]["headers"]["Api-User-Agent"] == "travel-assistant-test/1.0 (test@example.com)"

def test_image_client_uses_wikimedia_before_unsplash_when_only_unsplash_key_is_set():
    client = FakeHttpClient(
        [
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:Summer Palace Beijing.jpg",
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/thumb/summer-palace.jpg",
                                }
                            ],
                        }
                    }
                }
            }
        ]
    )
    images = UnsplashMCPClient(access_key="unsplash-key", http_client=client)

    url = images.image_for("Beijing Summer Palace")

    assert "commons.wikimedia.org" in client.calls[0]["url"]
    assert url == "https://upload.wikimedia.org/thumb/summer-palace.jpg"


def test_image_client_falls_back_to_pexels_when_open_sources_are_empty():
    client = FakeHttpClient(
        [
            {"query": {"pages": []}},
            {"results": []},
            {
                "photos": [
                    {
                        "src": {
                            "large": "https://images.pexels.com/photos/landmark.jpeg",
                        }
                    }
                ]
            },
        ]
    )
    images = UnsplashMCPClient(access_key="", pexels_api_key="pexels-key", pixabay_api_key="", http_client=client)

    url = images.image_for("Zhuhai landmark")

    assert "commons.wikimedia.org" in client.calls[0]["url"]
    assert "api.openverse.org" in client.calls[1]["url"]
    assert "api.pexels.com" in client.calls[2]["url"]
    assert client.calls[2]["headers"]["Authorization"] == "pexels-key"
    assert url == "https://images.pexels.com/photos/landmark.jpeg"


def test_image_client_logs_each_provider_attempt_and_fallback(caplog):
    client = FakeHttpClient(
        [
            {"query": {"pages": {}}},
            {"results": []},
        ]
    )
    images = UnsplashMCPClient(access_key="", http_client=client)

    with caplog.at_level(logging.INFO, logger="app.integrations.services"):
        url = images.image_for("珠海 唐家古镇")

    messages = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]

    assert url == ""
    assert [message["event"] for message in messages] == [
        "image_search_attempt",
        "image_search_no_result",
        "image_search_attempt",
        "image_search_no_result",
        "image_search_fallback",
    ]
    assert [message.get("provider") for message in messages[:4]] == [
        "wikimedia",
        "wikimedia",
        "openverse",
        "openverse",
    ]
    assert all(message["query"] == "珠海 唐家古镇" for message in messages)


def test_unsplash_client_fallback_avoids_network_placeholder():
    unsplash = UnsplashMCPClient(access_key="", http_client=FakeHttpClient([]))

    url = unsplash.image_for("Zhuhai Yuanming New Garden", use_api=False)

    assert url == ""


def test_orchestrator_generates_complete_plan_with_four_agent_outputs():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    request = TripPlanRequest(prompt="我想去北京玩 3 天，喜欢历史文化，预算中等")

    result = orchestrator.plan(request)
    plan = result.selected_plan

    assert result.selected_option_id == "balanced"
    assert [option.id for option in result.options] == ["balanced", "relaxed", "deep_dive"]
    assert len(result.research_context) >= 1
    assert result.clarifying_suggestions
    assert plan.city == "北京"
    assert len(plan.days) == 3
    assert len(plan.weather) == 3
    assert plan.budget.total > 0
    assert plan.agent_trace == [
        "AttractionSearchAgent",
        "WeatherQueryAgent",
        "HotelAgent",
        "PlannerAgent",
    ]
    for day in plan.days:
        assert len(day.attractions) >= 2
        assert len(day.meals) == 3
        assert day.hotel.name


def test_orchestrator_logs_each_agent_input_and_output_with_timestamp(caplog):
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)

    with caplog.at_level(logging.INFO, logger="travel_assistant.agent"):
        orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "travel_assistant.agent"
    ]
    expected_agents = [
        "AttractionSearchAgent",
        "WeatherQueryAgent",
        "HotelAgent",
        "PlannerAgent",
    ]

    for agent_name in expected_agents:
        agent_events = [event for event in events if event["agent"] == agent_name]
        assert [event["event"] for event in agent_events] == ["input", "output"]
        assert all(event["timestamp"] for event in agent_events)
        assert all("payload" in event for event in agent_events)


def test_planner_prompt_includes_research_context():
    from app.domain.models import ResearchSnippet

    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 1 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)
    research = [
        ResearchSnippet(
            source="web",
            title="故宫预约提醒",
            url="https://example.com/gugong",
            summary="故宫博物院需要提前预约。",
            keywords=["故宫", "预约"],
        )
    ]

    prompt = PlannerAgent()._build_prompt(requirement, attractions, weather, hotels, research)
    payload = json.loads(prompt.rsplit("\n", 1)[1])

    assert payload["research_context"][0]["title"] == "故宫预约提醒"
    assert "提前预约" in AgentPrompts.PLANNER


def test_planner_agent_invokes_langchain_runtime_when_model_is_available(monkeypatch):
    fake_plan = _fake_llm_plan()
    runtime = FakeLangChainAgent(json.dumps(fake_plan, ensure_ascii=False))

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        return runtime if name == "planner_agent" else object()

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    orchestrator = TravelAgentOrchestrator(llm=object(), disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert runtime.calls, "PlannerAgent should call the LangChain create_agent runtime"
    assert runtime.calls[0]["messages"][0]["role"] == "user"
    assert "search_meals" not in runtime.calls[0]["messages"][0]["content"]
    assert "search_meals" in AgentPrompts.PLANNER
    assert plan.city == "北京"
    assert plan.days[0].summary == "LLM生成的历史文化一日游"
    assert plan.budget.total == 860
    assert plan.generation_mode == "llm"


def test_planner_agent_falls_back_to_raw_llm_when_create_agent_fails(monkeypatch):
    fake_plan = _fake_llm_plan()
    llm = FakeLLM(json.dumps(fake_plan, ensure_ascii=False))

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        raise RuntimeError("unsupported model")

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    orchestrator = TravelAgentOrchestrator(llm=llm, disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert llm.calls
    assert plan.days[0].summary == fake_plan["days"][0]["summary"]
    assert plan.generation_mode == "llm"


def test_planner_agent_retries_with_error_feedback_after_schema_failure(monkeypatch, tmp_path):
    from app.workflows.reflection_memory import FailedCaseNotebook, SupervisorReflectionAgent

    fake_plan = _fake_llm_plan()
    invalid_payload = {
        "thought": "first attempt omitted plan payload",
        "selected_option_id": "balanced",
        "options": [{"id": "balanced"}],
    }
    llm = SequentialFakeLLM(
        [
            json.dumps(invalid_payload, ensure_ascii=False),
            json.dumps(fake_plan, ensure_ascii=False),
        ]
    )

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        raise RuntimeError("unsupported model")

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    planner = PlannerAgent(
        llm=llm,
        max_iterations=2,
        reflection_agent=SupervisorReflectionAgent(notebook=FailedCaseNotebook(tmp_path / "failed.jsonl")),
    )
    requirement = TravelRequirement(
        prompt="planner retry test",
        city=fake_plan["city"],
        days=1,
        preferences=fake_plan["preferences"],
        budget_level=fake_plan["budget_level"],
        start_date=date.fromisoformat(fake_plan["days"][0]["date"]),
    )
    attractions = [Attraction.model_validate(fake_plan["days"][0]["attractions"][0])]
    weather = [WeatherInfo.model_validate(fake_plan["weather"][0])]
    hotels = [Hotel.model_validate(fake_plan["days"][0]["hotel"])]

    result = planner.run(requirement, attractions, weather, hotels)

    assert result.generation_mode == "llm"
    assert len(llm.calls) == 2
    assert "Previous attempt failed" in llm.calls[1][1].content
    assert (tmp_path / "failed.jsonl").exists()


def test_planner_agent_reflects_failed_cases_to_memory_after_max_iterations(monkeypatch, tmp_path):
    from app.workflows.reflection_memory import FailedCaseNotebook, ReflectionMemoryStore, SupervisorReflectionAgent

    store = FakeTravelVectorStore()
    memory = ReflectionMemoryStore(store)
    llm = SequentialFakeLLM(["not json", "still not json"])

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        raise RuntimeError("unsupported model")

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    planner = PlannerAgent(
        llm=llm,
        max_iterations=2,
        reflection_memory=memory,
        reflection_agent=SupervisorReflectionAgent(
            memory_store=memory,
            notebook=FailedCaseNotebook(tmp_path / "failed.jsonl"),
        ),
    )
    fake_plan = _fake_llm_plan()
    requirement = TravelRequirement(
        prompt="planner failure memory test",
        city=fake_plan["city"],
        days=1,
        preferences=fake_plan["preferences"],
        budget_level=fake_plan["budget_level"],
        start_date=date.fromisoformat(fake_plan["days"][0]["date"]),
    )
    attractions = [Attraction.model_validate(fake_plan["days"][0]["attractions"][0])]
    weather = [WeatherInfo.model_validate(fake_plan["weather"][0])]
    hotels = [Hotel.model_validate(fake_plan["days"][0]["hotel"])]

    result = planner.run(requirement, attractions, weather, hotels)

    assert result.generation_mode == "fallback"
    assert len(llm.calls) == 2
    assert store.saved
    assert store.saved[0]["source_name"] == "planner-reflection"
    assert (tmp_path / "failed.jsonl").read_text(encoding="utf-8")


def test_planner_agent_invokes_langchain_agent_runtime():
    fake_plan = _fake_llm_plan()
    runtime = FakeLangChainAgent(json.dumps(fake_plan, ensure_ascii=False))
    planner = PlannerAgent(llm=None)
    planner.langchain_agent = runtime
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 1 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)

    plan = planner.run(requirement, attractions, weather, hotels)

    assert runtime.calls
    assert runtime.calls[0]["messages"][0]["role"] == "user"
    assert plan.generation_mode == "llm"


def test_planner_agent_does_not_retry_invoke_when_stream_fails():
    fake_plan = _fake_llm_plan()

    class StreamFailInvokeAgent:
        def __init__(self):
            self.stream_calls = []
            self.invoke_calls = []

        def stream(self, state, **kwargs):
            self.stream_calls.append({"state": state, "kwargs": kwargs})
            raise AttributeError("'NoneType' object has no attribute 'get'")
            yield

        def invoke(self, state):
            self.invoke_calls.append(state)
            return {"messages": [FakeMessage(json.dumps(fake_plan, ensure_ascii=False))]}

    runtime = StreamFailInvokeAgent()
    planner = PlannerAgent(llm=None)
    planner.langchain_agent = runtime
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 1 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)

    plan = planner.run(requirement, attractions, weather, hotels)

    assert runtime.stream_calls
    assert not runtime.invoke_calls
    assert plan.generation_mode == "fallback"


def test_planner_agent_does_not_retry_raw_llm_when_langchain_agent_fails():
    fake_plan = _fake_llm_plan()

    class BrokenLangChainAgent:
        def stream(self, state, **kwargs):
            raise AttributeError("'NoneType' object has no attribute 'get'")
            yield

        def invoke(self, state):
            raise AttributeError("'NoneType' object has no attribute 'get'")

    llm = FakeLLM(json.dumps(fake_plan, ensure_ascii=False))
    planner = PlannerAgent(llm=None)
    planner.llm = llm
    planner.langchain_agent = BrokenLangChainAgent()
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 1 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)

    plan = planner.run(requirement, attractions, weather, hotels)

    assert not llm.calls
    assert plan.generation_mode == "fallback"


def test_planner_agent_marks_fallback_generation_mode_without_model():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert plan.generation_mode == "fallback"


def test_planner_agent_repairs_common_llm_schema_variants(monkeypatch):
    start = date.today() + timedelta(days=7)
    llm_payload = {
        "city": "北京",
        "days_count": 1,
        "preferences": ["历史文化"],
        "budget_level": "中等",
        "map_center": [116.397, 39.916],
        "weather": [
            {
                "date": start.isoformat(),
                "day_weather": "晴",
                "night_weather": "多云",
                "day_temp": "25℃",
                "night_temp": 15,
                "wind": "东北风1-3级",
            }
        ],
        "days": [
            {
                "day_number": 1,
                "date": start.isoformat(),
                "theme": "历史文化",
                "transport": {"mode": "地铁 + 步行", "cost_estimate": 20},
                "hotel": {"id": "hotel-0", "name": "模型精选酒店"},
                "activities": [
                    {
                        "name": "故宫博物院",
                        "duration_minutes": 180,
                        "description": "上午参观皇家宫殿建筑群。",
                    }
                ],
                "meals": [
                    {"type": "早餐", "suggestion": "酒店附近胡同早餐", "estimated_cost": 35},
                    {"type": "午餐", "suggestion": "故宫附近京味午餐", "estimated_cost": 75},
                    {"type": "晚餐", "suggestion": "前门烤鸭", "estimated_cost": 120},
                ],
            }
        ],
        "budget": {
            "total_attractions": 0,
            "total_hotels": 0,
            "total_meals": 0,
            "total_transportation": 0,
            "total": 999,
        },
        "overall_suggestions": "热门景点提前预约。",
        "agent_trace": "plan_generated_v1.0",
    }
    runtime = FakeLangChainAgent(json.dumps(llm_payload, ensure_ascii=False))

    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        return runtime if name == "planner_agent" else object()

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    orchestrator = TravelAgentOrchestrator(llm=object(), disable_external_api=True)

    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 1 天，喜欢历史文化，预算中等"))

    assert runtime.calls
    assert plan.generation_mode == "llm"
    assert plan.days[0].day_index == 1
    assert plan.days[0].hotel.name
    assert plan.days[0].transportation == "地铁 + 步行"
    assert plan.days[0].attractions[0].address
    assert plan.days[0].meals[0].type == "breakfast"
    assert plan.weather[0].suggestion
    assert plan.weather[0].day_temp == 25
    assert plan.map_center.longitude == 116.397
    assert plan.budget.total_attractions > 0
    assert plan.budget.total_hotels > 0
    assert plan.budget.total_meals > 0
    assert plan.budget.total_transportation > 0
    assert plan.budget.total == (
        plan.budget.total_attractions
        + plan.budget.total_hotels
        + plan.budget.total_meals
        + plan.budget.total_transportation
    )
    assert plan.budget.total != 999
    assert plan.overall_suggestions == ["热门景点提前预约。"]


def test_planner_agent_preserves_llm_recommended_attraction_outside_source_pool(monkeypatch):
    start = date.today() + timedelta(days=7)
    amap = AmapMCPClient(api_key="")
    attractions = amap._fallback_attractions_for_city("珠海")
    weather = amap.get_weather("珠海", start, 2)
    hotels = amap.search_hotels("珠海", "中等", limit=3)
    llm_payload = {
        "city": "珠海",
        "days_count": 2,
        "preferences": ["历史文化"],
        "budget_level": "中等",
        "days": [
            {
                "day_index": 1,
                "date": start.isoformat(),
                "summary": "上午游览圆明新园，感受岭南园林。",
                "attractions": [{"name": "圆明新园"}],
            },
            {
                "day_index": 2,
                "date": (start + timedelta(days=1)).isoformat(),
                "summary": "上午漫步唐家古镇，下午前往斗门御温泉，享受温泉放松。",
                "attractions": [
                    {"name": "唐家古镇"},
                    {
                        "name": "斗门御温泉",
                        "category": "休闲温泉",
                        "address": "珠海市斗门区斗门镇",
                        "location": {"longitude": 113.21015, "latitude": 22.23098},
                        "visit_duration_minutes": 180,
                        "description": "由大模型根据用户想泡温泉的需求推荐，适合半日放松。",
                        "ticket_price": 198,
                    },
                ],
            },
        ],
        "weather": [item.model_dump(mode="json") for item in weather],
        "budget": {"total": 0},
        "map_center": {"longitude": 113.576, "latitude": 22.275},
        "overall_suggestions": ["第二天预留温泉放松时间。"],
    }
    def fake_create_agent(model, tools=None, system_prompt=None, name=None, **kwargs):
        raise RuntimeError("unsupported model")

    monkeypatch.setattr("app.workflows.agents.create_agent", fake_create_agent)
    planner = PlannerAgent(llm=FakeLLM(json.dumps(llm_payload, ensure_ascii=False)))
    requirement = TravelRequirement(
        prompt="我想去珠海玩 2 天，喜欢历史文化，预算中等，第二天想去泡温泉",
        city="珠海",
        days=2,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=start,
    )

    result = planner.run(requirement, attractions, weather, hotels)
    second_day_names = [item.name for item in result.selected_plan.days[1].attractions]

    assert result.generation_mode == "llm"
    assert "斗门御温泉" in second_day_names


def test_planner_agent_searches_image_for_llm_attraction_name():
    class FakeImageProvider:
        def __init__(self):
            self.queries = []

        def image_for(self, query):
            self.queries.append(query)
            return "https://example.test/yu-hot-spring.jpg"

    images = FakeImageProvider()
    planner = PlannerAgent(llm=None, image_provider=images)
    fallback = Attraction(
        id="fallback",
        name="\u4f1a\u540c\u53e4\u6751\u65c5\u6e38\u533a",
        category="\u5386\u53f2\u6587\u5316",
        address="\u73e0\u6d77\u5e02\u9999\u6d32\u533a",
        location=Location(longitude=113.5, latitude=22.3),
        visit_duration_minutes=120,
        description="\u4fdd\u5b58\u8f83\u5b8c\u6574\u7684\u5cad\u5357\u6751\u843d\u3002",
        ticket_price=0,
        image_url="https://example.test/wrong-fallback.jpg",
    )

    repaired = planner._repair_llm_attraction(
        {
            "name": "\u73e0\u6d77\u5fa1\u6e29\u6cc9",
            "address": "\u73e0\u6d77\u5e02\u6597\u95e8\u533a\u6597\u95e8\u9547\u5fa1\u6e29\u6cc9\u5ea6\u5047\u6751",
            "location": {"longitude": 113.2456, "latitude": 22.1982},
            "ticket_price": 168,
        },
        fallback,
        day_index=1,
        offset=1,
        city="\u73e0\u6d77",
    )

    assert images.queries == ["\u73e0\u6d77 \u73e0\u6d77\u5fa1\u6e29\u6cc9"]
    assert repaired["image_url"] == "https://example.test/yu-hot-spring.jpg"


def test_planner_agent_prefers_real_amap_meals_over_generic_llm_meal_text():
    class FakeMealAmap:
        def search_meals(self, city, budget_level, food_preferences="", route_points=None):
            return [
                Meal(
                    id="real-breakfast",
                    type="breakfast",
                    name="珠海老字号早茶",
                    address="情侣中路1号",
                    estimated_cost=35,
                    description="餐饮服务;早茶，距离当日路线约0.5公里，适合安排为早餐。",
                    location=Location(longitude=113.576, latitude=22.292),
                    rating=4.7,
                    category="餐饮服务;中餐厅;广东菜",
                ),
                Meal(
                    id="real-lunch",
                    type="lunch",
                    name="珠海本地菜馆",
                    address="海虹路88号",
                    estimated_cost=70,
                    description="餐饮服务;中餐厅，适合安排为午餐。",
                    location=Location(longitude=113.577, latitude=22.293),
                    rating=4.6,
                    category="餐饮服务;中餐厅",
                ),
                Meal(
                    id="real-dinner",
                    type="dinner",
                    name="珠海海鲜小馆",
                    address="唐家湾镇",
                    estimated_cost=105,
                    description="餐饮服务;海鲜，适合安排为晚餐。",
                    location=Location(longitude=113.596, latitude=22.358),
                    rating=4.5,
                    category="餐饮服务;海鲜",
                ),
            ]

    planner = PlannerAgent(llm=None, amap=FakeMealAmap())

    meals = planner._repair_meals(
        [
            {"type": "早餐", "suggestion": "珠海胡同早餐", "estimated_cost": 35},
            {"type": "午餐", "suggestion": "珠海特色午餐", "estimated_cost": 70},
            {"type": "晚餐", "suggestion": "珠海风味晚餐", "estimated_cost": 105},
        ],
        city="珠海",
        budget_level="中等",
        route_points=[{"longitude": 113.576561, "latitude": 22.292980}],
    )

    assert meals[0]["name"] == "珠海老字号早茶"
    assert meals[0]["address"] == "情侣中路1号"
    assert meals[0]["id"] == "real-breakfast"
    assert meals[0]["location"] == {"longitude": 113.576, "latitude": 22.292}


def test_fallback_plan_groups_nearby_attractions_and_hotels_by_route():
    planner = PlannerAgent(llm=None)
    requirement = TravelRequirement(
        prompt="我想去北京玩 2 天，预算中等",
        city="北京",
        days=2,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )
    attractions = [
        Attraction(id="a1", name="东城景点A", category="景点", address="东城", location=Location(longitude=116.400, latitude=39.900), visit_duration_minutes=90, description="东城片区", ticket_price=20),
        Attraction(id="b1", name="西郊景点A", category="景点", address="西郊", location=Location(longitude=116.000, latitude=39.600), visit_duration_minutes=90, description="西郊片区", ticket_price=20),
        Attraction(id="a2", name="东城景点B", category="景点", address="东城", location=Location(longitude=116.405, latitude=39.905), visit_duration_minutes=90, description="东城片区", ticket_price=20),
        Attraction(id="b2", name="西郊景点B", category="景点", address="西郊", location=Location(longitude=116.005, latitude=39.605), visit_duration_minutes=90, description="西郊片区", ticket_price=20),
    ]
    hotels = [
        Hotel(id="hotel-east", name="东城酒店", address="东城", location=Location(longitude=116.402, latitude=39.902), type="中等型酒店", rating=4.6, nightly_price=520, description="靠近东城景点"),
        Hotel(id="hotel-west", name="西郊酒店", address="西郊", location=Location(longitude=116.002, latitude=39.602), type="中等型酒店", rating=4.6, nightly_price=520, description="靠近西郊景点"),
    ]
    weather = AmapMCPClient(api_key="").get_weather("北京", requirement.start_date, requirement.days)

    plan = planner._fallback_plan(requirement, attractions, weather, hotels)

    assert [item.name for item in plan.days[0].attractions] == ["东城景点A", "东城景点B"]
    assert [item.name for item in plan.days[1].attractions] == ["西郊景点A", "西郊景点B"]
    assert plan.days[0].hotel.name == "东城酒店"
    assert plan.days[1].hotel.name == "西郊酒店"
    assert planner._route_span_km(plan.days[0].route_points) < 1
    assert planner._route_span_km(plan.days[1].route_points) < 1


def test_llm_plan_normalization_repairs_cross_district_days_and_hotels():
    planner = PlannerAgent(llm=None)
    requirement = TravelRequirement(
        prompt="我想去北京玩 2 天，预算中等",
        city="北京",
        days=2,
        preferences=["历史文化"],
        budget_level="中等",
        start_date=date(2026, 6, 1),
    )
    attractions = [
        Attraction(id="a1", name="东城景点A", category="景点", address="东城", location=Location(longitude=116.400, latitude=39.900), visit_duration_minutes=90, description="东城片区", ticket_price=20),
        Attraction(id="b1", name="西郊景点A", category="景点", address="西郊", location=Location(longitude=116.000, latitude=39.600), visit_duration_minutes=90, description="西郊片区", ticket_price=20),
        Attraction(id="a2", name="东城景点B", category="景点", address="东城", location=Location(longitude=116.405, latitude=39.905), visit_duration_minutes=90, description="东城片区", ticket_price=20),
        Attraction(id="b2", name="西郊景点B", category="景点", address="西郊", location=Location(longitude=116.005, latitude=39.605), visit_duration_minutes=90, description="西郊片区", ticket_price=20),
    ]
    hotels = [
        Hotel(id="hotel-east", name="东城酒店", address="东城", location=Location(longitude=116.402, latitude=39.902), type="中等型酒店", rating=4.6, nightly_price=520, description="靠近东城景点"),
        Hotel(id="hotel-west", name="西郊酒店", address="西郊", location=Location(longitude=116.002, latitude=39.602), type="中等型酒店", rating=4.6, nightly_price=520, description="靠近西郊景点"),
    ]
    weather = AmapMCPClient(api_key="").get_weather("北京", requirement.start_date, requirement.days)

    normalized = planner._normalize_llm_data(
        {
            "days": [
                {"attractions": [{"id": "a1"}, {"id": "b1"}], "hotel": hotels[1].model_dump(mode="json")},
                {"attractions": [{"id": "a2"}, {"id": "b2"}], "hotel": hotels[0].model_dump(mode="json")},
            ]
        },
        requirement,
        attractions,
        weather,
        hotels,
    )

    assert [item["name"] for item in normalized["days"][0]["attractions"]] == ["东城景点A", "东城景点B"]
    assert [item["name"] for item in normalized["days"][1]["attractions"]] == ["西郊景点A", "西郊景点B"]
    assert normalized["days"][0]["hotel"]["name"] == "东城酒店"
    assert normalized["days"][1]["hotel"]["name"] == "西郊酒店"


def test_planner_prompt_limits_source_lists_for_faster_llm_response():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    requirement = orchestrator.parser.parse("我想去北京玩 3 天，喜欢历史文化，预算中等")
    attractions = orchestrator.attractions.run(requirement)
    weather = orchestrator.weather.run(requirement)
    hotels = orchestrator.hotels.run(requirement)

    prompt = PlannerAgent()._build_prompt(requirement, attractions, weather, hotels)
    payload = json.loads(prompt.rsplit("\n", 1)[1])

    assert len(payload["attractions"]) <= 6
    assert len(payload["hotels"]) <= 2
    assert len(prompt) < 5000


def test_budget_recalculates_after_deleting_attraction():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    plan = orchestrator.plan(TripPlanRequest(prompt="我想去北京玩 2 天，喜欢历史文化，预算中等"))
    removed_price = plan.days[0].attractions[0].ticket_price
    plan.days[0].attractions.pop(0)

    recalculated = BudgetCalculator().calculate(plan.days)

    assert recalculated.total_attractions == plan.budget.total_attractions - removed_price
    assert recalculated.total == (
        recalculated.total_attractions
        + recalculated.total_hotels
        + recalculated.total_meals
        + recalculated.total_transportation
    )


def test_api_health_plan_and_recalculate_endpoints():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert "llm" in health.json()
    assert health.json()["amap_transport"] == "mcp-stdio"

    response = client.post("/api/trip/plan", json={"prompt": "我想去北京玩 3 天，喜欢历史文化，预算中等"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["city"] == "北京"
    assert payload["data"]["selected_option_id"] == "balanced"
    assert [option["id"] for option in payload["data"]["options"]] == ["balanced", "relaxed", "deep_dive"]
    assert payload["data"]["research_context"]
    assert payload["data"]["clarifying_suggestions"]

    plan = payload["data"]["options"][0]["plan"]
    original_total = plan["budget"]["total"]
    for day in plan["days"]:
        paid_index = next((index for index, item in enumerate(day["attractions"]) if item["ticket_price"] > 0), None)
        if paid_index is not None:
            del day["attractions"][paid_index]
            break
    recalc = client.post("/api/trip/recalculate", json={"plan": plan})

    assert recalc.status_code == 200
    assert recalc.json()["data"]["budget"]["total"] < original_total

    poi = client.get("/api/map/poi", params={"keywords": "历史文化", "city": "北京"})
    assert poi.status_code == 200
    assert poi.json()["success"] is True
    assert poi.json()["data"]

    weather = client.get("/api/map/weather", params={"city": "北京", "days": 2})
    assert weather.status_code == 200
    assert len(weather.json()["data"]) == 2


def test_api_exposes_travel_qa_and_news_ingestion(monkeypatch):
    class FakeQAAgent:
        def ask(self, question, top_k=5, conversation_history=None):
            return TravelQAResponse(
                answer=f"回答：{question}",
                sources=[
                    TravelKnowledgeSource(
                        title="南京端午预约提醒",
                        url="https://example.test/nanjing",
                        summary="热门景区建议提前预约。",
                        source="rss",
                        score=0.9,
                    )
                ],
                retrieved_count=1,
                generation_mode="fallback",
            )

    class FakeNewsAgent:
        def fetch_travel_feeds(self, feed_urls):
            return {
                "total_seen": 2,
                "total_added": 2,
                "feeds": [{"url": feed_urls[0], "seen": 2, "added": 2}],
                "errors": [],
            }

    monkeypatch.setattr(main_module, "qa_agent", FakeQAAgent())
    monkeypatch.setattr(main_module, "news_agent", FakeNewsAgent())
    client = TestClient(app)

    qa_response = client.post("/api/qa/ask", json={"question": "端午去南京要注意什么？"})
    assert qa_response.status_code == 200
    assert qa_response.json()["data"]["answer"].startswith("回答")
    assert qa_response.json()["data"]["sources"][0]["title"] == "南京端午预约提醒"

    ingest_response = client.post("/api/news/ingest", json={"feed_urls": ["https://feeds.example.test/travel"]})
    assert ingest_response.status_code == 200
    assert ingest_response.json()["data"]["total_added"] == 2


def test_api_qa_persists_conversation_history():
    class FakeQAAgent:
        def __init__(self):
            self.calls = []

        def ask(self, question, top_k=5, conversation_history=None, config=None):
            self.calls.append(
                {
                    "question": question,
                    "top_k": top_k,
                    "conversation_history": conversation_history or [],
                    "config": config,
                }
            )
            return TravelQAResponse(
                answer="南京博物院可以在官方渠道提前预约。",
                sources=[],
                retrieved_count=0,
                generation_mode="fallback",
                used_web_search=True,
            )

    class FakeQAStore:
        def __init__(self):
            self.messages = [
                {"role": "user", "content": "端午去南京三天怎么安排？"},
                {"role": "assistant", "content": "建议关注夫子庙和南京博物院。"},
            ]
            self.saved = []

        def health(self):
            return {"enabled": True, "ok": True}

        def get_or_create_conversation(self, conversation_id=None, user_id=None, anonymous_id=None, title=None):
            return {
                "id": conversation_id or "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "user_id": user_id,
                "anonymous_id": anonymous_id,
                "title": title or "端午去南京三天怎么安排？",
                "created_at": datetime(2026, 6, 14, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 6, 14, tzinfo=timezone.utc),
            }

        def get_recent_messages(self, conversation_id, limit=8):
            return self.messages[-limit:]

        def append_message(self, conversation_id, role, content, **kwargs):
            self.saved.append(
                {
                    "conversation_id": conversation_id,
                    "role": role,
                    "content": content,
                    **kwargs,
                }
            )

    fake_agent = FakeQAAgent()
    fake_store = FakeQAStore()
    original_qa_agent = main_module.qa_agent
    original_qa_store = getattr(main_module, "qa_store", None)
    main_module.qa_agent = fake_agent
    main_module.qa_store = fake_store
    try:
        client = TestClient(app)
        response = client.post(
            "/api/qa/ask",
            json={
                "question": "那博物馆怎么预约？",
                "conversation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "anonymous_id": "anon-browser-1",
            },
        )
    finally:
        main_module.qa_agent = original_qa_agent
        main_module.qa_store = original_qa_store

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["conversation_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert fake_agent.calls[0]["conversation_history"] == fake_store.messages
    assert fake_agent.calls[0]["config"]["configurable"]["thread_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert [item["role"] for item in fake_store.saved] == ["user", "assistant"]
    assert fake_store.saved[0]["content"] == "那博物馆怎么预约？"
    assert fake_store.saved[1]["content"] == "南京博物院可以在官方渠道提前预约。"
    assert fake_store.saved[1]["used_web_search"] is True


def test_api_qa_stream_returns_incremental_answer_events():
    class FakeQAAgent:
        def __init__(self):
            self.calls = []

        def stream(self, question, top_k=5, conversation_history=None, config=None):
            self.calls.append(
                {
                    "question": question,
                    "top_k": top_k,
                    "conversation_history": conversation_history or [],
                    "config": config,
                }
            )
            yield {"event": "answer_delta", "data": {"content": "南京"}}
            yield {"event": "answer_delta", "data": {"content": "需要提前预约。"}}
            yield {
                "event": "done",
                "data": TravelQAResponse(
                    answer="南京需要提前预约。",
                    sources=[],
                    retrieved_count=0,
                    generation_mode="fallback",
                    used_web_search=True,
                ),
            }

    class FakeQAStore:
        def __init__(self):
            self.saved = []

        def get_or_create_conversation(self, conversation_id=None, user_id=None, anonymous_id=None, title=None):
            return {
                "id": conversation_id or "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "title": title or "新的旅行问答",
                "created_at": datetime(2026, 6, 14, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 6, 14, tzinfo=timezone.utc),
            }

        def get_recent_messages(self, conversation_id, limit=8):
            return []

        def append_message(self, conversation_id, role, content, **kwargs):
            self.saved.append({"role": role, "content": content, **kwargs})
            return {"id": f"{role}-message-id"}

    fake_store = FakeQAStore()
    fake_agent = FakeQAAgent()
    original_qa_agent = main_module.qa_agent
    original_qa_store = getattr(main_module, "qa_store", None)
    main_module.qa_agent = fake_agent
    main_module.qa_store = fake_store
    try:
        client = TestClient(app)
        with client.stream(
            "POST",
            "/api/qa/ask/stream",
            json={"question": "南京博物馆怎么预约？", "anonymous_id": "anon-browser-1"},
        ) as response:
            body = response.read().decode("utf-8")
    finally:
        main_module.qa_agent = original_qa_agent
        main_module.qa_store = original_qa_store

    assert "event: answer_delta" in body
    assert body.count("event: answer_delta") == 2
    assert "data: {\"content\":\"南京\"}" in body
    assert "event: done" in body
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in body
    assert fake_agent.calls[0]["config"]["configurable"]["thread_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert [item["role"] for item in fake_store.saved] == ["user", "assistant"]
    assert fake_store.saved[1]["content"] == "南京需要提前预约。"
    assert fake_store.saved[1]["used_web_search"] is True


def test_in_memory_qa_store_returns_conversation_history_and_detail():
    from app.storage.qa_store import InMemoryQAConversationStore

    store = InMemoryQAConversationStore()
    conversation = store.get_or_create_conversation(
        user_id="user-1",
        anonymous_id="anon-1",
        title="端午去南京三天有哪些预约建议？",
    )
    store.append_message(conversation["id"], "user", "端午去南京三天有哪些预约建议？")
    store.append_message(
        conversation["id"],
        "assistant",
        "热门场馆建议提前预约。",
        generation_mode="fallback",
        used_web_search=True,
    )

    recent = store.get_recent_messages(conversation["id"])
    summaries = store.list_conversations(user_id="user-1")
    detail = store.get_conversation(conversation["id"])

    assert recent == [
        {"role": "user", "content": "端午去南京三天有哪些预约建议？"},
        {"role": "assistant", "content": "热门场馆建议提前预约。"},
    ]
    assert summaries[0].id == conversation["id"]
    assert detail is not None
    assert detail.messages[1].role == "assistant"
    assert detail.messages[1].content == "热门场馆建议提前预约。"
    assert detail.messages[1].used_web_search is True


def test_api_plan_persists_report_and_returns_report_id():
    class FakeReportStore:
        def __init__(self):
            self.saved = []
            self.saved_logs = []
            self.cached_assets = []
            self.updated_images = []

        def health(self):
            return {"enabled": True, "ok": True}

        def save_report(self, request, result):
            self.saved.append({"request": request, "result": result})
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "created_at": datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
            }

        def save_plan_logs(self, report_id, logs):
            self.saved_logs.append({"report_id": report_id, "logs": logs})

        def get_cached_asset(self, asset_type, cache_key):
            return None

        def upsert_asset_cache(self, asset_type, cache_key, city, name, value, response_payload=None):
            self.cached_assets.append(
                {
                    "asset_type": asset_type,
                    "cache_key": cache_key,
                    "city": city,
                    "name": name,
                    "value": value,
                    "response_payload": response_payload,
                }
            )

        def update_report_attraction_image(self, report_id, attraction_name, image_url):
            self.updated_images.append(
                {
                    "report_id": report_id,
                    "attraction_name": attraction_name,
                    "image_url": image_url,
                }
            )

    class FakeImageProvider:
        def image_for(self, query):
            return f"https://img.example.test/{len(query)}.jpg"

    store = FakeReportStore()
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    main_module.report_store = store
    main_module.image_provider = FakeImageProvider()
    client = TestClient(app)

    response = client.post("/api/trip/plan", json={"prompt": "我想去北京玩 1 天，喜欢历史文化，预算中等"})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["report_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["report_created_at"] == "2026-05-19T08:00:00Z"
    assert store.saved[0]["request"].prompt == "我想去北京玩 1 天，喜欢历史文化，预算中等"
    assert store.saved[0]["result"].selected_option_id == "balanced"
    assert store.saved_logs[0]["report_id"] == "11111111-1111-1111-1111-111111111111"
    assert store.cached_assets
    assert store.updated_images
    main_module.report_store = None
    main_module.image_provider = None


def test_api_poi_photo_uses_cached_asset_and_updates_report_without_provider_call():
    class FakeReportStore:
        def __init__(self):
            self.updated_images = []

        def health(self):
            return {"enabled": True, "ok": True}

        def get_cached_asset(self, asset_type, cache_key):
            assert asset_type == "attraction_image"
            assert "北京" in cache_key
            assert "故宫博物院" in cache_key
            return {"value": "https://img.example.test/cached.jpg"}

        def update_report_attraction_image(self, report_id, attraction_name, image_url):
            self.updated_images.append((report_id, attraction_name, image_url))

    class ForbiddenImageProvider:
        def image_for(self, query):
            raise AssertionError("cached image should avoid provider calls")

    store = FakeReportStore()
    main_module.report_store = store
    main_module.image_provider = ForbiddenImageProvider()
    client = TestClient(app)

    response = client.get(
        "/api/poi/photo",
        params={
            "name": "故宫博物院",
            "city": "北京",
            "report_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["photo_url"] == "https://img.example.test/cached.jpg"
    assert store.updated_images == [
        ("11111111-1111-1111-1111-111111111111", "故宫博物院", "https://img.example.test/cached.jpg")
    ]
    main_module.report_store = None
    main_module.image_provider = None


def test_api_poi_photo_caches_provider_result_and_updates_report():
    class FakeReportStore:
        def __init__(self):
            self.cached_assets = []
            self.updated_images = []

        def health(self):
            return {"enabled": True, "ok": True}

        def get_cached_asset(self, asset_type, cache_key):
            return None

        def upsert_asset_cache(self, asset_type, cache_key, city, name, value, response_payload=None):
            self.cached_assets.append((asset_type, cache_key, city, name, value, response_payload))

        def update_report_attraction_image(self, report_id, attraction_name, image_url):
            self.updated_images.append((report_id, attraction_name, image_url))

    class FakeImageProvider:
        def __init__(self):
            self.calls = []

        def image_for(self, query):
            self.calls.append(query)
            return "https://img.example.test/fresh.jpg"

    store = FakeReportStore()
    provider = FakeImageProvider()
    main_module.report_store = store
    main_module.image_provider = provider
    client = TestClient(app)

    response = client.get(
        "/api/poi/photo",
        params={
            "name": "故宫博物院",
            "city": "北京",
            "report_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["photo_url"] == "https://img.example.test/fresh.jpg"
    assert provider.calls == ["北京 故宫博物院 China landmark"]
    assert store.cached_assets[0][0] == "attraction_image"
    assert store.cached_assets[0][2] == "北京"
    assert store.cached_assets[0][3] == "故宫博物院"
    assert store.updated_images == [
        ("11111111-1111-1111-1111-111111111111", "故宫博物院", "https://img.example.test/fresh.jpg")
    ]
    main_module.report_store = None
    main_module.image_provider = None


def test_api_map_poi_uses_cached_asset_without_amap_call():
    class FakeReportStore:
        def health(self):
            return {"enabled": True, "ok": True}

        def get_cached_asset(self, asset_type, cache_key):
            assert asset_type == "map_poi"
            assert "北京" in cache_key
            assert "历史文化" in cache_key
            return {
                "value": "cached",
                "response_payload": [
                    {
                        "id": "cached-poi",
                        "name": "缓存景点",
                        "category": "历史文化",
                        "address": "缓存地址",
                        "location": {"longitude": 116.397, "latitude": 39.916},
                        "visit_duration_minutes": 120,
                        "description": "来自缓存",
                        "ticket_price": 0,
                        "image_url": "",
                        "rating": 4.6,
                    }
                ],
            }

    class ForbiddenAmap:
        def search_pois(self, *args, **kwargs):
            raise AssertionError("cached map POI should avoid Amap calls")

    previous_orchestrator = main_module.orchestrator
    main_module.orchestrator = SimpleNamespace(amap=ForbiddenAmap(), planner=SimpleNamespace(llm=None), unsplash=None)
    main_module.report_store = FakeReportStore()
    client = TestClient(app)

    response = client.get("/api/map/poi", params={"keywords": "历史文化", "city": "北京", "limit": 3})

    assert response.status_code == 200
    assert response.json()["data"][0]["name"] == "缓存景点"
    main_module.orchestrator = previous_orchestrator
    main_module.report_store = None


def test_api_recalculate_persists_report_revision_when_report_id_is_supplied():
    class FakeReportStore:
        def __init__(self):
            self.updated = []

        def health(self):
            return {"enabled": True, "ok": True}

        def save_report(self, request, result):
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "created_at": datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
            }

        def update_report_plan(self, report_id, plan, operation, research_context):
            self.updated.append(
                {
                    "report_id": report_id,
                    "plan": plan,
                    "operation": operation,
                    "research_context": research_context,
                }
            )

    store = FakeReportStore()
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    main_module.report_store = store
    client = TestClient(app)
    response = client.post("/api/trip/plan", json={"prompt": "我想去北京玩 1 天，喜欢历史文化，预算中等"})
    plan = response.json()["data"]["options"][0]["plan"]

    recalc = client.post(
        "/api/trip/recalculate",
        json={
            "report_id": "11111111-1111-1111-1111-111111111111",
            "plan": plan,
            "operation": "reorder_day",
            "day_index": 1,
        },
    )

    assert recalc.status_code == 200
    assert store.updated[0]["report_id"] == "11111111-1111-1111-1111-111111111111"
    assert store.updated[0]["operation"] == "reorder_day"
    assert store.updated[0]["plan"].budget.total == recalc.json()["data"]["budget"]["total"]
    main_module.report_store = None


def test_api_plan_get_returns_usage_hint():
    client = TestClient(app)

    response = client.get("/api/trip/plan")

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "POST"
    assert "POST" in body["message"]
    assert body["example"]["prompt"]


def test_api_recalculate_can_refill_day_after_deleting_attractions():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)
    response = client.post("/api/trip/plan", json={"prompt": "我想去北京玩 2 天，喜欢历史文化，预算中等"})
    plan = response.json()["data"]["options"][0]["plan"]
    plan["days"][0]["attractions"] = plan["days"][0]["attractions"][:1]

    recalc = client.post(
        "/api/trip/recalculate",
        json={"plan": plan, "operation": "refill_day", "day_index": 1},
    )

    assert recalc.status_code == 200
    assert len(recalc.json()["data"]["days"][0]["attractions"]) > 1


def test_api_plan_honors_structured_dates_and_days():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)

    response = client.post(
        "/api/trip/plan",
        json={
            "prompt": "我想去北京玩，喜欢历史文化，预算中等",
            "start_date": "2026-06-01",
            "days": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["days_count"] == 2
    assert [day["date"] for day in payload["days"]] == ["2026-06-01", "2026-06-02"]
    assert [item["date"] for item in payload["weather"]] == ["2026-06-01", "2026-06-02"]


def test_api_uses_resources_from_app_state_when_available():
    class FakeStore:
        def health(self):
            return {"enabled": True, "ok": True, "source": "state"}

    previous_state = getattr(app.state, "resources", None)
    previous_orchestrator = main_module.orchestrator
    previous_report_store = main_module.report_store
    previous_vector_store = main_module.travel_vector_store
    try:
        main_module.orchestrator = None
        main_module.report_store = None
        main_module.travel_vector_store = None
        app.state.resources = SimpleNamespace(
            orchestrator=TravelAgentOrchestrator(disable_llm=True, disable_external_api=True),
            report_store=FakeStore(),
            travel_vector_store=FakeStore(),
            news_agent=main_module.news_agent,
            qa_agent=main_module.qa_agent,
            image_provider=main_module.image_provider,
        )
        client = TestClient(app)

        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["database"]["source"] == "state"
        assert response.json()["travel_knowledge"]["source"] == "state"
    finally:
        main_module.orchestrator = previous_orchestrator
        main_module.report_store = previous_report_store
        main_module.travel_vector_store = previous_vector_store
        if previous_state is None:
            try:
                del app.state.resources
            except AttributeError:
                pass
        else:
            app.state.resources = previous_state


def test_orchestrator_runs_independent_planning_steps_concurrently():
    orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    sleep_seconds = 0.18

    class SlowAttractionAgent:
        name = "SlowAttractionAgent"

        def run(self, requirement):
            time.sleep(sleep_seconds)
            return [
                Attraction(
                    id="a1",
                    name="Concurrent Attraction",
                    category="landmark",
                    address="Concurrent Road",
                    location=Location(longitude=116.397, latitude=39.916),
                    visit_duration_minutes=90,
                    description="A deterministic test attraction",
                    ticket_price=10,
                )
            ]

    class SlowWeatherAgent:
        name = "SlowWeatherAgent"

        def run(self, requirement):
            time.sleep(sleep_seconds)
            return AmapMCPClient(api_key="").get_weather(requirement.city, requirement.start_date, requirement.days)

    class SlowHotelAgent:
        name = "SlowHotelAgent"

        def run(self, requirement):
            time.sleep(sleep_seconds)
            return [
                Hotel(
                    id="h1",
                    name="Concurrent Hotel",
                    address="Concurrent Road",
                    location=Location(longitude=116.397, latitude=39.916),
                    type="standard",
                    rating=4.6,
                    nightly_price=500,
                    description="A deterministic test hotel",
                )
            ]

    class SlowResearchService:
        def research(self, city, preferences, days):
            time.sleep(sleep_seconds)
            return []

    orchestrator.attractions = SlowAttractionAgent()
    orchestrator.weather = SlowWeatherAgent()
    orchestrator.hotels = SlowHotelAgent()
    orchestrator.research = SlowResearchService()

    started = time.perf_counter()
    result = orchestrator.plan(TripPlanRequest(prompt="test prompt for concurrency", days=1, start_date=date(2026, 6, 1)))
    elapsed = time.perf_counter() - started

    assert result.selected_plan.days_count == 1
    assert elapsed < sleep_seconds * 3


def test_map_endpoints_reject_excessive_query_sizes():
    main_module.orchestrator = TravelAgentOrchestrator(disable_llm=True, disable_external_api=True)
    client = TestClient(app)

    poi = client.get("/api/map/poi", params={"keywords": "history", "city": "Beijing", "limit": 500})
    weather = client.get("/api/map/weather", params={"city": "Beijing", "days": 99})

    assert poi.status_code == 422
    assert weather.status_code == 422


def _fake_llm_plan():
    start = date.today() + timedelta(days=7)
    return {
        "city": "北京",
        "days_count": 1,
        "preferences": ["历史文化"],
        "budget_level": "中等",
        "days": [
            {
                "day_index": 1,
                "date": start.isoformat(),
                "theme": "历史文化",
                "summary": "LLM生成的历史文化一日游",
                "transportation": "公共交通 + 步行",
                "hotel": {
                    "id": "hotel-llm",
                    "name": "LLM精选酒店",
                    "address": "北京市东城区",
                    "location": {"longitude": 116.397128, "latitude": 39.916527},
                    "type": "中等型酒店",
                    "rating": 4.7,
                    "nightly_price": 520,
                    "description": "靠近核心景点",
                },
                "attractions": [
                    {
                        "id": "poi-llm-1",
                        "name": "故宫博物院",
                        "category": "历史文化",
                        "address": "北京市东城区景山前街4号",
                        "location": {"longitude": 116.397, "latitude": 39.916},
                        "visit_duration_minutes": 240,
                        "description": "皇家宫殿建筑群",
                        "ticket_price": 60,
                        "image_url": None,
                    }
                ],
                "meals": [
                    {"type": "breakfast", "name": "胡同早餐", "address": "酒店附近", "estimated_cost": 35, "description": "本地早餐"},
                    {"type": "lunch", "name": "京味午餐", "address": "故宫附近", "estimated_cost": 70, "description": "北京菜"},
                    {"type": "dinner", "name": "烤鸭晚餐", "address": "前门附近", "estimated_cost": 120, "description": "经典晚餐"},
                ],
                "route_points": [{"longitude": 116.397, "latitude": 39.916}],
                "estimated_transport_cost": 55,
            }
        ],
        "weather": [
            {
                "date": start.isoformat(),
                "day_weather": "晴",
                "night_weather": "多云",
                "day_temp": 25,
                "night_temp": 15,
                "wind": "东北风 1-3级",
                "suggestion": "适合步行",
            }
        ],
        "budget": {
            "total_attractions": 60,
            "total_hotels": 520,
            "total_meals": 225,
            "total_transportation": 55,
            "total": 860,
        },
        "map_center": {"longitude": 116.397, "latitude": 39.916},
        "overall_suggestions": ["提前预约热门景点"],
        "agent_trace": ["AttractionSearchAgent", "WeatherQueryAgent", "HotelAgent", "PlannerAgent"],
    }
