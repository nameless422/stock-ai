from __future__ import annotations

import threading
from typing import Optional

import httpx


_thread_local = threading.local()


def get_sync_http_client(
    *,
    timeout: float = 15.0,
    user_agent: str = "Mozilla/5.0",
    referer: Optional[str] = None,
    follow_redirects: bool = True,
) -> httpx.Client:
    key = (timeout, user_agent, referer, follow_redirects)
    clients = getattr(_thread_local, "clients", None)
    if clients is None:
        clients = {}
        _thread_local.clients = clients

    client = clients.get(key)
    if client is None:
        headers = {"User-Agent": user_agent}
        if referer:
            headers["Referer"] = referer
        client = httpx.Client(
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )
        clients[key] = client
    return client
