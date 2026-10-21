"""RSS / Atom 订阅推送。

能力：
  - 读 config/rss.yaml 里的订阅源
  - 周期性拉取，解析 RSS 2.0 / Atom / RDF
  - 新条目按规则推送到指定 QQ 群或私聊
  - 保留已推送记录，重启不重复刷屏
  - 零第三方依赖（只用标准库 xml.etree + urllib）

用法（在 bot 里）：
    feed = RssWatcher(cfg)
    await feed.run_forever(client)   # 作为后台任务启动
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from urllib.request import ProxyHandler, build_opener

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TIMEOUT = 15
MAX_PER_FEED = 5          # 一次最多推几条，防止刷屏
SEEN_KEEP = 800           # 每个源保留多少条已推记录


@dataclass
class Item:
    id: str
    title: str
    link: str
    summary: str = ""
    published: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "link": self.link,
                "summary": self.summary[:400], "published": self.published}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Item":
        return cls(id=str(d.get("id", "")), title=str(d.get("title", "")),
                   link=str(d.get("link", "")), summary=str(d.get("summary", "")),
                   published=float(d.get("published") or 0))


@dataclass
class Feed:
    name: str
    url: str
    targets: List[Dict[str, Any]] = field(default_factory=list)  # [{type:group|private, id:123}]
    interval: int = 900          # 秒
    max_items: int = 3
    enabled: bool = True
    template: str = ""           # 可选自定义推送模板
    last_fetch: float = 0.0
    seen: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return html.unescape("".join(el.itertext()).strip())


def _strip_tags(s: str, limit: int = 260) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _parse_date(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip()
    fmts = (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    for f in fmts:
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, f).timestamp()
        except Exception:  # noqa: BLE001
            continue
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def parse_feed(xml_bytes: bytes, base_url: str = "") -> List[Item]:
    """解析 RSS2.0 / Atom / RDF，返回条目列表（不保证已排序）。"""
    root = ET.fromstring(xml_bytes)
    tag = root.tag.split("}")[-1].lower()
    items: List[Item] = []

    def _find(el: ET.Element, *names: str) -> Optional[ET.Element]:
        for n in names:
            for child in el:
                if child.tag.split("}")[-1].lower() == n:
                    return child
        return None

    def _link_of(el: ET.Element) -> str:
        # Atom: <link href="..."/>
        for child in el:
            if child.tag.split("}")[-1].lower() == "link":
                href = child.get("href")
                rel = (child.get("rel") or "alternate").lower()
                if href and rel in ("alternate", ""):
                    return href
        t = _text(_find(el, "link"))
        return t

    containers = []
    if tag in ("rss", "rdf"):
        for ch in root.iter():
            if ch.tag.split("}")[-1].lower() == "item":
                containers.append(ch)
    else:  # atom / feed
        for ch in root.iter():
            if ch.tag.split("}")[-1].lower() == "entry":
                containers.append(ch)

    for el in containers:
        title = _strip_tags(_text(_find(el, "title")), 200)
        link = _link_of(el)
        if link and base_url and link.startswith("/"):
            p = urlparse(base_url)
            link = f"{p.scheme}://{p.netloc}{link}"
        guid = _text(_find(el, "guid", "id")) or link or title
        summary = _strip_tags(_text(_find(el, "description", "summary", "content", "encoded")))
        pub = _text(_find(el, "pubdate", "published", "updated", "date"))
        items.append(Item(id=guid, title=title or "(无标题)", link=link,
                          summary=summary, published=_parse_date(pub)))
    return items


def fetch(url: str, proxy: str = "") -> bytes:
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    opener = build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with opener.open(req, timeout=TIMEOUT) as r:
        return r.read()


# --------------------------------------------------------------------------- #
# 主逻辑
# --------------------------------------------------------------------------- #
class RssWatcher:
    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.feeds: List[Feed] = []
        self.state_path = self._state_path()
        self._dirty = False
        self._stop = False
        self.load_feeds()
        self.load_state()

    # ---------- 路径 ----------
    def _config_path(self) -> Path:
        try:
            from bot import _resolve  # type: ignore
            return _resolve(str(getattr(self.cfg, "rss_file", "rss.yaml")))
        except Exception:  # noqa: BLE001
            return Path(getattr(self.cfg, "rss_file", "rss.yaml"))

    def _state_path(self) -> Path:
        p = self._config_path()
        return p.parent / ".rss-state.json"

    # ---------- 配置 ----------
    def load_feeds(self) -> None:
        path = self._config_path()
        if not path.exists():
            return
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return
        out: List[Feed] = []
        for raw in (data.get("feeds") or []):
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            targets = []
            for t in (raw.get("to") or []):
                if isinstance(t, str):
                    # "g123456" / "p789" 简写
                    if t[:1].lower() == "g":
                        targets.append({"type": "group", "id": int(t[1:])})
                    elif t[:1].lower() == "p":
                        targets.append({"type": "private", "id": int(t[1:])})
                elif isinstance(t, dict) and t.get("id"):
                    targets.append({"type": t.get("type", "group"), "id": int(t["id"])})
            out.append(Feed(
                name=str(raw.get("name") or urlparse(raw["url"]).netloc),
                url=str(raw["url"]),
                targets=targets,
                interval=max(120, int(raw.get("interval") or 900)),
                max_items=max(1, min(10, int(raw.get("max_items") or 3))),
                enabled=bool(raw.get("enabled", True)),
                template=str(raw.get("template") or ""),
            ))
        self.feeds = out

    # ---------- 状态 ----------
    def load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        by_url = {f.url: f for f in self.feeds}
        for url, st in (data.get("feeds") or {}).items():
            f = by_url.get(url)
            if not f:
                continue
            f.last_fetch = float(st.get("last_fetch") or 0)
            f.seen = [str(x) for x in (st.get("seen") or [])][-SEEN_KEEP:]

    def save_state(self) -> None:
        try:
            data = {"feeds": {
                f.url: {"last_fetch": f.last_fetch, "seen": f.seen[-SEEN_KEEP:]}
                for f in self.feeds
            }}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
            self._dirty = False
        except Exception:  # noqa: BLE001
            pass

    # ---------- 推送格式 ----------
    @staticmethod
    def format_push(feed: Feed, items: List[Item]) -> List[str]:
        """把条目渲染成消息文本（可能拆成多条）。"""
        out: List[str] = []
        for it in items:
            if feed.template:
                try:
                    out.append(feed.template.format(
                        feed=feed.name, title=it.title, link=it.link, summary=it.summary))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            lines = [f"📰 {feed.name}", it.title.strip()]
            if it.summary:
                lines.append(it.summary.strip()[:180])
            if it.link:
                lines.append(it.link)
            out.append("\n".join(lines))
        return out

    # ---------- 拉取 ----------
    async def check_one(self, feed: Feed) -> List[Item]:
        loop = asyncio.get_running_loop()
        proxy = getattr(self.cfg, "llm_proxy", "") or ""
        try:
            raw = await loop.run_in_executor(None, lambda: fetch(feed.url, proxy))
        except Exception as exc:  # noqa: BLE001
            log_msg = f"[RSS] {feed.name} 拉取失败: {exc}"
            try:
                from bot import log  # type: ignore
                log.warning(log_msg)
            except Exception:  # noqa: BLE001
                pass
            feed.last_fetch = time.time()
            return []

        feed.last_fetch = time.time()
        try:
            items = parse_feed(raw, feed.url)
        except Exception as exc:  # noqa: BLE001
            try:
                from bot import log  # type: ignore
                log.warning("[RSS] %s 解析失败: %s", feed.name, exc)
            except Exception:  # noqa: BLE001
                pass
            return []

        # 按发布时间倒序（没时间的按原序）
        items.sort(key=lambda i: i.published or 0, reverse=True)
        seen = set(feed.seen)
        fresh = [i for i in items if i.id not in seen]

        # 首次运行：只记录不推送，避免一次性刷屏
        if not feed.seen:
            feed.seen = [i.id for i in items[:SEEN_KEEP]]
            self._dirty = True
            try:
                from bot import log  # type: ignore
                log.info("[RSS] %s 首次订阅，记录 %d 条基线（不推送）", feed.name, len(feed.seen))
            except Exception:  # noqa: BLE001
                pass
            return []

        fresh = fresh[:feed.max_items]
        for i in fresh:
            feed.seen.append(i.id)
        feed.seen = feed.seen[-SEEN_KEEP:]
        if fresh:
            self._dirty = True
        return fresh

    async def run_forever(self, client: Any) -> None:
        """后台循环：到期的源就拉一次并推送。"""
        await asyncio.sleep(8)  # 等机器人连上
        while not self._stop:
            try:
                enabled = [f for f in self.feeds if f.enabled and f.targets]
                now = time.time()
                for feed in enabled:
                    if now - feed.last_fetch < feed.interval:
                        continue
                    items = await self.check_one(feed)
                    if not items:
                        continue
                    texts = self.format_push(feed, items)
                    for t in texts:
                        for tgt in feed.targets:
                            try:
                                if tgt["type"] == "group":
                                    await client.send_group_msg(int(tgt["id"]), t)
                                else:
                                    await client.send_private_msg(int(tgt["id"]), t)
                            except Exception as exc:  # noqa: BLE001
                                try:
                                    from bot import log  # type: ignore
                                    log.warning("[RSS] 推送失败: %s", exc)
                                except Exception:  # noqa: BLE001
                                    pass
                        await asyncio.sleep(1.2)
                    if self._dirty:
                        self.save_state()
                if self._dirty:
                    self.save_state()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                try:
                    from bot import log  # type: ignore
                    log.warning("[RSS] 循环异常: %s", exc)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._stop = True
        self.save_state()

    # ---------- 手动 ----------
    async def check_all_now(self, client: Any, push: bool = True) -> str:
        """立即检查全部源（管理指令用）。"""
        lines = []
        for feed in self.feeds:
            if not feed.enabled:
                continue
            items = await self.check_one(feed)
            if not items:
                lines.append(f"- {feed.name}: 无新条目")
                continue
            lines.append(f"- {feed.name}: {len(items)} 条新")
            if push:
                for t in self.format_push(feed, items):
                    for tgt in feed.targets:
                        try:
                            if tgt["type"] == "group":
                                await client.send_group_msg(int(tgt["id"]), t)
                            else:
                                await client.send_private_msg(int(tgt["id"]), t)
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(0.8)
        self.save_state()
        return "\n".join(lines) if lines else "没有启用中的订阅源。"

    def status_text(self) -> str:
        if not self.feeds:
            return "还没有配置 RSS 订阅。编辑 config/rss.yaml 添加 feeds。"
        lines = []
        for f in self.feeds:
            mark = "✅" if f.enabled else "⏸"
            nxt = max(0, int(f.interval - (time.time() - f.last_fetch))) if f.last_fetch else 0
            to = ", ".join(f"{t['type']}:{t['id']}" for t in f.targets) or "未设置推送目标"
            lines.append(f"{mark} {f.name} | 每 {f.interval // 60} 分钟 | "
                         f"{'下次 ' + str(nxt // 60) + ' 分钟后' if nxt else '待检查'} | {to}")
        return "\n".join(lines)
