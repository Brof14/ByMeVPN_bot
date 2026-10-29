"""HTTP client factory for YooKassa requests with optional source-IP binding."""

import httpx

from config import YOOKASSA_SOURCE_IP


def yookassa_client(timeout: float) -> httpx.AsyncClient:
    """Build a client bound to the configured egress address, if provided."""
    transport = httpx.AsyncHTTPTransport(
        local_address=YOOKASSA_SOURCE_IP or None,
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout)
