"""长期记忆 + 情绪状态模块。

解决两个问题:
  1. 短期记忆: 当前对话上下文 (原来就有, 但有 bug: 切人格会清空)
  2. 长期记忆: 跨会话记住关于用户的事实, 以及她自己的"情绪状态"

核心设计:
  - facts: 关于用户的具体事实 ("在互联网公司上班", "讨厌老板", "爱吃辣")
  - mood:  她的当前情绪 (开心/生气/委屈/困), 带强度与过期时间
  - interactions: 交互统计 (总次数, 最后互动时间, 好感度)

持久化到 memory/<session_key>.json, 重启不丢。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("qq-bot.memory")

# 情绪衰减: 情绪从产生到完全过期的时间 (秒)
MOOD_DECAY = {
    "生气": 3600,      # 生气 1 小时
    "委屈": 2400,      # 委屈 40 分钟
    "开心": 1800,
    "害羞": 900,
    "困": 1800,
    "难过": 2400,
    "吃醋": 1800,
}


@dataclass
class Fact:
    """关于用户的一条事实。"""
    text: str
    created: float = field(default_factory=time.time)
    hits: int = 0          # 被提到的次数


@dataclass
class Mood:
    name: str = ""         # 当前情绪名
    intensity: float = 0.0  # 0~1
    since: float = 0.0
    reason: str = ""       # 为什么会这样

    def decayed(self, now: float) -> "Mood":
        if not self.name or not self.since:
            return Mood()
        life = MOOD_DECAY.get(self.name, 1800)
        age = now - self.since
        if age >= life:
            return Mood()
        remain = 1 - age / life
        return Mood(self.name, self.intensity * remain, self.since, self.reason)


@dataclass
class SessionMemory:
    """一个会话 (群或私聊) 的记忆。"""
    facts: List[Fact] = field(default_factory=list)
    mood: Mood = field(default_factory=Mood)
    total_interactions: int = 0
    last_seen: float = 0.0
    last_user_msg: str = ""
    history: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionMemory":
        m = cls()
        m.facts = [Fact(**f) for f in d.get("facts", []) if isinstance(f, dict) and f.get("text")]
        md = d.get("mood") or {}
        m.mood = Mood(**{k: md[k] for k in ("name", "intensity", "since", "reason") if k in md})
        m.total_interactions = int(d.get("total_interactions", 0))
        m.last_seen = float(d.get("last_seen", 0))
        m.last_user_msg = str(d.get("last_user_msg", ""))
        m.history = [h for h in d.get("history", []) if isinstance(h, dict)]
        return m


class MemoryStore:
    """按会话保存记忆, 线程安全。"""

    def __init__(self, root: Path, max_facts: int = 60, max_history: int = 20):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_facts = max_facts
        self.max_history = max_history
        self._lock = threading.Lock()
        self._cache: Dict[str, SessionMemory] = {}

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^\w\-]", "_", key)
        return self.root / f"{safe}.json"

    def get(self, key: str) -> SessionMemory:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            p = self._path(key)
            if p.exists():
                try:
                    mem = SessionMemory.from_dict(json.loads(p.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    log.exception("读取记忆失败: %s, 重置", p)
                    mem = SessionMemory()
            else:
                mem = SessionMemory()
            self._cache[key] = mem
            return mem

    def save(self, key: str) -> None:
        with self._lock:
            mem = self._cache.get(key)
            if not mem:
                return
            p = self._path(key)
            try:
                tmp = p.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(mem.to_dict(), ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                tmp.replace(p)
            except Exception:  # noqa: BLE001
                log.exception("保存记忆失败: %s", p)

    # ---------------- 事实管理 ----------------
    def add_fact(self, key: str, text: str) -> bool:
        text = text.strip().rstrip("。.!！")
        if not text or len(text) > 40:
            return False
        mem = self.get(key)
        for f in mem.facts:
            if text in f.text or f.text in text:
                f.hits += 1
                return False
        mem.facts.append(Fact(text))
        if len(mem.facts) > self.max_facts:
            # 淘汰最少被提到且最老的
            mem.facts.sort(key=lambda f: (f.hits, f.created))
            mem.facts.pop(0)
        return True

    # ---------------- 情绪管理 ----------------
    def set_mood(self, key: str, name: str, intensity: float, reason: str = "") -> None:
        mem = self.get(key)
        mem.mood = Mood(name, max(0.2, min(1.0, intensity)), time.time(), reason)

    def clear_mood(self, key: str) -> None:
        self.get(key).mood = Mood()


# --------------------------------------------------------------------------- #
# 从用户消息里抽取"事实"
# --------------------------------------------------------------------------- #
# 只匹配陈述句式的自我披露, 避免把提问当事实
_FACT_PATTERNS = [
    # "我在XX上班" "我在XX工作" —— 取"在"到"上班/工作"之间的部分
    (re.compile(r"我在([^，。,.!？?！\s]{2,14}?)(?:上班|工作|实习)"),
     ("在{}上班",)),
    # "我是XX" (职业)
    (re.compile(r"我是(?:一[个名])?([^，。,.!？?！\s]{2,10}?)(?:。|，|,|$|的)"),
     ("是{}",)),
    # "我喜欢XX" "我爱吃XX" "我讨厌XX" "我受不了XX"
    (re.compile(r"我(喜欢|爱|讨厌|怕|受不了|不吃|爱吃|馋)([^，。,.!？?！]{1,12})"),
     None),
    # "我住在XX"
    (re.compile(r"我住(?:在)?([^，。,.!？?！]{2,15})"),
     ("住在{}",)),
    # "我叫XX"
    (re.compile(r"我(?:叫|名字是)([^，。,.!？?！]{1,10})"),
     ("叫{}",)),
    # "我今年XX岁"
    (re.compile(r"我今年(\d{1,2}岁)"), ("今年{}",)),
    # 生活状态
    (re.compile(r"我(在考研|在减肥|在找工作|在学[^，。,.!？?！]{1,8}|养了[^，。,.!？?！]{1,10})"),
     None),
]

# 明显不是事实的问句特征
_QUESTION_MARKERS = ("？", "?", "吗", "呢", "什么", "怎么", "为什么", "谁", "哪", "几")


def extract_facts(text: str) -> List[str]:
    """从一句话里抽取关于用户的事实。"""
    if not text or len(text) < 3:
        return []
    # 问句不提取
    if any(q in text for q in _QUESTION_MARKERS):
        return []

    out: List[str] = []
    for pattern, fmt in _FACT_PATTERNS:
        for m in pattern.finditer(text):
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            if fmt:
                try:
                    out.append(fmt[0].format(*groups))
                except (IndexError, KeyError):
                    out.append(text[m.start():m.end()])
            else:
                out.append(text[m.start():m.end()])
    # 去重、截断
    seen = set()
    clean = []
    for f in out:
        f = f.strip()
        if f and f not in seen and 3 <= len(f) <= 30:
            seen.add(f)
            clean.append(f)
    return clean[:3]


# --------------------------------------------------------------------------- #
# 从用户消息/回复里推断情绪
# --------------------------------------------------------------------------- #
_MOOD_RULES = [
    # (触发词, 情绪, 强度)
    (("滚", "烦死了", "别理我", "讨厌你", "生气了", "有病", "烦人", "闭嘴", "神经"), "生气", 0.85),
    (("你一点都不懂", "你根本", "好失望", "算了", "随便你", "无所谓"), "委屈", 0.7),
    (("你是AI", "你是机器人", "你不是真人", "假的"), "委屈", 0.4),
    (("好开心", "太好了", "哈哈", "笑死", "爽", "升职", "加薪", "赢了"), "开心", 0.8),
    (("晚安", "困了", "睡了", "好困", "睡不着"), "困", 0.6),
    (("难受", "难过", "想哭", "委屈", "崩溃", "好累", "心累"), "难过", 0.7),
    (("别的女生", "别人", "那个女的", "女同事", "前任", "闺蜜"), "吃醋", 0.6),
    (("宝宝", "亲爱的", "想你", "爱你", "喜欢你"), "害羞", 0.7),
]


def infer_mood(text: str) -> Optional[tuple]:
    """从用户消息推断她该有的情绪 (触发词, 情绪, 强度)。"""
    low = text.lower()
    for triggers, mood, inten in _MOOD_RULES:
        for t in triggers:
            if t in low or t in text:
                return mood, inten, t
    return None


def build_memory_prompt(mem: SessionMemory, now: Optional[float] = None) -> str:
    """把记忆渲染成给 LLM 的提示词片段。"""
    now = now or time.time()
    parts: List[str] = []

    # 情绪
    mood = mem.mood.decayed(now)
    if mood.name:
        strength = "很" if mood.intensity > 0.7 else ("有点" if mood.intensity > 0.4 else "稍微")
        reason = f"（因为他说了「{mood.reason}」）" if mood.reason else ""
        parts.append(
            f"【你现在的情绪】{strength}{mood.name}{reason}。"
            f"这个情绪要体现在语气里; 如果他在哄你, 可以慢慢缓和, 但不要一下子翻篇。"
        )
    else:
        parts.append("【你现在的情绪】平静, 心情正常。")

    # 事实
    if mem.facts:
        facts = [f.text for f in sorted(mem.facts, key=lambda f: -f.hits)[:12]]
        parts.append(
            "【你记得关于他的事】" + "；".join(facts) +
            "。聊到相关话题时可以自然提起来, 但不要生硬背诵。"
        )

    # 互动历史
    if mem.total_interactions > 0:
        gap = now - mem.last_seen if mem.last_seen else 0
        if gap > 86400 * 3:
            parts.append("【关系】你们已经好几天没聊了, 可以有点「怎么这么久才来」的小情绪。")
        elif gap > 3600 * 6:
            parts.append("【关系】你们隔了大半天才聊, 可以随口问一句去哪了。")

    return "\n".join(parts)
