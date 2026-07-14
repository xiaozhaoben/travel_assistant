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


def test_pending_anonymous_merges_are_partitioned_by_target_user():
    session_source = _read("services/authSession.ts")
    auth_source = _read("services/auth.ts")

    for function_name in (
        "stageCurrentAnonymousForMerge",
        "getPendingAnonymousTokens",
        "clearMergedAnonymousToken",
        "hasPendingAnonymousMerge",
    ):
        assert re.search(
            rf"function\s+{function_name}\s*\(\s*targetUserId\s*:\s*string",
            session_source,
        )
    assert re.search(r"pendingAnonymous\w*Key\s*\(\s*targetUserId", session_source)
    assert re.search(r"stageCurrentAnonymousForMerge\(result\.user_id\)", auth_source)
    assert re.search(r"getPendingAnonymousTokens\(targetUserId\)", auth_source)
    assert re.search(r"clearMergedAnonymousToken\(targetUserId,\s*anonymousToken\)", auth_source)
    assert re.search(r"expires_at\s*>\s*Date\.now\(\)\s*\+\s*30_000", session_source)


def test_auth_reactive_state_subscribes_to_storage_session_changes():
    session_source = _read("services/authSession.ts")
    auth_source = _read("services/auth.ts")

    assert "subscribeToAuthSession" in session_source
    assert "notifyAuthSessionChanged" in session_source
    assert "subscribeToAuthSession" in auth_source
    assert re.search(r"token\.value\s*=\s*getStoredUserToken\(\)", auth_source)
    assert re.search(r"user\.value\s*=\s*getStoredUser\(\)", auth_source)


def test_logout_anonymous_issue_is_fail_soft_and_jwt_payload_is_padded():
    auth_source = _read("services/auth.ts")
    session_source = _read("services/authSession.ts")

    logout_match = re.search(
        r"async\s+function\s+logout\b(?P<body>.*?)(?=\n\s*async\s+function\s+)",
        auth_source,
        re.DOTALL,
    )
    assert logout_match is not None
    assert "void ensureAnonymousToken().catch" in logout_match.group("body")
    assert ".padEnd(" in session_source


def test_anonymous_bootstrap_does_not_block_render_or_overwrite_a_logged_in_user():
    main_source = _read("main.ts")
    session_source = _read("services/authSession.ts")

    assert "await ensureAnonymousToken()" not in main_source
    assert "void ensureAnonymousToken().catch" in main_source
    assert main_source.index("ensureAnonymousToken()") < main_source.index("app.mount('#app')")
    assert re.search(
        r"const currentUserToken = getStoredUserToken\(\).*?"
        r"if \(currentUserToken && getStoredUser\(\)\) return currentUserToken.*?"
        r"storageSet\(ANONYMOUS_PRINCIPAL_KEY",
        session_source,
        re.DOTALL,
    )


def test_auth_races_and_sse_unauthorized_response_are_recoverable():
    auth_source = _read("services/auth.ts")
    api_source = _read("services/api.ts")
    logout_body = re.search(
        r"async\s+function\s+logout\b(?P<body>.*?)(?=\n\s*async\s+function\s+)",
        auth_source,
        re.DOTALL,
    )

    assert logout_body is not None
    assert "await ensureAnonymousToken()" not in logout_body.group("body")
    assert "void ensureAnonymousToken().catch" in logout_body.group("body")
    assert "mergeRerunRequested" in auth_source
    assert re.search(r"while\s*\(true\).*?getPendingAnonymousTokens\(targetUserId\)", auth_source, re.DOTALL)
    assert re.search(r"response\.status === 401\).*?invalidateBearerToken", api_source)


def test_browser_storage_and_malformed_jwt_fail_closed():
    session_source = _read("services/authSession.ts")

    assert "function storageGet" in session_source
    assert "function storageSet" in session_source
    assert "function storageRemove" in session_source
    assert re.search(
        r"typeof parsed\.exp !== 'number'\s*\|\|\s*parsed\.exp \* 1000 <=",
        session_source,
    )
