# -*- coding: utf-8 -*-
"""版本与更新检查。

不依赖第三方库, 用标准库 urllib 查询 GitHub Releases API。

设计要点:
  - 静默失败: 网络不通、无 Release、限流等情况一律不打扰用户
  - 本地缓存: 一天最多查一次, 避免频繁请求
  - 手动检查: 设置页可强制刷新
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import ProxyHandler, build_opener

# 当前版本
CURRENT_VERSION = "1.3.0"

# 更新源 (可换成自己的仓库)
REPO = "tong174/qq-bot-console"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

CACHE_TTL = 86400  # 一天


def _cache_file(root: Path) -> Path:
    return root / "config" / ".update-check.json"


def _read_cache(root: Path) -> Dict[str, Any]:
    f = _cache_file(root)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(root: Path, data: Dict[str, Any]) -> None:
    try:
        f = _cache_file(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def parse_version(v: str) -> tuple:
    """把 v1.2.3 解析成 (1,2,3)。"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0, 0, 0)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def check_update(root: Path, force: bool = False) -> Dict[str, Any]:
    """检查更新。

    返回:
        {
          "current": "1.2.0",
          "latest": "1.3.0" | None,
          "has_update": bool,
          "url": str,
          "notes": str,
          "published": str,
          "error": str | None,
          "checked_at": float,
        }
    """
    result: Dict[str, Any] = {
        "current": CURRENT_VERSION,
        "latest": None,
        "has_update": False,
        "url": "",
        "notes": "",
        "published": "",
        "error": None,
        "checked_at": 0,
    }

    cache = _read_cache(root)
    now = time.time()

    # 用缓存
    if not force and cache and (now - cache.get("checked_at", 0)) < CACHE_TTL:
        result.update({k: v for k, v in cache.items() if k in result})
        return result

    # 请求 GitHub
    try:
        req = urllib.request.Request(
            API_URL,
            headers={
                "User-Agent": f"QQBotConsole/{CURRENT_VERSION}",
                "Accept": "application/vnd.github+json",
            },
        )
        # 走系统代理 (GitHub 在国内常需代理)
        op = build_opener()
        with op.open(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 仓库还没有 Release, 算正常情况
            result["error"] = "暂无发布版本"
        else:
            result["error"] = f"HTTP {exc.code}"
        result["checked_at"] = now
        _write_cache(root, {**result, "_failed": True})
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"网络不可用: {type(exc).__name__}"
        result["checked_at"] = now
        _write_cache(root, {**result, "_failed": True})
        return result

    latest = str(data.get("tag_name") or data.get("name") or "").lstrip("vV")
    result["latest"] = latest or None
    result["has_update"] = bool(latest) and is_newer(latest, CURRENT_VERSION)
    result["url"] = str(data.get("html_url") or "")
    result["notes"] = str(data.get("body") or "")[:2000]
    result["published"] = str(data.get("published_at") or "")[:10]
    result["checked_at"] = now
    _write_cache(root, result)
    return result


def get_version_info(root: Path) -> Dict[str, Any]:
    """返回版本信息 (含缓存的更新状态)。"""
    return check_update(root, force=False)
