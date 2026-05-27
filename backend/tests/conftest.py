import sys
import os
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
):
    os.environ[key] = ""
