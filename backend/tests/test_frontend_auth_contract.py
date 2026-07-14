from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = ROOT / "frontend" / "src"


def _read(relative_path: str) -> str:
    return (FRONTEND_SRC / relative_path).read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    match = re.search(
        rf"export\s+async\s+function\s+{re.escape(function_name)}\b(?P<body>.*?)(?=\nexport\s+(?:async\s+)?function\s+|\nexport\s+default\b|\Z)",
        source,
        re.DOTALL,
    )
    assert match is not None, f"missing exported function {function_name}"
    return match.group("body")


def test_frontend_never_creates_or_sends_client_selected_principal_ids():
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FRONTEND_SRC.rglob("*")
        if path.suffix in {".ts", ".vue"}
    )
    assert "travel_qa_anonymous_id" not in sources
    assert "crypto.randomUUID" not in sources

    api_source = _read("services/api.ts")
    for function_name in (
        "askTravelQuestion",
        "streamTravelQuestion",
        "listQAConversations",
    ):
        function_body = _function_body(api_source, function_name)
        assert "anonymous_id" not in function_body
        assert "user_id" not in function_body


def test_anonymous_token_bootstrap_is_server_issued_shared_and_retryable():
    session_source = _read("services/authSession.ts")
    api_source = _read("services/api.ts")

    assert "PrincipalTokenResponse" in api_source
    assert "'/api/auth/anonymous'" in api_source
    assert re.search(r"anonymous\w*Promise\s*:\s*Promise", session_source)
    assert re.search(r"if\s*\(anonymous\w*Promise\)", session_source)
    assert "finally" in session_source
    assert re.search(r"anonymous\w*Promise\s*=\s*null", session_source)
    assert "resolveBearerToken" in session_source


def test_protected_axios_and_sse_share_the_same_bearer_resolver():
    api_source = _read("services/api.ts")
    stream_body = _function_body(api_source, "streamTravelQuestion")

    assert re.search(r"interceptors\.request\.use\(async", api_source)
    assert "resolveBearerToken" in api_source
    assert "resolveBearerToken" in stream_body
    assert re.search(r"Authorization\s*[:=]", stream_body)


def test_login_merge_uses_preserved_anonymous_token_header_without_identity_body():
    auth_source = _read("services/auth.ts")
    api_source = _read("services/api.ts")
    merge_body = _function_body(api_source, "mergeAnonymousSessions")

    assert "X-Anonymous-Token" in merge_body
    assert "Authorization" in merge_body
    assert "anonymous_id" not in merge_body
    assert "anonymousMergePending" in auth_source
    assert re.search(r"clear\w*Anonymous\w*\(", auth_source)


def test_router_guest_and_knowledge_access_are_based_on_user_principal():
    main_source = _read("main.ts")

    assert "hasStoredUserPrincipal" in main_source
    assert re.search(r"/knowledge.*requiresUser", main_source)
    assert "travel_auth_token" not in main_source
