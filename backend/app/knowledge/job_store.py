from __future__ import annotations

from dataclasses import dataclass
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

UPDATE_JOB_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end

local current_status = redis.call('HGET', KEYS[1], 'status')
if ARGV[1] == '1' then
    local next_status = ARGV[2]
    local valid = false
    if current_status == 'queued' or current_status == 'pending' then
        valid = next_status == 'running' or next_status == 'completed' or next_status == 'failed'
    elseif current_status == 'running' then
        valid = next_status == 'running' or next_status == 'completed' or next_status == 'failed'
    elseif current_status == 'completed' then
        valid = next_status == 'completed'
    elseif current_status == 'failed' then
        valid = next_status == 'failed'
    end
    if not valid then
        return -1
    end
end

local fields = {'updated_at', ARGV[9]}
if ARGV[1] == '1' then
    table.insert(fields, 'status')
    table.insert(fields, ARGV[2])
end
if ARGV[3] == '1' then
    table.insert(fields, 'message')
    table.insert(fields, ARGV[4])
end
if ARGV[5] == '1' then
    table.insert(fields, 'result_json')
    table.insert(fields, ARGV[6])
end
if ARGV[7] == '1' then
    table.insert(fields, 'error_code')
    table.insert(fields, ARGV[8])
end

redis.call('HSET', KEYS[1], unpack(fields))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[10]))
return 1
"""


class KnowledgeJobNotFound(Exception):
    pass


class KnowledgeJobStoreUnavailable(Exception):
    pass


class KnowledgeJobInvalidTransition(Exception):
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
        allowed = {"status", "message", "result", "error_code"}
        unexpected = set(changes) - allowed
        if unexpected:
            raise ValueError("Unsupported knowledge job field")
        if "result" in changes:
            changes["result"] = _sanitize_result(changes["result"])
        client = self._require_client()
        now = datetime.now(timezone.utc)
        result_json = ""
        if changes.get("result") is not None:
            result_json = json.dumps(
                changes["result"],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        args = (
            "1" if "status" in changes else "0",
            str(changes.get("status") or ""),
            "1" if "message" in changes else "0",
            str(changes.get("message") or ""),
            "1" if "result" in changes else "0",
            result_json,
            "1" if "error_code" in changes else "0",
            str(changes.get("error_code") or ""),
            now.isoformat(),
            str(self.ttl_seconds),
        )
        try:
            result = int(client.eval(UPDATE_JOB_SCRIPT, 1, self._key(job_id), *args))
        except Exception as exc:
            self._raise_unavailable("update", exc)
        if result == 0:
            raise KnowledgeJobNotFound("Knowledge ingest job not found")
        if result == -1:
            raise KnowledgeJobInvalidTransition("Knowledge job status transition is invalid")
        if result != 1:
            raise KnowledgeJobStoreUnavailable("Knowledge job store is unavailable")
        return self.get(job_id)

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
        try:
            normalized = {
                _decode_redis_value(key): _decode_redis_value(value)
                for key, value in raw.items()
            }
            result_json = normalized.get("result_json", "")
            result = json.loads(result_json) if result_json else None
            if result is not None and not isinstance(result, dict):
                raise ValueError("Invalid result payload")
            result = _sanitize_result(result)
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
        except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
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


def _sanitize_result(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if not isinstance(result, dict):
        return {}
    safe: dict[str, Any] = {}
    for field in ("doc_id", "document_id"):
        value = result.get(field)
        if isinstance(value, str) and value:
            safe[field] = value
    chunks_added = result.get("chunks_added")
    if isinstance(chunks_added, int) and not isinstance(chunks_added, bool) and chunks_added >= 0:
        safe["chunks_added"] = chunks_added
    return safe
