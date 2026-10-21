"""群消息记忆与总结。

功能：
  - 记录群内所有消息（不管机器人是否回复），落到 memory/group_<gid>.json
  - 按时间窗口检索、按人聚合
  - 生成总结用的提示词上下文（交给 LLM 调用方去总结）

设计要点：
  - 只存必要字段，避免把图片/表情原始数据写进磁盘
  - 滚动保留最近 N 条 + 按天分桶，防止文件无限增长
  - 线程安全（机器人是 asyncio 单线程，但保存在内置线程池里跑）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_KEEP = 4000          # 每个群最多保留的条数
MAX_CONTENT = 300        # 单条消息内容截断长度


@dataclass
class GroupMsg:
    t: float                      # 时间戳
    uid: int                      # 发送者 QQ
    name: str                     # 昵称/群名片
    text: str                     # 文本
    has_image: bool = False
    msg_id: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "t": round(self.t, 1),
            "uid": self.uid,
            "name": self.name,
            "text": self.text,
            **({"img": 1} if self.has_image else {}),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GroupMsg":
        return cls(
            t=float(d.get("t") or 0),
            uid=int(d.get("uid") or 0),
            name=str(d.get("name") or ""),
            text=str(d.get("text") or ""),
            has_image=bool(d.get("img")),
        )


def _safe(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > MAX_CONTENT:
        text = text[:MAX_CONTENT] + "…"
    return text


class GroupMemory:
    """一个群的滚动消息库。"""

    def __init__(self, root: Path, gid: int, max_keep: int = MAX_KEEP):
        self.root = Path(root)
        self.gid = gid
        self.max_keep = max_keep
        self._msgs: List[GroupMsg] = []
        self._loaded = False
        self._path = self.root / f"group_{gid}.json"

    # ---------- 持久化 ----------
    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._msgs = [GroupMsg.from_dict(d) for d in raw.get("msgs", [])]
        except Exception:  # noqa: BLE001
            self._msgs = []

    def save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            data = {
                "gid": self.gid,
                "updated": round(time.time(), 1),
                "msgs": [m.to_dict() for m in self._msgs[-self.max_keep:]],
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 写入 ----------
    def add(self, uid: int, name: str, text: str, has_image: bool = False,
            msg_id: Any = None, now: Optional[float] = None) -> None:
        self._ensure()
        text = _safe(text)
        if not text and not has_image:
            return
        self._msgs.append(GroupMsg(
            t=now if now is not None else time.time(),
            uid=uid, name=name, text=text, has_image=has_image, msg_id=msg_id,
        ))
        if len(self._msgs) > self.max_keep + 500:
            self._msgs = self._msgs[-self.max_keep:]

    # ---------- 读取 ----------
    def count(self) -> int:
        self._ensure()
        return len(self._msgs)

    def recent(self, seconds: float = 3600, limit: int = 0) -> List[GroupMsg]:
        """最近 N 秒内的消息。limit>0 时只取最后 limit 条。"""
        self._ensure()
        since = time.time() - seconds
        out = [m for m in self._msgs if m.t >= since]
        if limit > 0:
            out = out[-limit:]
        return out

    def since(self, ts: float, limit: int = 0) -> List[GroupMsg]:
        self._ensure()
        out = [m for m in self._msgs if m.t >= ts]
        if limit > 0:
            out = out[-limit:]
        return out

    def last_seen(self) -> float:
        self._ensure()
        return self._msgs[-1].t if self._msgs else 0.0

    # ---------- 总结辅助 ----------
    def speakers(self, msgs: List[GroupMsg]) -> Dict[str, int]:
        """按人统计发言条数。"""
        stat: Dict[str, int] = {}
        for m in msgs:
            stat[m.name or str(m.uid)] = stat.get(m.name or str(m.uid), 0) + 1
        return dict(sorted(stat.items(), key=lambda kv: -kv[1]))

    def render(self, msgs: List[GroupMsg], max_chars: int = 6000) -> str:
        """把消息渲染成给模型看的对话记录。"""
        if not msgs:
            return ""
        lines: List[str] = []
        total = 0
        for m in msgs:
            hhmm = time.strftime("%H:%M", time.localtime(m.t))
            body = m.text or "（图片）"
            if m.has_image and m.text and "图片" not in m.text:
                body = m.text + "（含图片）"
            line = f"[{hhmm}] {m.name or m.uid}: {body}"
            if total + len(line) > max_chars:
                lines.append("……（后面还有消息，已省略）")
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    def stats_text(self, msgs: List[GroupMsg]) -> str:
        if not msgs:
            return "这段时间群里没人说话。"
        stat = self.speakers(msgs)
        top = list(stat.items())[:8]
        span = msgs[-1].t - msgs[0].t
        hours = max(1, round(span / 3600))
        head = f"共 {len(msgs)} 条消息，{len(stat)} 人参与，跨度约 {hours} 小时。"
        tail = "发言最多：" + "、".join(f"{n}({c}条)" for n, c in top)
        return head + tail

    def sweep_old(self, keep_days: int = 14) -> None:
        """清掉太久以前的消息。"""
        self._ensure()
        cutoff = time.time() - keep_days * 86400
        before = len(self._msgs)
        self._msgs = [m for m in self._msgs if m.t >= cutoff]
        if len(self._msgs) != before:
            self.save()


class GroupMemoryStore:
    """按群管理 GroupMemory。"""

    def __init__(self, root: Path, max_keep: int = MAX_KEEP):
        self.root = Path(root)
        self.max_keep = max_keep
        self._groups: Dict[int, GroupMemory] = {}

    def get(self, gid: int) -> GroupMemory:
        g = self._groups.get(gid)
        if g is None:
            g = GroupMemory(self.root, gid, self.max_keep)
            self._groups[gid] = g
        return g

    def save_all(self) -> None:
        for g in self._groups.values():
            g.save()


# --------------------------------------------------------------------------- #
# 总结提示词
# --------------------------------------------------------------------------- #
SUMMARY_INSTRUCTION = """请你总结上面这段群聊。要求：

1. 用聊天口吻写，像在跟群友复述，不要写成会议纪要
2. 结构：
   - 一句话说这段时间群里的主旋律
   - 3~6 条要点（谁说了什么/讨论了啥/有什么结论或梗）
   - 如果有人在问问题没人回答、或者有约定没落实，单独点出来
3. 保留具体的人名、数字、时间、链接等关键信息
4. 不要编造没出现的内容；信息不足就写「没聊出什么结论」
5. 控制在 300 字以内
"""


def build_summary_prompt(stat_text: str, rendered: str) -> str:
    parts = ["【群聊记录】"]
    if stat_text:
        parts.append(stat_text)
    parts.append(rendered)
    parts.append("")
    parts.append(SUMMARY_INSTRUCTION)
    return "\n".join(p for p in parts if p is not None)


_TIME_PATTERNS = [
    (re.compile(r"^\s*(?:总结|汇总|小结)\s*(?:一下)?\s*(?:今天|今日)\s*(?:的)?\s*(?:群|群聊|聊天)?"), 86400),
    (re.compile(r"^\s*(?:总结|汇总|小结)\s*(?:一下)?\s*(?:刚才|刚刚|刚才的)?"), 3600),
]


def parse_summary_request(text: str) -> Optional[float]:
    """识别"总结群消息"类指令，返回统计窗口秒数；不是这类指令返回 None。"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return None
    if "总结" not in t and "汇总" not in t and "小结" not in t:
        return None
    # 明确的时间窗
    if re.search(r"(今天|今日|整天|一天)", t):
        return 86400
    if re.search(r"(昨天)", t):
        return 172800
    if re.search(r"(\d+)\s*(小时|钟头)", t):
        m = re.search(r"(\d+)\s*(小时|钟头)", t)
        return max(60, int(m.group(1)) * 3600)
    if re.search(r"(\d+)\s*分钟", t):
        m = re.search(r"(\d+)\s*分钟", t)
        return max(60, int(m.group(1)) * 60)
    if re.search(r"(本周|这周|一周)", t):
        return 7 * 86400
    if re.search(r"(上午|早上|中午|下午|晚上|刚才|刚刚|最近|这段时间)", t):
        return 3 * 3600
    return 3600  # 默认最近一小时
