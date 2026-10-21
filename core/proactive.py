"""私聊主动发消息。

让她不只是"你问一句我答一句"，而是会主动找你。

策略（可在 config/bot.yaml 的 proactive 段调整）：
  - 只在私聊、且对方最近有过互动的前提下主动（不做冷启动骚扰）
  - 静默超过 min_gap 分钟后才有资格触发，且每次触发后重置计时
  - 一天最多 max_per_day 条
  - 只在 wake_hours 时间窗内发（默认 9:00~23:30）
  - 深夜/上班时间不打扰
  - 内容由 LLM 结合记忆+情绪+距上次聊天时长生成，不是模板

实现：
  - ProactiveScheduler 每 60 秒扫一遍所有私聊会话
  - 命中条件就调用 bot 的生成函数，拿到文本后经 OneBot 发出
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TICK = 60  # 扫描间隔（秒）


@dataclass
class PeerState:
    """一个私聊对象的主动消息状态。"""
    last_active: float = 0.0        # 对方最后一次发消息
    last_proactive: float = 0.0     # 我们最后一次主动
    sent_today: int = 0
    day: str = ""                   # YYYY-MM-DD，用于跨天清零
    decline: int = 0                # 连续被"敷衍"次数，用于降低频率


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class ProactiveScheduler:
    def __init__(self, bot: Any, cfg: Any):
        self.bot = bot
        self.cfg = cfg
        self.peers: Dict[int, PeerState] = {}
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    # ---------- 配置读取（都可缺省） ----------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "proactive_enabled", True))

    @property
    def min_gap(self) -> float:
        return float(getattr(self.cfg, "proactive_min_gap_min", 90)) * 60

    @property
    def max_per_day(self) -> int:
        return int(getattr(self.cfg, "proactive_max_per_day", 4))

    @property
    def wake_from(self) -> int:
        return int(getattr(self.cfg, "proactive_wake_from", 9))

    @property
    def wake_to(self) -> int:
        return int(getattr(self.cfg, "proactive_wake_to", 23))

    # ---------- 记录互动 ----------
    def note_incoming(self, uid: int) -> None:
        st = self.peers.setdefault(uid, PeerState())
        st.last_active = time.time()
        st.decline = 0

    def note_replied(self, uid: int) -> None:
        st = self.peers.setdefault(uid, PeerState())
        st.last_active = time.time()

    # ---------- 判定 ----------
    def _in_window(self, now: float) -> bool:
        h = int(time.localtime(now).tm_hour)
        a, b = self.wake_from, self.wake_to
        if a <= b:
            return a <= h < b
        return h >= a or h < b   # 跨零点

    def should_ping(self, uid: int, now: Optional[float] = None) -> bool:
        """这个私聊现在该不该主动发一条？"""
        if not self.enabled:
            return False
        now = now or time.time()
        st = self.peers.get(uid)
        if st is None or st.last_active <= 0:
            return False                      # 从没聊过，不主动
        if not self._in_window(now):
            return False
        today = _today()
        if st.day != today:
            st.day = today
            st.sent_today = 0
        if st.sent_today >= self.max_per_day:
            return False
        # 距上次互动（对方或我们）都要够久
        anchor = max(st.last_active, st.last_proactive)
        if now - anchor < self.min_gap:
            return False
        # 连续敷衍时把间隔拉长
        if st.decline >= 2 and now - anchor < self.min_gap * 2:
            return False
        return True

    def mark_sent(self, uid: int) -> None:
        st = self.peers.setdefault(uid, PeerState())
        st.last_proactive = time.time()
        st.sent_today += 1
        st.day = _today()

    # ---------- 主循环 ----------
    async def run_forever(self) -> None:
        await asyncio.sleep(20)
        while not self._stop:
            try:
                if self.enabled:
                    now = time.time()
                    for uid in list(self.peers.keys()):
                        if self._stop:
                            break
                        if not self.should_ping(uid, now):
                            continue
                        await self._ping(uid)
                        # 每条之间错开，避免同时轰炸
                        await asyncio.sleep(random.uniform(3, 9))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                try:
                    from bot import log  # type: ignore
                    log.warning("[主动] 循环异常: %s", exc)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(TICK)

    async def _ping(self, uid: int) -> None:
        try:
            text = await self.bot.compose_proactive(uid)
        except Exception as exc:  # noqa: BLE001
            try:
                from bot import log  # type: ignore
                log.warning("[主动] 生成失败: %s", exc)
            except Exception:  # noqa: BLE001
                pass
            return
        if not text:
            return
        client = getattr(self.bot, "client", None)
        if client is None:
            return
        try:
            await client.send_private_msg(uid, text)
            self.mark_sent(uid)
            try:
                from bot import log  # type: ignore
                log.info("[主动] 已私聊 %s: %s", uid, text[:40])
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                from bot import log  # type: ignore
                log.warning("[主动] 发送失败: %s", exc)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> asyncio.Task:
        self._task = asyncio.ensure_future(self.run_forever())
        return self._task

    def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()

    def status_text(self) -> str:
        if not self.enabled:
            return "主动发消息：已关闭"
        lines = [
            f"主动发消息：开启 | 静默 {int(self.min_gap // 60)} 分钟后可触发 | "
            f"每日上限 {self.max_per_day} 条 | 时段 {self.wake_from}:00~{self.wake_to}:00",
        ]
        if self.peers:
            for uid, st in list(self.peers.items())[:10]:
                idle = int((time.time() - max(st.last_active, st.last_proactive)) // 60)
                lines.append(f"- {uid}: 静默 {idle} 分钟, 今日已发 {st.sent_today}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
PROACTIVE_INSTRUCTION = """【现在是你主动找他】

不是他问你答，是你想起他了，主动发一条消息。

要求：
- 一句话到两句话，别写成小作文
- 结合你记得的关于他的事、上次聊到哪儿、现在几点
- 可以是想起来问一句、分享个小情绪、吐槽、催他吃饭睡觉、翻旧账、撒娇、发个梗
- 不要每句都问"在干嘛"；不要像客服问候；不要连发三条
- 情绪状态如果还没缓和，就别突然甜
- 只输出你要发的话，不要加引号、不要解释、不要写"（笑）"之外的动作描述
"""


def build_proactive_prompt(base_sys: str, idle_minutes: int, hour: int,
                           memory_ctx: str = "", last_user_msg: str = "",
                           mood_note: str = "") -> str:
    parts = [base_sys]
    if memory_ctx:
        parts.append(memory_ctx)
    if mood_note:
        parts.append(mood_note)
    ctx = [f"现在时间：{hour} 点", f"距上次聊天：约 {idle_minutes} 分钟"]
    if last_user_msg:
        ctx.append(f"他上次说的是：{last_user_msg[:120]}")
    parts.append("【情境】\n" + "\n".join(ctx))
    parts.append(PROACTIVE_INSTRUCTION)
    return "\n\n".join(p for p in parts if p)
