from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging

from app.core.api_errors import api_error


logger = logging.getLogger(__name__)

FIXED_WINDOW_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {current, redis.call('TTL', KEYS[1])}
""".strip()


@dataclass(frozen=True)
class Policy:
    name: str
    limit: int
    window_seconds: int
    fail_open: bool


class RateLimiter:
    def __init__(self, client, *, enabled: bool):
        self.client = client
        self.enabled = enabled

    def enforce(self, policy: Policy, subject: str) -> int:
        if not self.enabled:
            return policy.limit
        if self.client is None:
            return self._redis_unavailable(policy, "MissingClient")

        key = self._key(policy, subject)
        try:
            current, ttl = self.client.eval(
                FIXED_WINDOW_SCRIPT,
                1,
                key,
                policy.window_seconds,
            )
            current = int(current)
            retry_after = max(1, int(ttl))
        except Exception as exc:
            return self._redis_unavailable(policy, type(exc).__name__)

        if current > policy.limit:
            error = api_error(429, "RATE_LIMITED", "请求过于频繁，请稍后重试")
            error.headers = {"Retry-After": str(retry_after)}
            raise error
        return max(0, policy.limit - current)

    @staticmethod
    def _key(policy: Policy, subject: str) -> str:
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        return f"rate_limit:{policy.name}:{subject_hash}"

    @staticmethod
    def _redis_unavailable(policy: Policy, exception_type: str) -> int:
        logger.warning(
            "Redis rate limit check failed policy=%s exception_type=%s",
            policy.name,
            exception_type,
        )
        if policy.fail_open:
            return policy.limit
        raise api_error(503, "REDIS_UNAVAILABLE", "限流服务暂时不可用")


def create_rate_limit_policies(settings) -> dict[str, Policy]:
    window = settings.rate_limit_window_seconds
    return {
        "anonymous_issue": Policy("anonymous_issue", settings.rate_limit_anonymous_issue_limit, window, False),
        "register": Policy("register", settings.rate_limit_register_limit, window, False),
        "login": Policy("login", settings.rate_limit_login_limit, window, False),
        "qa": Policy("qa", settings.rate_limit_qa_limit, window, True),
        "planning": Policy("planning", settings.rate_limit_planning_limit, window, True),
        "map": Policy("map", settings.rate_limit_map_limit, window, True),
        "knowledge_read": Policy("knowledge_read", settings.rate_limit_knowledge_read_limit, window, False),
        "knowledge_write": Policy("knowledge_write", settings.rate_limit_knowledge_write_limit, window, False),
    }
