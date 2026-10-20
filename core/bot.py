"""QQ 聊天机器人 - 基于 NapCat (OneBot 11 反向 WebSocket) + OpenAI 兼容 LLM。

用法:
    python bot.py

首次运行前请:
  1. 安装依赖:  pip install -r requirements.txt
  2. 复制 config.example.yaml 为 config.yaml 并按注释填写
  3. 启动 NapCat, 在 WebUI 里新建一个 WebSocket 客户端,
     URL 填本程序配置里的 ws_url (默认 ws://127.0.0.1:3001), 并启用
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

import websockets
import yaml

from memory import (
    MemoryStore,
    build_memory_prompt,
    extract_facts,
    infer_mood,
)

import webskill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qq-bot")

BASE_DIR = Path(__file__).resolve().parent
# 分发版目录结构: <root>/core/bot.py, 配置与数据在 <root>/config 和 <root>/data
# 兼容旧结构: bot.py 与 config.yaml 同目录
APP_DIR = BASE_DIR.parent if (BASE_DIR.parent / "config").is_dir() or (BASE_DIR.parent / "data").is_dir() else BASE_DIR
CONFIG_DIR = APP_DIR / "config"
DATA_DIR = APP_DIR / "data"


def _resolve(sub: str) -> Path:
    """在 config/ 或 data/ 下找文件, 找不到则回退到 APP_DIR 根。"""
    for base in (CONFIG_DIR, DATA_DIR, APP_DIR, BASE_DIR):
        p = base / sub
        if p.exists():
            return p
    return CONFIG_DIR / sub


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    # server = 本程序监听 ws_url 端口, 等 NapCat 的「WebSocket 客户端」连入 (反向 WS)
    # client = 本程序主动连 ws_url, 需要 NapCat 建「WebSocket 服务端」(正向 WS)
    mode: str = "server"

    # LLM
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.85
    llm_max_tokens: int = 600
    llm_timeout: float = 60.0
    llm_proxy: str = ""
    llm_fallbacks: List[str] = field(default_factory=list)
    llm_extra_body: Dict[str, Any] = field(default_factory=dict)

    # 人格
    persona: str = "tsundere"
    personas: Dict[str, Any] = field(default_factory=dict)

    # 行为
    group_reply_mode: str = "at"      # at | mention | all
    private_enabled: bool = True
    max_history: int = 20
    trigger_words: List[str] = field(default_factory=list)
    admins: List[int] = field(default_factory=list)
    group_blacklist: List[int] = field(default_factory=list)
    cooldown_seconds: float = 1.5
    max_context_chars: int = 4000
    sticker_dir: str = "stickers"
    # 连续发多条: 把多行回复按行拆成多条消息依次发出 (模拟真人)
    split_messages: bool = True
    # 记忆
    memory_enabled: bool = True
    memory_dir: str = "memory"
    # 视觉: 允许把用户发的图片一起送给模型
    vision_enabled: bool = True
    # 联网: 遇到实时性问题时先搜索再回答
    web_enabled: bool = True
    web_max_results: int = 4
    # 热梗库
    memes_file: str = "memes.yaml"
    use_memes: bool = True


STICKER_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def load_stickers(sticker_dir: str) -> Dict[str, List[str]]:
    """扫描表情包目录。

    目录结构约定 (推荐):
        stickers/
          情绪关键词/       <- 子目录名就是关键词, 如 "笑死" "无语" "抱抱"
             xxx.gif
             yyy.jpg
          另一个关键词/
            ...
    也可以把图片直接放在 stickers/ 下, 归入 "通用" 分组。
    """
    root = Path(sticker_dir)
    if not root.is_absolute():
        root = _resolve(sticker_dir)
    if not root.exists():
        return {}

    lib: Dict[str, List[str]] = {}
    for entry in root.iterdir():
        if entry.is_dir():
            files = [
                str(p) for p in entry.iterdir()
                if p.is_file() and p.suffix.lower() in STICKER_EXTS
            ]
            if files:
                lib[entry.name] = files
        elif entry.is_file() and entry.suffix.lower() in STICKER_EXTS:
            lib.setdefault("通用", []).append(str(entry))

    total = sum(len(v) for v in lib.values())
    if total:
        log.info("表情包库: %d 个分类, %d 张图片", len(lib), total)
        log.info("  分类: %s", ", ".join(lib.keys()))
    return lib


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _check_websockets_version() -> None:
    """websockets 的参数名改过, 旧版会以诡异方式失败, 提前拦一道。"""
    version = getattr(websockets, "__version__", "0")
    try:
        parts = [int(p) for p in version.split(".")[:2]]
    except ValueError:
        return
    if parts and tuple(parts) < (12, 0):
        log.warning(
            "websockets %s 过旧 (要求 >=12.0), 可能无法连接。请升级: python -m pip install -U websockets",
            version,
        )


def load_config() -> Config:
    cfg_path = _resolve("bot.yaml")
    if not cfg_path.exists():
        cfg_path = _resolve("config.yaml")
    if not cfg_path.exists():
        cfg_path = _resolve("config.example.yaml")
    if not cfg_path.exists():
        raise SystemExit(f"缺少配置文件, 请运行 start.bat 初始化 (查找位置: {CONFIG_DIR})")

    raw: Dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw.setdefault("napcat", {})
    raw.setdefault("llm", {})
    raw.setdefault("behavior", {})

    # 合并 secrets.yaml 里的 api_key (控制台把 key 存在这里, 与配置分离)
    try:
        sec_path = _resolve("secrets.yaml")
        if sec_path.exists():
            sec = yaml.safe_load(sec_path.read_text(encoding="utf-8")) or {}
            if sec.get("api_key"):
                raw["llm"]["api_key"] = sec["api_key"]
    except Exception:  # noqa: BLE001
        log.exception("读取 secrets.yaml 失败")

    # 合并 local.yaml 里的本机路径
    try:
        loc_path = _resolve("local.yaml")
        if loc_path.exists():
            loc = yaml.safe_load(loc_path.read_text(encoding="utf-8")) or {}
            if loc.get("napcat_dir"):
                raw.setdefault("napcat", {})["dir"] = loc["napcat_dir"]
    except Exception:  # noqa: BLE001
        pass

    # 人格库
    personas = _load_personas()
    persona_key = raw["llm"].get("persona", "tsundere")
    if persona_key not in personas:
        log.warning("人格 %s 不存在, 回退到 tsundere", persona_key)
        persona_key = "tsundere"
    # 配置里的人设覆盖同名人设库
    if raw["llm"].get("personas"):
        personas = _deep_merge(personas, raw["llm"]["personas"])

    c = Config(
        ws_url=raw["napcat"].get("ws_url", "ws://127.0.0.1:3001"),
        access_token=str(raw["napcat"].get("access_token", "") or ""),
        mode=str(raw["napcat"].get("mode", "server") or "server").lower(),
        llm_base_url=raw["llm"].get("base_url", "https://api.deepseek.com/v1").rstrip("/"),
        llm_api_key=str(raw["llm"].get("api_key", "") or ""),
        llm_model=raw["llm"].get("model", "deepseek-chat"),
        llm_temperature=float(raw["llm"].get("temperature", 0.85)),
        llm_max_tokens=int(raw["llm"].get("max_tokens", 600)),
        llm_timeout=float(raw["llm"].get("timeout", 60)),
        llm_proxy=str(raw["llm"].get("proxy", "") or ""),
        llm_fallbacks=[str(m) for m in (raw["llm"].get("fallbacks") or [])],
        llm_extra_body=raw["llm"].get("extra_body", {}) or {},
        persona=persona_key,
        personas=personas,
    )
    b = raw["behavior"]
    c.group_reply_mode = b.get("group_reply_mode", "at")
    c.private_enabled = bool(b.get("private_enabled", True))
    c.max_history = int(b.get("max_history", 20))
    c.trigger_words = [str(w) for w in b.get("trigger_words", [])]
    c.admins = [int(x) for x in b.get("admins", [])]
    c.group_blacklist = [int(x) for x in b.get("group_blacklist", [])]
    c.cooldown_seconds = float(b.get("cooldown_seconds", 1.5))
    c.max_context_chars = int(b.get("max_context_chars", 4000))
    c.sticker_dir = str(b.get("sticker_dir", "stickers") or "stickers")
    c.memory_enabled = bool(b.get("memory_enabled", True))
    c.memory_dir = str(b.get("memory_dir", "memory") or "memory")
    c.vision_enabled = bool(b.get("vision_enabled", True))
    c.web_enabled = bool(b.get("web_enabled", True))
    c.web_max_results = int(b.get("web_max_results", 4))
    c.memes_file = str(b.get("memes_file", "memes.yaml") or "memes.yaml")
    c.use_memes = bool(b.get("use_memes", True))
    return c


def load_memes(path: str) -> List[Dict[str, str]]:
    """加载热梗库, 返回 [{梗, 义}, ...]。"""
    p = Path(path)
    if not p.is_absolute():
        p = _resolve(path)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        log.exception("热梗库解析失败: %s", p)
        return []
    out: List[Dict[str, str]] = []
    for _cat, items in data.items():
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("梗"):
                out.append({"梗": str(it["梗"]), "义": str(it.get("义", ""))})
    if out:
        log.info("热梗库: %d 条", len(out))
    return out


def _load_personas() -> Dict[str, Any]:
    path = _resolve("personas.yaml")
    if not path.exists():
        raise SystemExit(f"缺少人格库文件 personas.yaml (查找位置: {DATA_DIR})")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


# --------------------------------------------------------------------------- #
# LLM 客户端 (OpenAI 兼容, 零依赖实现)
# --------------------------------------------------------------------------- #
class LLMError(RuntimeError):
    pass


# 模型冷却表: {model: 恢复时间戳}
# 被限流的模型在恢复前直接跳过, 避免每次都先撞一次墙
_MODEL_COOLDOWN: Dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


def _mark_cooldown(model: str, seconds: int) -> None:
    with _COOLDOWN_LOCK:
        _MODEL_COOLDOWN[model] = time.time() + max(5, min(seconds, 3600))


def _is_cooling(model: str) -> bool:
    with _COOLDOWN_LOCK:
        until = _MODEL_COOLDOWN.get(model, 0)
        if until and time.time() >= until:
            _MODEL_COOLDOWN.pop(model, None)
            return False
        return bool(until)


class LLMClient:
    """最小 OpenAI 兼容 chat/completions 客户端 (仅标准库)。

    本机网关 (127.0.0.1 / localhost) 自动直连, 不走系统代理;
    如需统一走代理, 在 config.yaml 的 llm.proxy 里填写, 例如 http://127.0.0.1:7890。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        host = (urlparse(cfg.llm_base_url).hostname or "").lower()
        self._is_local = host in ("127.0.0.1", "localhost", "::1")
        self._opener = build_opener(ProxyHandler({"http": cfg.llm_proxy, "https": cfg.llm_proxy} if cfg.llm_proxy else {}))

    def _open(self, req: Request):
        if self._is_local:
            # 直连, 绕过系统代理 (Windows 上系统代理会让 127.0.0.1 也走代理而失败)
            return build_opener(ProxyHandler({})).open(req, timeout=self.cfg.llm_timeout)
        return self._opener.open(req, timeout=self.cfg.llm_timeout)

    def chat(self, messages: List[Dict[str, str]], temperature: Optional[float] = None) -> str:
        cfg = self.cfg
        headers = {"Content-Type": "application/json"}
        if cfg.llm_api_key:
            headers["Authorization"] = f"Bearer {cfg.llm_api_key}"

        # 主模型失败时依次尝试备用模型 (应对 429 限流 / 模型繁忙)
        all_models = [cfg.llm_model] + [m for m in cfg.llm_fallbacks if m != cfg.llm_model]
        # 跳过冷却中的模型 (最近被限流的)
        models = [m for m in all_models if not _is_cooling(m)]
        if not models:
            # 全部在冷却中 -> 用备用里最早恢复的
            models = all_models
            log.warning("所有模型都在冷却中, 尝试最快恢复的 %s", models[0])
        elif len(models) < len(all_models):
            log.info("跳过冷却中的模型: %s", [m for m in all_models if _is_cooling(m)])
        last_err: Optional[Exception] = None
        for attempt, model in enumerate(models):
            body = {
                "model": model,
                "messages": messages,
                "temperature": cfg.llm_temperature if temperature is None else temperature,
                "max_tokens": cfg.llm_max_tokens,
                "stream": False,
            }
            if cfg.llm_extra_body:
                body.update(cfg.llm_extra_body)
            req = Request(
                cfg.llm_base_url + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            # 429 处理策略: 不等长重试, 快速切换下一个模型。
            # 等待 "模型繁忙" 通常要几十秒, 聊天场景下用户等不起。
            for try_no in range(2):
                try:
                    with self._open(req) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    return payload["choices"][0]["message"]["content"]
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", "ignore")[:500]
                    err = LLMError(f"HTTP {exc.code} ({model}): {detail}")
                    last_err = err
                    if exc.code == 429:
                        # 只在"很短就能恢复"时重试, 否则立刻换模型
                        wait = _parse_retry_after(detail, default=999)
                        if wait <= 3 and try_no == 0:
                            log.warning("模型 %s 限流 %ss, 短暂等待后重试", model, wait)
                            time.sleep(wait)
                            continue
                        log.warning("模型 %s 限流(需等 %ss), 立即切换备用模型", model, wait)
                        _mark_cooldown(model, wait)
                        break  # 跳到下一个模型
                    if exc.code in (400, 401, 403, 404):
                        break  # 配置类错误, 换模型也没用
                    time.sleep(1)
                    continue
                except (KeyError, IndexError, TypeError):
                    last_err = LLMError(f"响应解析失败 ({model}): {payload!r}")
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = LLMError(f"请求失败 ({model}): {exc}")
                    time.sleep(1)
                    continue
            if attempt < len(models) - 1:
                log.warning("模型 %s 不可用, 切换备用模型 %s", model, models[attempt + 1])

        raise last_err or LLMError("LLM 调用失败")


def _parse_retry_after(detail: str, default: int = 3) -> int:
    """从 429 响应体里解析要求的等待秒数。"""
    m = re.search(r'"retry_after"\s*:\s*(\d+)', detail)
    if not m:
        m = re.search(r"retry in (\d+)\s*s", detail)
    if m:
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            pass
    return default


# --------------------------------------------------------------------------- #
# OneBot 11 客户端
# --------------------------------------------------------------------------- #
class OneBotClient:
    def __init__(self, cfg: Config, handler):
        self.cfg = cfg
        self.handler = handler
        self.ws: Any = None
        self._echo = 0
        self._pending: Dict[str, asyncio.Future] = {}
        self.self_id: Optional[int] = None

    # ---------------- 反向模式: 本程序监听, NapCat 主动连进来 ----------------
    async def serve_forever(self) -> None:
        """监听 ws_url 里的端口, 等 NapCat 的 WebSocket 客户端连入。"""
        parsed = urlparse(self.cfg.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 3001
        path = parsed.path or "/"

        async def handler(ws: Any) -> None:
            # 校验 token: NapCat 端配置的 token 会放在 Authorization 头或 query 里
            if not self._check_token(ws):
                log.warning("拒绝连接: token 不匹配")
                await ws.close(code=1008, reason="invalid token")
                return
            peer = getattr(ws, "remote_address", None)
            log.info("NapCat 已连接 (反向 WS) 来自 %s", peer)
            self.ws = ws
            try:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await self._dispatch(payload)
            except websockets.ConnectionClosed:
                pass
            finally:
                if self.ws is ws:
                    self.ws = None
                log.warning("NapCat 连接已断开, 等待重连...")

        log.info("监听反向 WebSocket: ws://%s:%d%s (等待 NapCat 连入)", host, port, path)
        async with websockets.serve(
            handler,
            host,
            port,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ):
            while True:
                await asyncio.sleep(3600)

    def _check_token(self, ws: Any) -> bool:
        token = self.cfg.access_token
        if not token:
            return True
        headers = getattr(getattr(ws, "request", None), "headers", None) or {}
        auth = headers.get("Authorization", "") if hasattr(headers, "get") else ""
        if auth in (f"Bearer {token}", f"Token {token}", token):
            return True
        path = getattr(getattr(ws, "request", None), "path", "") or ""
        return f"access_token={token}" in path

    # ---------------- 正向模式: 本程序主动连 NapCat ----------------
    async def connect(self) -> None:
        headers = {}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"
        log.info("连接 NapCat: %s", self.cfg.ws_url)

        # websockets 的鉴权头参数名改过: 新版叫 additional_headers, 旧版叫
        # extra_headers。直接探测签名, 两个版本都能用。
        kwargs: Dict[str, Any] = {
            "max_size": 16 * 1024 * 1024,
            "ping_interval": 20,
            "ping_timeout": 20,
        }
        params = inspect.signature(websockets.connect).parameters
        header_key = "additional_headers"
        if "additional_headers" in params:
            header_key = "additional_headers"
        elif "extra_headers" in params:
            header_key = "extra_headers"
        else:
            log.warning(
                "websockets %s 不支持自定义鉴权头; access_token 已忽略。"
                "建议升级: python -m pip install -U websockets",
                getattr(websockets, "__version__", "?"),
            )
        if headers or header_key:
            kwargs[header_key] = headers

        async with websockets.connect(self.cfg.ws_url, **kwargs) as ws:
            self.ws = ws
            log.info("已连接, 等待事件...")
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(payload)

    async def start(self) -> None:
        if self.cfg.mode == "server":
            await self.serve_forever()
        else:
            await self.connect()

    async def _dispatch(self, payload: Dict[str, Any]) -> None:
        if "echo" in payload and payload["echo"] in self._pending:
            fut = self._pending.pop(payload["echo"])
            if not fut.done():
                fut.set_result(payload)
            return
        if payload.get("post_type") == "meta_event" and payload.get("meta_event_type") == "lifecycle":
            self.self_id = payload.get("self_id")
            log.info("生命周期事件: %s self_id=%s", payload.get("sub_type"), self.self_id)
            return
        if payload.get("post_type") == "message":
            try:
                await self.handler(payload)
            except Exception:  # noqa: BLE001
                log.exception("处理消息时出错")

    async def call(self, action: str, **params: Any) -> Dict[str, Any]:
        self._echo += 1
        echo = f"e{self._echo}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            resp = await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError as exc:
            self._pending.pop(echo, None)
            raise LLMError(f"API 超时: {action}") from exc
        return resp

    async def send_group_msg(self, group_id: int, text: str) -> None:
        await self.call("send_group_msg", group_id=group_id, message=text)

    async def send_private_msg(self, user_id: int, text: str) -> None:
        await self.call("send_private_msg", user_id=user_id, message=text)


# --------------------------------------------------------------------------- #
# 消息解析
# --------------------------------------------------------------------------- #
def extract_images(message: Any) -> List[str]:
    """从消息段里提取图片 URL。"""
    if isinstance(message, str):
        message = _parse_cq(message)
    urls: List[str] = []
    for seg in message or []:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        data = seg.get("data") or {}
        # NapCat 通常给 url 字段；file 字段可能是本地路径或 base64
        url = str(data.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
            continue
        # 有些实现把图片放在 file 里，且 file 是 http 链接
        f = str(data.get("file") or "").strip()
        if f.startswith(("http://", "https://")):
            urls.append(f)
    return urls[:4]  # 最多 4 张，避免请求过大


def extract_text(message: Any) -> str:
    """把 OneBot 消息段 (array 或 CQ 字符串) 拍平成纯文本。"""
    if isinstance(message, str):
        message = _parse_cq(message)
    parts: List[str] = []
    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            parts.append(str(seg.get("data", {}).get("text", "")))
        elif seg.get("type") == "face":
            parts.append(f"[表情{seg.get('data', {}).get('id', '')}]")
        elif seg.get("type") == "image":
            parts.append("[图片]")
        elif seg.get("type") == "at":
            qq = seg.get("data", {}).get("qq", "")
            if str(qq) != "all":
                # 后面补一个空格, 与下个文本段分隔; 末尾多余空白最后统一压掉
                parts.append(f"@{qq} ")
    return " ".join("".join(parts).split())


_CQ_RE = re.compile(r"\[CQ:([a-z]+)((?:,[^\]\[]+)?)\]")


def _parse_cq(text: str) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    last = 0
    for m in _CQ_RE.finditer(text):
        if m.start() > last:
            segs.append({"type": "text", "data": {"text": text[last : m.start()]}})
        data: Dict[str, str] = {}
        if m.group(2):
            for kv in m.group(2)[1:].split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    data[k] = v
        segs.append({"type": m.group(1), "data": data})
        last = m.end()
    if last < len(text):
        segs.append({"type": "text", "data": {"text": text[last:]}})
    return segs


# --------------------------------------------------------------------------- #
# 机器人
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    history: List[Dict[str, str]] = field(default_factory=list)
    last_active: float = 0.0
    last_reply: float = 0.0


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = LLMClient(cfg)
        self.sessions: Dict[str, Session] = {}
        # 正常流程 load_config() 已保证 key 存在; 这里兜底防止直接构造 Config 时 KeyError
        self.persona = cfg.personas.get(cfg.persona) or {"name": cfg.persona, "system_prompt": ""}
        # 表情包库: {"笑死": [路径, ...], "无语": [...], ...}
        self.stickers: Dict[str, List[str]] = load_stickers(cfg.sticker_dir)
        # 热梗库
        self.memes: List[Dict[str, str]] = load_memes(cfg.memes_file) if cfg.use_memes else []
        # 长期记忆
        if cfg.memory_enabled:
            mdir = Path(cfg.memory_dir)
            if not mdir.is_absolute():
                mdir = _resolve(mdir)
            self.memory = MemoryStore(mdir, max_facts=60, max_history=cfg.max_history)
        else:
            self.memory = None

    # ---------------- 触发判定 ----------------
    def should_reply(self, ev: Dict[str, Any], text: str, is_self: bool) -> bool:
        cfg = self.cfg
        if is_self:
            return False
        msg_type = ev.get("message_type")
        if msg_type == "private":
            return cfg.private_enabled
        if msg_type == "group":
            gid = ev.get("group_id")
            if gid in cfg.group_blacklist:
                return False
            mode = cfg.group_reply_mode
            if mode == "at":
                self_id = ev.get("self_id")
                return bool(self_id and f"@{self_id}" in text)
            if mode == "mention":
                return any(w in text for w in cfg.trigger_words)
            if mode == "all":
                return True
        return False

    # ---------------- 主处理 ----------------
    async def handle(self, ev: Dict[str, Any]) -> None:
        text = extract_text(ev.get("message"))
        images = extract_images(ev.get("message")) if self.cfg.vision_enabled else []
        # 纯图片消息也要处理
        if not text and not images:
            return
        if not text:
            text = "（他发来一张图片）"
        is_self = ev.get("user_id") == ev.get("self_id")
        if not self.should_reply(ev, text, is_self):
            return

        key = self._session_key(ev)
        now = time.time()
        sess = self.sessions.setdefault(key, Session())

        # 上下文过期只清对话历史, 不清长期记忆与情绪
        if now - sess.last_active > 3600:
            sess.history.clear()
        sess.last_active = now

        # ---- 更新长期记忆: 事实 + 情绪 ----
        if self.memory:
            mem = self.memory.get(key)
            for fact in extract_facts(text):
                if self.memory.add_fact(key, fact):
                    log.info("[记忆] 学到事实: %s", fact)
            mood_hit = infer_mood(text)
            if mood_hit:
                mood, inten, trigger = mood_hit
                self.memory.set_mood(key, mood, inten, trigger)
                log.info("[情绪] -> %s (%.0f%%) 触发词: %s", mood, inten * 100, trigger)
            mem.total_interactions += 1
            mem.last_seen = now
            mem.last_user_msg = text
            # 对话历史同步进持久化记忆
            mem.history = sess.history
            self.memory.save(key)

        if now - sess.last_reply < self.cfg.cooldown_seconds:
            return
        sess.last_reply = now

        reply = await self.generate_reply(ev, sess, text, key=key, images=images)
        if not reply:
            return
        reply = self._postprocess(reply)
        segs = self._parse_ai_reply(reply)
        await self._send(ev, segs)

        # 回复后落盘对话历史
        if self.memory:
            mem = self.memory.get(key)
            mem.history = sess.history
            self.memory.save(key)

    async def generate_reply(
        self,
        ev: Dict[str, Any],
        sess: Session,
        text: str,
        key: str = "",
        images: Optional[List[str]] = None,
    ) -> str:
        cfg = self.cfg
        persona = self.persona
        sys_prompt = persona.get("system_prompt", "")
        sender = ev.get("sender", {}) or {}
        nickname = sender.get("card") or sender.get("nickname") or str(ev.get("user_id", ""))
        if persona.get("style_hints"):
            sys_prompt += "\n\n风格要求: " + " ".join(persona["style_hints"])

        # ---- 联网搜索 (只对实时性问题触发) ----
        if cfg.web_enabled and webskill.should_search(text):
            try:
                query = webskill.extract_query(text)
                log.info("[联网] 搜索: %s", query)
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: webskill.web_search(query, cfg.web_max_results)
                )
                ctx = webskill.build_search_context(result)
                if ctx:
                    sys_prompt += "\n\n" + ctx
                    log.info("[联网] 命中 %d 条", len(result.get("results") or []))
                else:
                    log.info("[联网] 无结果")
            except Exception as exc:  # noqa: BLE001
                log.warning("[联网] 失败: %s", exc)

        # ---- 长期记忆与情绪 ----
        if self.memory and key:
            mem = self.memory.get(key)
            mem_ctx = build_memory_prompt(mem)
            if mem_ctx:
                sys_prompt += "\n\n" + mem_ctx
                mood = mem.mood.decayed(time.time())
                if mood.name and mood.intensity > 0.5:
                    sys_prompt += (
                        "\n【重要】你现在情绪很明显, 不能表现得若无其事。"
                        "如果刚才他惹你生气, 这一句不要突然说甜言蜜语或表白; "
                        "可以冷淡、可以阴阳怪气、可以要求他道歉。真人不会秒和好。"
                    )

        # ---- 热梗 ----
        if self.memes and persona.get("use_memes", True):
            sample = random.sample(self.memes, min(18, len(self.memes)))
            gloss = "；".join(f"{m['梗']}={m['义']}" for m in sample if m.get("义"))
            sys_prompt += (
                "\n\n【网络热梗(可用可不用的参考, 不要照搬清单)】" + gloss +
                "\n用法: 偶尔用在合适的地方显得接地气, 比如「这也太离谱了」「我麻了」。"
                "不要每句都塞梗, 不要为了用梗而扭曲意思, 不确定的梗宁可用大白话。"
            )

        # ---- 聊天样本 ----
        examples = persona.get("examples") or []
        if examples:
            sys_prompt += "\n\n【参考这几个聊天样本的语气和长度, 不要照抄内容】"
            for ex in examples:
                replies = ex.get("assistant") or []
                if isinstance(replies, str):
                    replies = [replies]
                lines = [f"他: {ex.get('user', '')}"]
                lines += [f"你: {r}" for r in replies]
                sys_prompt += "\n" + "\n".join(lines)

        # ---- 表情包 ----
        if self.stickers and persona.get("use_stickers", True):
            cats = "、".join(self.stickers.keys())
            sys_prompt += (
                "\n\n【表情包】你有表情包可以发, 分类: " + cats + "。"
                "当你情绪比较明显时(觉得好笑/无语/生气/心疼/想撒娇/道晚安), "
                "可以单独写一行 [sticker:关键词] 来发表情。"
                "注意: 大约每 4~5 次回复里可以有 1 次带表情, 不要每次都带。"
            )

        # ---- 组装用户消息 (支持图片) ----
        if self.cfg.vision_enabled and images:
            content: Any = [{"type": "text", "text": f"{nickname}: {text}"}]
            for u in images:
                content.append({"type": "image_url", "image_url": {"url": u}})
            sess.history.append({"role": "user", "content": content})  # type: ignore[arg-type]
            log.info("[视觉] 本轮带 %d 张图片", len(images))
        else:
            sess.history.append({"role": "user", "content": f"{nickname}: {text}"})
        self._trim_history(sess)

        messages = [{"role": "system", "content": sys_prompt}] + sess.history
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, self.llm.chat, messages)
        except LLMError as exc:
            log.error("LLM 调用失败: %s", exc)
            return random.choice(
                ["(脑子突然打结了，稍后再聊~)", "呜…断线了，再说一次?", "啊这，CPU 烧了，缓一缓"]
            )
        sess.history.append({"role": "assistant", "content": reply})
        self._trim_history(sess)
        return reply

    def _trim_history(self, sess: Session) -> None:
        cfg = self.cfg

        def _size(m: Dict[str, Any]) -> int:
            c = m.get("content")
            if isinstance(c, str):
                return len(c)
            if isinstance(c, list):
                # 多模态: 只算文本长度, 图片按固定 200 估算
                n = 0
                for part in c:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            n += len(str(part.get("text", "")))
                        else:
                            n += 200
                return n
            return 0

        total = sum(_size(m) for m in sess.history)
        while (
            len(sess.history) > cfg.max_history or total > cfg.max_context_chars
        ) and sess.history:
            removed = sess.history.pop(0)
            total -= _size(removed)

    def _postprocess(self, reply: str) -> str:
        # 去掉模型偶尔带上的角色名前缀
        reply = re.sub(r"^(assistant|机器人|AI|助手)\s*[:：]\s*", "", reply.strip())
        reply = re.sub(r"\n{3,}", "\n\n", reply)
        return reply.strip()[:1800]

    # ---------------- 消息发送 (支持多段/表情) ----------------
    def _parse_ai_reply(self, reply: str) -> List[Dict[str, Any]]:
        """把 AI 回复解析成消息段列表。

        规则:
          - 每一行(或空行分隔)算一条独立消息, 模拟真人连发几条短句
          - [sticker:关键词] 解析为表情消息段
          - 其余为文本
        """
        segs: List[Dict[str, Any]] = []
        # 先按行切, 保留 [sticker:xxx] 独立
        lines = [ln.strip() for ln in reply.split("\n")]
        lines = [ln for ln in lines if ln]
        for ln in lines:
            m = re.match(r"^\[sticker[:：]\s*(.+?)\]$", ln)
            if m:
                segs.append({"type": "sticker", "keyword": m.group(1).strip()})
                continue
            # 行内嵌的 sticker 也拆出来
            parts = re.split(r"\[sticker[:：]\s*(.+?)\]", ln)
            for i, part in enumerate(parts):
                if i % 2 == 1:
                    segs.append({"type": "sticker", "keyword": part.strip()})
                elif part.strip():
                    segs.append({"type": "text", "text": part.strip()})
        return segs or [{"type": "text", "text": reply.strip()}]

    def _pick_sticker(self, keyword: str) -> Optional[str]:
        """按关键词挑一张表情包, 挑不到就按心情随机。"""
        if not self.stickers:
            return None
        kw = (keyword or "").lower()
        # 关键词精确/包含匹配
        for key, files in self.stickers.items():
            if key in kw or kw in key:
                if files:
                    return random.choice(files)
        # 模糊: 任何一片段的字符匹配
        best = None
        for key, files in self.stickers.items():
            if any(ch in kw for ch in key) and files:
                best = files
                break
        if best:
            return random.choice(best)
        # 兜底: 随便挑一张
        allfiles = [f for files in self.stickers.values() for f in files]
        return random.choice(allfiles) if allfiles else None

    def _build_messages(self, segs: List[Dict[str, Any]], at_user: Optional[int]) -> List[str]:
        """把消息段拼成若干条独立的 OneBot 消息 (CQ 码), 模拟真人连发。

        文本段各自成一条; 表情段单独成一条。
        第一条带 @ 用户 (群聊且 at 模式时)。
        """
        out: List[str] = []
        for seg in segs:
            if seg["type"] == "text":
                out.append(seg["text"])
            elif seg["type"] == "sticker":
                path = self._pick_sticker(seg.get("keyword", ""))
                if path:
                    p = Path(path).resolve().as_posix()
                    out.append(f"[CQ:image,file=file:///{p}]")
        out = [m for m in out if m]
        if out and at_user:
            out[0] = f"[CQ:at,qq={at_user}] {out[0]}"
        return out

    async def _send(self, ev: Dict[str, Any], segs: List[Dict[str, Any]]) -> None:
        client: OneBotClient = self.client  # type: ignore[attr-defined]
        is_group = ev.get("message_type") == "group"
        at_user = ev.get("user_id") if (is_group and self.cfg.group_reply_mode == "at") else None

        messages = self._build_messages(segs, at_user)
        if not messages:
            return

        # 多条消息之间留一点间隔, 更像真人逐条打字
        for i, msg in enumerate(messages):
            if i > 0:
                await asyncio.sleep(random.uniform(0.6, 1.4))
            try:
                if is_group:
                    await client.send_group_msg(ev.get("group_id"), msg)
                else:
                    await client.send_private_msg(ev.get("user_id"), msg)
            except Exception:  # noqa: BLE001
                log.exception("发送消息失败: %s", msg[:80])

    def _session_key(self, ev: Dict[str, Any]) -> str:
        if ev.get("message_type") == "group":
            return f"g{ev.get('group_id')}"
        return f"p{ev.get('user_id')}"

    # ---------------- 指令 ----------------
    def admin_command(self, ev: Dict[str, Any], text: str) -> Optional[str]:
        """管理员指令, 返回要回复的文本或 None。"""
        uid = ev.get("user_id")
        if uid not in self.cfg.admins:
            return None
        m = re.match(r"^\s*(?:切换人设|qiehuan renshe|persona)\s+([\w\-]+)\s*$", text)
        if m:
            name = m.group(1)
            if name in self.cfg.personas:
                self.persona = self.cfg.personas[name]
                self.cfg.persona = name
                # 换人格不清空对话记忆 —— 记忆属于"这个人", 不属于人格
                return f"好，现在切换到「{self.persona.get('name', name)}」人格了"
            return f"没有人设叫 {name} 哦，现有: {', '.join(self.cfg.personas)}"
        if re.match(r"^\s*(?:人设列表|人格列表|renshe liebiao)\s*$", text):
            lines = [
                f"- {k}: {v.get('name', k)} — {v.get('desc', '')}"
                for k, v in self.cfg.personas.items()
            ]
            return "当前可用人设:\n" + "\n".join(lines)
        return None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
async def amain() -> None:
    _check_websockets_version()
    cfg = load_config()
    bot = Bot(cfg)

    async def handler(ev: Dict[str, Any]) -> None:
        text = extract_text(ev.get("message"))
        admin_reply = bot.admin_command(ev, text)
        if admin_reply:
            client: OneBotClient = bot.client  # type: ignore[attr-defined]
            await bot._send(ev, admin_reply)
            return
        await bot.handle(ev)

    client = OneBotClient(cfg, handler)
    bot.client = client  # type: ignore[attr-defined]

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    backoff = 1
    while not stop.is_set():
        try:
            await client.start()
            backoff = 1
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
            log.warning("连接断开 (%s), %ds 后重连", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception:  # noqa: BLE001
            log.exception("未知错误, %ds 后重连", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("已退出")


if __name__ == "__main__":
    main()
