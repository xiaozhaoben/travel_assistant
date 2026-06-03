from __future__ import annotations

MCP_STDIO_CLEANUP_DELAY_SECONDS = 0.1


def is_broken_resource_cleanup_error(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "BrokenResourceError":
        return True
    nested = getattr(exc, "exceptions", None)
    if not nested:
        return False
    return any(is_broken_resource_cleanup_error(item) for item in nested)


async def wait_for_stdio_transport_cleanup(anyio_module) -> None:
    await anyio_module.sleep(MCP_STDIO_CLEANUP_DELAY_SECONDS)
