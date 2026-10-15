"""按代理出口 IP 解析 IANA 时区，让请求体里的时区与实际出口一致。

结果按代理 URL 缓存；查询失败回退默认时区并短暂缓存，避免出口服务异常时拖慢每个请求。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from curl_cffi import requests

from utils.log import logger

DEFAULT_TIMEZONE = "Asia/Shanghai"
LOOKUP_TIMEOUT_SECONDS = 6
CACHE_SECONDS = 6 * 3600
FAILURE_CACHE_SECONDS = 60
# (URL, 响应中的时区字段)；第一个失败时试下一个。
LOOKUP_SERVICES = (
    ("https://ipinfo.io/json", "timezone"),
    ("https://ipapi.co/json/", "timezone"),
)

_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_cache: dict[str, tuple[str, float]] = {}


def _valid_timezone(name: object) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    try:
        ZoneInfo(value)
    except Exception:  # noqa: BLE001
        return ""
    return value


def _lookup(proxy_url: str) -> str:
    for url, field in LOOKUP_SERVICES:
        try:
            response = requests.get(url, proxy=proxy_url or None, timeout=LOOKUP_TIMEOUT_SECONDS)
            if response.status_code == 200 and (name := _valid_timezone((response.json() or {}).get(field))):
                return name
        except Exception as exc:  # noqa: BLE001
            logger.warning({"event": "egress_timezone_lookup_failed", "service": url, "error": str(exc)[:160]})
    return ""


def egress_timezone(proxy_url: str = "") -> str:
    """返回代理（空串表示直连）出口所在的 IANA 时区名，查不到时为 DEFAULT_TIMEZONE。"""
    key = str(proxy_url or "")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _lock:
            hit = _cache.get(key)
            if hit and hit[1] > time.monotonic():
                return hit[0]
        name = _lookup(key)
        with _lock:
            _cache[key] = (
                name or DEFAULT_TIMEZONE,
                time.monotonic() + (CACHE_SECONDS if name else FAILURE_CACHE_SECONDS),
            )
            return _cache[key][0]


def timezone_offset_min(name: str, now: datetime | None = None) -> int:
    """与浏览器 `Date#getTimezoneOffset` 同号：UTC+8 为 -480，UTC-5 为 300。"""
    offset = (now or datetime.now(ZoneInfo(name))).astimezone(ZoneInfo(name)).utcoffset()
    return -int((offset.total_seconds() if offset else 0) // 60)
