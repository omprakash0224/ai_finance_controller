"""
backend/tools/cache.py
======================
Upstash Redis caching layer for the AI Finance Controller.

Architecture
------------
Upstash is a serverless Redis service with an HTTP REST API.  The official
`upstash-redis` Python SDK wraps this HTTP API so there is no persistent TCP
connection to manage — each call is a single HTTPS request.  This is ideal
for serverless / edge deployments (Neon + Upstash are both serverless).

Cache Keys & TTLs
-----------------
  qa:<hash>           — Q&A answer cache   (TTL: QA_CACHE_TTL_SECONDS, default 300 s)
  metrics:<view_name> — Summary view cache (TTL: METRICS_CACHE_TTL_SECONDS, default 60 s)
  daily:<date>        — Daily metrics      (TTL: 86400 s — 24 h, since past days are immutable)

Graceful Degradation
--------------------
If UPSTASH_REDIS_URL or UPSTASH_REDIS_TOKEN are not set, every cache operation
silently no-ops (get → None, set → None, flush → 0).  The Q&A agent continues
to work correctly — just without caching.

Configuration (backend/.env)
-----------------------------
  UPSTASH_REDIS_URL=https://<your-instance>.upstash.io
  UPSTASH_REDIS_TOKEN=<your-rest-token>
  QA_CACHE_TTL_SECONDS=300       # optional, default 300
  METRICS_CACHE_TTL_SECONDS=60   # optional, default 60
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Module-level lazy-initialised Redis client
_redis: Optional[Any] = None
_redis_available: bool = False


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def _get_client() -> Optional[Any]:
    """
    Return the Upstash Redis client, initialising it on first call.

    Returns None (and logs a warning once) if credentials are missing.
    """
    global _redis, _redis_available

    if _redis is not None:
        return _redis

    url   = os.getenv("UPSTASH_REDIS_URL", "").strip()
    token = os.getenv("UPSTASH_REDIS_TOKEN", "").strip()

    if not url or not token:
        logger.info(
            "Upstash Redis not configured (UPSTASH_REDIS_URL / UPSTASH_REDIS_TOKEN missing). "
            "Cache is disabled — Q&A will run without caching."
        )
        _redis_available = False
        _redis = None
        return None

    try:
        from upstash_redis import Redis  # type: ignore[import]
        _redis = Redis(url=url, token=token)
        _redis_available = True
        logger.info("Upstash Redis connected: %s", url.split("@")[-1])
        return _redis
    except ImportError:
        logger.warning(
            "upstash-redis package not installed. "
            "Run: pip install upstash-redis. Cache disabled."
        )
        _redis_available = False
        return None
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Upstash Redis init failed: %s. Cache disabled.", exc)
        _redis_available = False
        return None


def is_cache_available() -> bool:
    """Return True if Redis is configured and reachable."""
    return _redis_available and _get_client() is not None


# ---------------------------------------------------------------------------
# TTL constants (overridable via env vars)
# ---------------------------------------------------------------------------

def _qa_ttl() -> int:
    return int(os.getenv("QA_CACHE_TTL_SECONDS", "300"))

def _metrics_ttl() -> int:
    return int(os.getenv("METRICS_CACHE_TTL_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Cache key helpers
# ---------------------------------------------------------------------------

def qa_cache_key(question: str) -> str:
    """
    Derive a stable cache key from a Q&A question.

    Normalises: lowercase, strip whitespace, collapse runs of whitespace.
    This ensures 'What is the match rate?' and 'what is the match rate ?'
    map to the same key.
    """
    import re
    normalised = re.sub(r"\s+", " ", question.lower().strip())
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:16]
    return f"qa:{digest}"


def metrics_cache_key(view_name: str) -> str:
    """Cache key for a named pre-aggregated metrics view."""
    return f"metrics:{view_name}"


def daily_cache_key(date_str: str) -> str:
    """Cache key for an immutable past-day metrics snapshot."""
    return f"daily:{date_str}"


# ---------------------------------------------------------------------------
# Core get / set / delete operations
# ---------------------------------------------------------------------------

def get_cached(key: str) -> Optional[dict]:
    """
    Retrieve a cached value by key.

    Returns the deserialised dict, or None on cache miss / error / disabled.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)
    except Exception as exc:                                         # noqa: BLE001
        logger.debug("Cache GET failed for key '%s': %s", key, exc)
        return None


def set_cached(key: str, value: dict, ttl_seconds: int) -> bool:
    """
    Store a value in the cache with an expiry.

    Returns True on success, False on any error.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        client.setex(key, ttl_seconds, json.dumps(value, default=str))
        logger.debug("Cache SET key='%s' ttl=%ds", key, ttl_seconds)
        return True
    except Exception as exc:                                         # noqa: BLE001
        logger.debug("Cache SET failed for key '%s': %s", key, exc)
        return False


def delete_key(key: str) -> bool:
    """Delete a single cache key."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.delete(key)
        return True
    except Exception as exc:                                         # noqa: BLE001
        logger.debug("Cache DELETE failed for key '%s': %s", key, exc)
        return False


# ---------------------------------------------------------------------------
# Bulk invalidation helpers
# ---------------------------------------------------------------------------

def invalidate_prefix(prefix: str) -> int:
    """
    Delete all keys matching a given prefix pattern (prefix:*).

    Uses SCAN + DEL to avoid blocking the Redis server with KEYS.
    Returns the number of keys deleted.

    Note: Upstash REST API supports SCAN via the upstash-redis SDK.
    """
    client = _get_client()
    if client is None:
        return 0

    deleted = 0
    try:
        cursor = 0
        pattern = f"{prefix}:*"
        while True:
            cursor, keys = client.scan(cursor, match=pattern, count=100)
            if keys:
                client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        logger.info("Invalidated %d keys with prefix '%s'", deleted, prefix)
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Cache prefix invalidation failed for '%s': %s", prefix, exc)
    return deleted


def flush_all_qa_cache() -> int:
    """Delete all Q&A cached answers (qa:* keys)."""
    return invalidate_prefix("qa")


def flush_all_metrics_cache() -> int:
    """Delete all metrics cached summaries (metrics:* keys)."""
    return invalidate_prefix("metrics")


def flush_cache() -> dict[str, int]:
    """
    Flush all caches managed by this module.

    Returns counts of deleted keys per prefix for observability.
    """
    return {
        "qa_keys_deleted":      flush_all_qa_cache(),
        "metrics_keys_deleted": flush_all_metrics_cache(),
    }


# ---------------------------------------------------------------------------
# Convenience decorators / wrappers for common patterns
# ---------------------------------------------------------------------------

def cached_metrics(view_name: str, compute_fn, ttl_seconds: Optional[int] = None) -> dict:
    """
    Return a cached metrics view, computing and caching it on first call.

    Parameters
    ----------
    view_name  : unique name for this metric snapshot (used as cache key suffix)
    compute_fn : zero-argument callable that returns the dict to cache
    ttl_seconds: override the default METRICS_CACHE_TTL_SECONDS

    Example
    -------
    >>> result = cached_metrics("match_rate", lambda: _compute_match_rate())
    """
    key = metrics_cache_key(view_name)
    cached = get_cached(key)
    if cached is not None:
        logger.debug("Cache HIT: metrics/%s", view_name)
        return {**cached, "_cache": "hit"}

    value = compute_fn()
    set_cached(key, value, ttl_seconds or _metrics_ttl())
    logger.debug("Cache MISS: metrics/%s — computed and stored", view_name)
    return {**value, "_cache": "miss"}
