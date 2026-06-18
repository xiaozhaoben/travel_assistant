import os
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

for key in (
    "DATABASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "AMAP_API_KEY",
    "AMAP_MAPS_API_KEY",
    "UNSPLASH_ACCESS_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "WEB_SEARCH_MCP_COMMAND",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "DASHSCOPE_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_MAX_RESULTS",
    "TAVILY_SEARCH_DEPTH",
    "ROLLINGGO_HOTEL_MCP_URL",
    "ROLLINGGO_HOTEL_API_KEY",
    "ROLLINGGO_HOTEL_ACCEPT_LANGUAGE",
):
    os.environ[key] = ""

for logger_name in (
    "langsmith",
    "langsmith.client",
    "langsmith._internal._background_thread",
    "urllib3",
    "urllib3.connectionpool",
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

if os.getenv("RUN_LANGSMITH_EVAL", "").lower() not in {"1", "true", "yes", "on"}:
    os.environ["LANGSMITH_TRACING"] = "false"
