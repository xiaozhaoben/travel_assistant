from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import uuid
from typing import Any


logger = logging.getLogger(__name__)

KEY_PREFIX = "travel-assistant:knowledge-job:"
HASH_FIELDS = frozenset(
    {
        "job_id",
        "status",
        "message",
        "source_type",
        "result_json",
        "error_code",
        "created_at",
        "updated_at",
    }
)


class KnowledgeJobNotFound(Exception):
    pass


class KnowledgeJobStoreUnavailable(Exception):
    pass


@dataclass(frozen=True)
class KnowledgeIngestJob:
    job_id: str
    status: str
    message: str
    source_type: str
    result: dict[str, Any] | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class RedisKnowledgeJobStore:
    def __init__(self, client: Any | None, *, ttl_seconds: int):
        self.client = client
        self.ttl_seconds = max(1, int(ttl_seconds))

    def create(self, *, source_type: str, message: str) -> KnowledgeIngestJob:
        now = datetime.now(timezone.utc)
        job = KnowledgeIngestJob(
            job_id=uuid.uuid4().hex,
            status="queued",
            message=message,
            source_type=source_type,
            result=None,
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        self._write(job)
        return job

    def get(self, job_id: str) -> KnowledgeIngestJob:
        client = self._require_client()
        try:
            raw = client.hgetall(self._key(job_id))
        except Exception as exc:
            self._raise_unavailable("get", exc)
        if not raw:
            raise KnowledgeJobNotFound("Knowledge ingest job not found")
        return self._deserialize(raw)

    def update(self, job_id: str, **changes: Any) -> KnowledgeIngestJob:
        current = self.get(job_id)
        allowed = {"status", "message", "result", "error_code"}
        unexpected = set(changes) - allowed
        if unexpected:
            raise ValueError("Unsupported knowledge job field")
        updated = replace(current, **changes, updated_at=datetime.now(timezone.utc))
        self._write(updated)
        return updated

    def _write(self, job: KnowledgeIngestJob) -> None:
        client = self._require_client()
        key = self._key(job.job_id)
        payload = self._serialize(job)
        try:
            pipeline = client.pipeline(transaction=True)
            pipeline.hset(key, mapping=payload)
            pipeline.expire(key, self.ttl_seconds)
            results = pipeline.execute()
            if len(results) != 2 or not bool(results[1]):
                raise RuntimeError("Knowledge job TTL transaction failed")
        except Exception as exc:
            self._raise_unavailable("write", exc)

    def _require_client(self):
        if self.client is None:
            raise KnowledgeJobStoreUnavailable("Knowledge job store is unavailable")
        return self.client

    @staticmethod
    def _key(job_id: str) -> str:
        return f"{KEY_PREFIX}{job_id}"

    @staticmethod
    def _serialize(job: KnowledgeIngestJob) -> dict[str, str]:
        result_json = ""
        if job.result is not None:
            result_json = json.dumps(job.result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        payload = {
            "job_id": job.job_id,
            "status": job.status,
            "message": job.message,
            "source_type": job.source_type,
            "result_json": result_json,
            "error_code": job.error_code or "",
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
        if set(payload) != HASH_FIELDS:  # pragma: no cover - schema guard
            raise RuntimeError("Knowledge job hash schema mismatch")
        return payload

    @staticmethod
    def _deserialize(raw: dict[Any, Any]) -> KnowledgeIngestJob:
        normalized = {
            _decode_redis_value(key): _decode_redis_value(value)
            for key, value in raw.items()
        }
        try:
            result_json = normalized.get("result_json", "")
            result = json.loads(result_json) if result_json else None
            if result is not None and not isinstance(result, dict):
                raise ValueError("Invalid result payload")
            return KnowledgeIngestJob(
                job_id=normalized["job_id"],
                status=normalized["status"],
                message=normalized["message"],
                source_type=normalized["source_type"],
                result=result,
                error_code=normalized.get("error_code") or None,
                created_at=datetime.fromisoformat(normalized["created_at"]),
                updated_at=datetime.fromisoformat(normalized["updated_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Knowledge job decode failed exception_type=%s", type(exc).__name__)
            raise KnowledgeJobStoreUnavailable("Knowledge job store is unavailable") from exc

    @staticmethod
    def _raise_unavailable(operation: str, exc: Exception):
        logger.warning(
            "Knowledge job store operation failed operation=%s exception_type=%s",
            operation,
            type(exc).__name__,
        )
        raise KnowledgeJobStoreUnavailable("Knowledge job store is unavailable") from exc


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
