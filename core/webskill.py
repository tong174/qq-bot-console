# -*- coding: utf-8 -*-
"""联网搜索能力。

给机器人一个轻量的联网工具：
  - 判断用户是否在问需要实时信息的问题
  - 用 DuckDuckGo / Bing 抓取摘要
  - 把结果拼成上下文喂给模型

设计取舍:
  - 不引入第三方依赖, 用标准库 urllib
  - 超时严格 (5s), 失败静默降级为"不联网"
  - 因为中文搜索源稳定性一般, 优先用 DuckDuckGo HTML 版
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Dict, List, Optional
from urllib.request import Request, build_opener, ProxyHandler

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT = 6


# --------------------------------------------------------------------------- #
# 触发判断: 什么时候该联网
# --------------------------------------------------------------------------- #
# 明显的实时性需求
_REALTIME_PATTERNS = [
    r"今天|现在|最新|最近|今年|本周|本月|刚才|刚刚",
    r"天气|气温|下雨|台风|雾霾",
    r"股价|汇率|油价|金价|房价|币价",
    r"新闻|发生了什么|怎么回事|热搜",
    r"比分|赛况|票房|排名",
    r"多少钱|价格|报价",
    r"谁是|是什么人|哪一年|多少岁",
    r"发布|上市|开售|发售",
]

# 明显不需要联网（纯聊天/情绪）
_NO_SEARCH_PATTERNS = [
    r"^在吗|^在不在|^你好|^嗨|^晚安|^早安|^早|^睡了",
    r"^哈哈+|^草|^笑死|^无语|^行吧|^好的|^嗯+|^哦",
    r"想你|爱你|喜欢你|抱抱|亲亲",
    r"^你是谁|你叫什么|你几岁",
]

# 搜索类查询特征（用户明确要查）
_SEARCH_KEYWORDS = [
    "搜索", "查一下", "查查", "帮我查", "百度一下", "谷歌",
    "搜一下", "找一下", "查资料",
]


def should_search(text: str) -> bool:
    """判断这句话是否需要联网。"""
    if not text or len(text) < 2:
        return False

    # 明确要求搜索
    for kw in _SEARCH_KEYWORDS:
        if kw in text:
            return True

    # 纯聊天不搜
    for pat in _NO_SEARCH_PATTERNS:
        if re.search(pat, text):
            return False

    # 实时性需求
    for pat in _REALTIME_PATTERNS:
        if re.search(pat, text):
            return True

    return False


# --------------------------------------------------------------------------- #
# 搜索实现
# --------------------------------------------------------------------------- #
def _fetch(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = TIMEOUT) -> str:
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    # 走系统代理（国内访问外网常需要）
    op = build_opener()
    with op.open(req, timeout=timeout) as r:
        raw = r.read()
    # 尝试常见编码
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def search_duckduckgo(query: str, limit: int = 4) -> List[Dict[str, str]]:
    """DuckDuckGo HTML 版搜索 (无需 API key)。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        page = _fetch(url)
    except Exception:  # noqa: BLE001
        return []

    results: List[Dict[str, str]] = []
    # 结果块: <a class="result__a" href="...">标题</a> ... <a class="result__snippet">摘要</a>
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
        page, re.S,
    ):
        href, title, snippet = m.group(1), m.group(2), m.group(3) or ""
        title = _strip_tags(title)
        snippet = _strip_tags(snippet)
        if not title:
            continue
        # DDG 的跳转链接
        if "uddg=" in href:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = qs.get("uddg", [href])[0]
            except Exception:  # noqa: BLE001
                pass
        results.append({"title": title, "url": href, "snippet": snippet[:280]})
        if len(results) >= limit:
            break
    return results


def search_bing(query: str, limit: int = 4) -> List[Dict[str, str]]:
    """Bing 搜索 (备用源)。"""
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-CN"
    try:
        page = _fetch(url)
    except Exception:  # noqa: BLE001
        return []

    results: List[Dict[str, str]] = []
    for m in re.finditer(
        r'<li class="b_algo".*?<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<p[^>]*>(.*?)</p>',
        page, re.S,
    ):
        href, title, snippet = m.group(1), _strip_tags(m.group(2)), _strip_tags(m.group(3))
        if title and href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet[:280]})
        if len(results) >= limit:
            break
    return results


def web_search(query: str, limit: int = 4) -> Dict[str, Any]:
    """主入口: 依次尝试多个源。"""
    query = query.strip()
    if not query:
        return {"ok": False, "query": "", "results": [], "source": ""}

    for name, fn in (("DuckDuckGo", search_duckduckgo), ("Bing", search_bing)):
        try:
            rs = fn(query, limit)
        except Exception:  # noqa: BLE001
            rs = []
        if rs:
            return {"ok": True, "query": query, "results": rs, "source": name}

    return {"ok": False, "query": query, "results": [], "source": ""}


# --------------------------------------------------------------------------- #
# 拼接给模型看的文本
# --------------------------------------------------------------------------- #
def build_search_context(result: Dict[str, Any], max_chars: int = 1200) -> str:
    """把搜索结果拼成提示词片段。"""
    if not result.get("ok"):
        return ""
    lines = [
        "【联网搜索结果】",
        f"查询：{result['query']}（来源：{result['source']}）",
    ]
    for i, r in enumerate(result["results"], 1):
        line = f"{i}. {r['title']}：{r['snippet']}"
        if sum(len(x) for x in lines) + len(line) > max_chars:
            break
        lines.append(line)
    lines.append(
        "回答要求：以上是刚查到的最新信息，请自然地融入回答，"
        "不要机械复述、不要说「根据搜索结果」。如果信息不完整就说不知道。"
    )
    return "\n".join(lines)


def extract_query(text: str) -> str:
    """从用户话里提取搜索关键词。"""
    q = text
    # 去掉这些前缀
    for p in ("帮我搜一下", "帮我查一下", "帮我查查", "搜索一下", "搜一下",
              "查一下", "查查", "百度一下", "谷歌一下", "帮我", "请", "你能"):
        q = q.replace(p, "")
    # 去掉 @xxx
    q = re.sub(r"@\d+", "", q)
    # 去掉多余标点
    q = re.sub(r"[？?！!。，,、\s]+", " ", q).strip()
    return q[:60]
