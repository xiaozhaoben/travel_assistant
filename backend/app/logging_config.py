from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import BACKEND_DIR

AGENT_LOGGER_NAME = "travel_assistant.agent"


def setup_logging(level: str = "INFO") -> None:
    log_dir = BACKEND_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(level.upper())

    if not any(getattr(handler, "_travel_assistant_handler", False) for handler in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler._travel_assistant_handler = True

        file_handler = RotatingFileHandler(
            log_dir / "travel_assistant.log",
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler._travel_assistant_handler = True

        root.addHandler(console_handler)
        root.addHandler(file_handler)


def log_agent_event(agent: str, event: str, payload: Any) -> None:
    logging.getLogger(AGENT_LOGGER_NAME).info(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": agent,
                "event": event,
                "payload": _to_jsonable(payload),
            },
            ensure_ascii=False,
        )
    )


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value
