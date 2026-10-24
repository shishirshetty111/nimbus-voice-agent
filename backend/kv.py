"""Shared key-value store for state that must survive across server instances (carts, conversations).

In production on Vercel several copies of the server may run at once, and a request can land on any
of them, so carts and conversation memory live in Upstash Redis. Locally nothing is configured and
callers keep their own in-process / file storage instead (client() returns None).

Upstash is used over its REST API (one HTTPS request per command) through the shared httpx client,
so there is no extra dependency and no connection pool to manage in a serverless function.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger("nimbus")

# Names set by the Vercel Marketplace Upstash integration, then Upstash's own names.
_ENV = [("KV_REST_API_URL", "KV_REST_API_TOKEN"), ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")]


class RedisKV:
    def __init__(self, url: str, token: str):
        self.url, self.headers = url.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def _cmd(self, *args):
        from backend import clients

        r = clients.http().post(self.url, headers=self.headers, json=[str(a) for a in args], timeout=5)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"Redis: {body['error']}")
        return body.get("result")

    def get_json(self, key: str):
        raw = self._cmd("GET", key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value, ttl_s: int) -> None:
        self._cmd("SET", key, json.dumps(value, ensure_ascii=False), "EX", ttl_s)

    def push_json(self, key: str, value, ttl_s: int) -> None:
        self._cmd("RPUSH", key, json.dumps(value, ensure_ascii=False))
        self._cmd("EXPIRE", key, ttl_s)

    def delete(self, key: str) -> None:
        self._cmd("DEL", key)


@lru_cache(maxsize=1)
def client() -> RedisKV | None:
    for url_var, token_var in _ENV:
        url, token = os.getenv(url_var), os.getenv(token_var)
        if url and token:
            log.warning("shared state: Upstash Redis (%s)", url_var)
            return RedisKV(url, token)
    return None
