"""QQ 机器人管理面板 —— 本地 Web 控制台。

零依赖实现 (只用 Python 标准库), 监听 127.0.0.1, 通过浏览器管理机器人。

用法:
    python console.py            # 默认 http://127.0.0.1:8789
    python console.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import ProxyHandler, build_opener

import yaml

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
CONFIG_DIR = ROOT / "config"
LOGS = ROOT / "logs"
ASSETS = ROOT / "assets"
CORE = ROOT / "core"

LOG_FILE = LOGS / "bot.log"
GW_URL = "http://127.0.0.1:8788"
GW_HEALTH = GW_URL + "/healthz"
BOT_PORT = 3001
CONSOLE_PORT = 8789

log = logging.getLogger("console")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def read_yaml(path: Path) -> Dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """写 YAML, 保留中文不转义。"""
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def port_established(port: int) -> bool:
    """检查端口上是否有已建立的连接 (判断 NapCat 是否连上 bot)。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=6,
            encoding="gbk", errors="ignore",
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    for line in out.splitlines():
        if f":{port} " in line and "ESTABLISHED" in line:
            return True
    return False


def count_process(name: str) -> int:
    try:
        out = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {name}", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=6, encoding="gbk", errors="ignore",
        ).stdout
        return sum(1 for ln in out.splitlines() if name.lower() in ln.lower())
    except Exception:  # noqa: BLE001
        return 0


def find_pids_on_port(port: int) -> List[int]:
    pids: List[int] = []
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=6,
            encoding="gbk", errors="ignore",
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(pids))


def kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        pass


def napcat_dir() -> Optional[Path]:
    """找 NapCat 目录: 本机配置 > bot.yaml > 本分发包内 > 用户主目录/桌面/下载。

    只扫有限几个位置, 不做全盘遍历 (慢且可能误命中)。
    """
    # 1. local.yaml (本机特定, 不分享)
    loc = read_yaml(local_path())
    nd = loc.get("napcat_dir") or ""
    if not nd:
        # 2. bot.yaml
        cfg = read_yaml(bot_config_path())
        nd = (cfg.get("napcat") or {}).get("dir") or ""
    if nd:
        p = Path(os.path.expandvars(str(nd)))
        if (p / "napiloader.dll").exists():
            return p
    home = Path(os.environ.get("USERPROFILE", ""))
    for cand in (
        ROOT / "NapCat.Framework",
        home / "NapCat.Framework",
        home / "NapCatQQ",
        home / "Desktop" / "NapCat.Framework",
        home / "Downloads" / "NapCat.Framework",
    ):
        try:
            if (cand / "napiloader.dll").exists():
                return cand
        except OSError:
            continue
    return None


def napcat_ok() -> bool:
    """NapCat 是否真的可用 (目录 + 关键文件都在)。"""
    nd = napcat_dir()
    return bool(nd and (nd / "napiloader.dll").exists())


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def bot_config_path() -> Path:
    return CONFIG_DIR / "bot.yaml"


def secrets_path() -> Path:
    """敏感信息 (API Key) 单独存放, 不进入发布包。"""
    return CONFIG_DIR / "secrets.yaml"


def local_path() -> Path:
    """本机特定信息 (NapCat 路径等), 不进入发布包。"""
    return CONFIG_DIR / "local.yaml"


def load_bot_config() -> Dict[str, Any]:
    cfg = read_yaml(bot_config_path())
    # 合并 secrets.yaml 里的 api_key
    sec = read_yaml(secrets_path())
    if sec.get("api_key"):
        cfg.setdefault("llm", {})["api_key"] = sec["api_key"]
    # 合并 local.yaml 里的本机路径 (napcat.dir)
    loc = read_yaml(local_path())
    if loc.get("napcat_dir"):
        cfg.setdefault("napcat", {})["dir"] = loc["napcat_dir"]
    return cfg


def save_bot_config(cfg: Dict[str, Any]) -> None:
    """写回 bot.yaml, 敏感字段剥离到 secrets.yaml。"""
    # 深拷贝一份再剥离 key, 避免改动调用方的 dict
    import copy
    out = copy.deepcopy(cfg)
    llm = out.get("llm") or {}
    api_key = str(llm.pop("api_key", "") or "")
    write_yaml(bot_config_path(), out)
    # API Key 单独存
    old_sec = read_yaml(secrets_path())
    if api_key or ("api_key" in old_sec):
        write_yaml(secrets_path(), {"api_key": api_key, "_note": "此文件含敏感信息, 不要分享"})


def personas_path() -> Path:
    return DATA / "personas.yaml"


def load_personas() -> Dict[str, Any]:
    return read_yaml(personas_path())


def save_personas(data: Dict[str, Any]) -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    personas_path().write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def start_gateway() -> str:
    if port_open(8788):
        return "网关已在运行"
    gw = Path(os.environ.get("USERPROFILE", "")) / "workbuddy-gateway"
    bat = gw / "start-wb-proxy.bat"
    if not bat.exists():
        return "网关未安装 (运行 tools\\setup-gateway.bat)"
    subprocess.Popen(
        ["cmd", "/c", "start", "WorkBuddy Gateway", "/MIN", "/D", str(gw), "cmd", "/c", str(bat)],
        cwd=str(gw), creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    time.sleep(5)
    return "网关启动中"


def start_bot() -> str:
    if port_open(BOT_PORT):
        return "机器人已在运行"
    py = sys.executable
    LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(CORE)
    logf = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    logf.write(f"\n{'='*60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 控制台启动机器人\n")
    logf.flush()
    subprocess.Popen(
        [py, "bot.py"],
        cwd=str(CORE), env=env, stdout=logf, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    for _ in range(24):
        time.sleep(0.5)
        if port_open(BOT_PORT):
            return "机器人已启动, 监听 3001"
    return "机器人启动超时 (查看日志)"


def start_napcat() -> str:
    nd = napcat_dir()
    if not nd:
        return "未找到 NapCat 目录 (运行 tools\\setup-napcat.bat)"
    bat = nd / "napiLoader.bat"
    if not bat.exists():
        return f"NapCat 启动脚本不存在: {bat}"
    if count_process("QQ.exe") > 0:
        return "QQ 已在运行 (如需重载 NapCat 配置请先关闭 QQ)"
    subprocess.Popen(
        ["cmd", "/c", "start", "NapCat", "/MIN", "cmd", "/c", "napiLoader.bat"],
        cwd=str(nd), creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return "NapCat 启动中, 请扫码登录 QQ"


def stop_all() -> str:
    msgs = []
    pids = find_pids_on_port(BOT_PORT)
    for p in pids:
        kill_pid(p)
    if pids:
        msgs.append(f"机器人已停止 ({len(pids)} 个进程)")
    # QQ / NapCat
    try:
        subprocess.run(["taskkill", "/f", "/im", "QQ.exe"], capture_output=True, timeout=8)
        msgs.append("QQ 已关闭")
    except Exception:  # noqa: BLE001
        pass
    pids = find_pids_on_port(8788)
    for p in pids:
        kill_pid(p)
    if pids:
        msgs.append("网关已停止")
    return "; ".join(msgs) or "没有运行中的组件"


def restart_bot() -> str:
    for p in find_pids_on_port(BOT_PORT):
        kill_pid(p)
    time.sleep(1.2)
    return start_bot()


def test_llm() -> Dict[str, Any]:
    """用当前配置真实调用一次模型。主模型失败时依次试备用模型。"""
    cfg = load_bot_config()
    llm = cfg.get("llm") or {}
    base = str(llm.get("base_url", "")).rstrip("/")
    key = str(llm.get("api_key", ""))
    model = str(llm.get("model", ""))
    fallbacks = [str(m) for m in (llm.get("fallbacks") or []) if str(m).strip()]
    if not base or not model:
        raise RuntimeError("模型配置不完整，请在「模型与网关」页填写")
    if not key:
        raise RuntimeError("API Key 为空")

    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    op = build_opener(ProxyHandler({})) if host in ("127.0.0.1", "localhost") else build_opener()

    body_bytes = json.dumps({
        "messages": [{"role": "user", "content": "说一个字：好"}],
        "max_tokens": 30, "stream": False,
    }).encode()

    last_err: Optional[Exception] = None
    for m in [model] + [x for x in fallbacks if x != model]:
        body = json.loads(body_bytes.decode())
        body["model"] = m
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with op.open(req, timeout=45) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            last_err = RuntimeError(f"HTTP {e.code} ({m}): {detail}")
            # 429 且明确限流, 换下一个模型; 400/401/403/404 是配置问题, 直接报
            if e.code == 429:
                continue
            raise last_err
        except Exception as e:  # noqa: BLE001
            last_err = RuntimeError(f"连接失败 ({m}): {e}")
            continue
        try:
            reply = d["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise RuntimeError(f"响应格式异常: {str(d)[:150]}")
        return {"reply": reply.strip()[:60], "model": d.get("model", m)}

    raise last_err or RuntimeError("所有模型都不可用")


def fetch_models() -> List[str]:
    cfg = load_bot_config()
    llm = cfg.get("llm") or {}
    base = str(llm.get("base_url", "")).rstrip("/")
    key = str(llm.get("api_key", ""))
    req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}"})
    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    op = build_opener(ProxyHandler({})) if host in ("127.0.0.1", "localhost") else build_opener()
    try:
        with op.open(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        return sorted(m["id"] for m in d.get("data", []) if isinstance(m, dict) and "id" in m)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"拉取失败: {e}")


def gateway_check() -> Dict[str, Any]:
    up = port_open(8788)
    models: List[str] = []
    if up:
        try:
            cfg = load_bot_config()
            key = str((cfg.get("llm") or {}).get("api_key", ""))
            req = urllib.request.Request(GW_URL + "/v1/models", headers={"Authorization": f"Bearer {key}"})
            with build_opener(ProxyHandler({})).open(req, timeout=8) as r:
                models = [m["id"] for m in json.loads(r.read().decode()).get("data", [])][:20]
        except Exception:  # noqa: BLE001
            pass
    return {"up": up, "url": GW_URL, "models": models}


def read_logs(tail: int = 400) -> List[str]:
    if not LOG_FILE.exists():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    lines = text.splitlines()
    return lines[-tail:]


def memory_sessions() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    mdir = DATA / "memory"
    if not mdir.exists():
        return out
    for f in sorted(mdir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        mood = d.get("mood") or {}
        mname = mood.get("name") if isinstance(mood, dict) else None
        out.append({
            "key": f.stem,
            "facts": [x.get("text", "") for x in d.get("facts", []) if isinstance(x, dict)][:40],
            "mood": f"{mname} {float(mood.get('intensity', 0)):.0%}" if mname else "",
            "total": d.get("total_interactions", 0),
            "last": time.strftime("%m-%d %H:%M", time.localtime(d.get("last_seen", 0))) if d.get("last_seen") else "",
        })
    return out


def sticker_stats() -> Dict[str, int]:
    root = DATA / "stickers"
    cats = 0
    total = 0
    if root.exists():
        for d in root.iterdir():
            if d.is_dir():
                n = sum(1 for f in d.iterdir() if f.suffix.lower() in (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"))
                if n:
                    cats += 1
                    total += n
    return {"cats": cats, "total": total}


def meme_count() -> int:
    d = read_yaml(DATA / "memes.yaml")
    n = 0
    for v in d.values():
        if isinstance(v, list):
            n += sum(1 for i in v if isinstance(i, dict) and i.get("梗"))
    return n


# --------------------------------------------------------------------------- #
# 表情包管理
# --------------------------------------------------------------------------- #
STICKER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def sticker_detail() -> Dict[str, Any]:
    """返回每个分类的图片数量与文件名。"""
    root = DATA / "stickers"
    cats = []
    total = 0
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            files = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in STICKER_EXTS]
            if not files:
                continue
            total += len(files)
            cats.append({
                "name": d.name,
                "count": len(files),
                "size_kb": round(sum(f.stat().st_size for f in files) / 1024, 1),
                "samples": [f.name for f in files[:6]],
            })
    return {"categories": cats, "total": total, "dir": str(root)}


def add_sticker_category(name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise RuntimeError("分类名不能为空")
    if len(name) > 12:
        raise RuntimeError("分类名最多 12 个字")
    if any(c in name for c in r'\/:*?"<>|'):
        raise RuntimeError("分类名不能包含 \\ / : * ? \" < > |")
    d = DATA / "stickers" / name
    if d.exists():
        raise RuntimeError(f"分类「{name}」已存在")
    d.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "name": name, "path": str(d)}


def delete_sticker_category(name: str) -> Dict[str, Any]:
    d = DATA / "stickers" / name
    if not d.exists() or not d.is_dir():
        raise RuntimeError(f"分类「{name}」不存在")
    # 只删空目录，防止误删图片
    files = [f for f in d.iterdir() if f.is_file()]
    if files:
        raise RuntimeError(f"分类里还有 {len(files)} 张图片，请先手动删除目录内的图片")
    d.rmdir()
    return {"ok": True}


def open_sticker_dir() -> Dict[str, Any]:
    d = DATA / "stickers"
    d.mkdir(parents=True, exist_ok=True)
    os.startfile(str(d))  # noqa: S606
    return {"ok": True, "path": str(d)}


# --------------------------------------------------------------------------- #
# 聊天记录
# --------------------------------------------------------------------------- #
def chat_history(key: str = "") -> List[Dict[str, Any]]:
    """从 memory/<key>.json 读对话历史。"""
    if not key:
        return []
    safe = re.sub(r"[^\w\-]", "_", key)
    f = DATA / "memory" / f"{safe}.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in d.get("history", []):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        content = str(m.get("content", ""))
        if not content:
            continue
        out.append({
            "role": "user" if role == "user" else "bot",
            "text": content,
        })
    return out


# --------------------------------------------------------------------------- #
# 配置导入导出
# --------------------------------------------------------------------------- #
def export_config() -> Dict[str, Any]:
    """导出配置（不含 API Key）。"""
    cfg = load_bot_config()
    # 剥离敏感字段
    cfg.pop("llm", None)
    llm = (load_bot_config().get("llm") or {})
    safe_llm = {k: v for k, v in llm.items() if k != "api_key"}
    cfg["llm"] = safe_llm
    cfg.setdefault("napcat", {})["dir"] = ""      # 不含本机路径

    personas = load_personas()
    memes = read_yaml(DATA / "memes.yaml")
    stk = sticker_detail()

    return {
        "ok": True,
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "personas": personas,
        "memes": memes,
        "sticker_categories": [c["name"] for c in stk["categories"]],
    }


def import_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """导入配置。只合并安全内容，不动 API Key。"""
    if not isinstance(data, dict) or "config" not in data:
        raise RuntimeError("文件格式不对，请选择由本软件导出的配置文件")

    applied = []

    # 1. 主配置（保护 api_key 与本机路径）
    incoming = data.get("config") or {}
    if isinstance(incoming, dict):
        cur = load_bot_config()
        cur_key = (cur.get("llm") or {}).get("api_key", "")
        cur_nap = (cur.get("napcat") or {}).get("dir", "")

        for section in ("llm", "behavior"):
            if isinstance(incoming.get(section), dict):
                cur.setdefault(section, {}).update(incoming[section])
        if isinstance(incoming.get("napcat"), dict):
            cur.setdefault("napcat", {}).update(incoming["napcat"])

        # 恢复本机敏感信息
        cur.setdefault("llm", {})["api_key"] = cur_key
        cur.setdefault("napcat", {})["dir"] = cur_nap
        save_bot_config(cur)
        applied.append("主配置")

    # 2. 人格库
    if isinstance(data.get("personas"), dict) and data["personas"]:
        save_personas(data["personas"])
        applied.append(f"人格库({len(data['personas'])}套)")

    # 3. 热梗库
    if isinstance(data.get("memes"), dict) and data["memes"]:
        text = yaml.safe_dump(data["memes"], allow_unicode=True, sort_keys=False,
                              default_flow_style=False, width=1000)
        (DATA / "memes.yaml").write_text(text, encoding="utf-8")
        applied.append("热梗库")

    # 4. 表情分类（只建目录，不覆盖已有）
    created = 0
    for name in (data.get("sticker_categories") or []):
        if not isinstance(name, str) or not name:
            continue
        d = DATA / "stickers" / name
        if not d.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
                created += 1
            except OSError:
                pass
    if created:
        applied.append(f"新建 {created} 个表情分类")

    return {"ok": True, "applied": applied}


def save_behavior(body: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_bot_config()
    b = cfg.setdefault("behavior", {})
    mode = str(body.get("group_reply_mode", "")).strip()
    if mode in ("at", "mention", "all"):
        b["group_reply_mode"] = mode
    if "private_enabled" in body:
        b["private_enabled"] = bool(body["private_enabled"])
    save_bot_config(cfg)
    return {"ok": True}


def clear_logs() -> Dict[str, Any]:
    if LOG_FILE.exists():
        try:
            LOG_FILE.write_text("", encoding="utf-8")
        except OSError:
            raise RuntimeError("日志文件被占用，请先停止机器人")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 版本 / 诊断 / 崩溃
# --------------------------------------------------------------------------- #
def version_info(force: bool = False) -> Dict[str, Any]:
    try:
        import updater
        return updater.check_update(ROOT, force=force)
    except Exception as exc:  # noqa: BLE001
        return {
            "current": "?",
            "latest": None,
            "has_update": False,
            "error": str(exc),
            "checked_at": time.time(),
        }


def diagnostic_report() -> str:
    try:
        import crashguard
        return crashguard.make_diagnostic_report(ROOT)
    except Exception as exc:  # noqa: BLE001
        return f"生成诊断报告失败: {exc}"


def crash_dir() -> Path:
    return LOGS / "crash"


def crash_list() -> List[Dict[str, Any]]:
    try:
        import crashguard
        return crashguard.list_crashes(crash_dir())
    except Exception:  # noqa: BLE001
        return []


def crash_read(name: str) -> str:
    try:
        import crashguard
        return crashguard.read_crash(crash_dir(), name)
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "QQBotConsole/1.0"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:  # noqa: BLE001
            return {}

    # ---------------- GET ----------------
    def do_GET(self):
        p = self.path.split("?")[0]
        try:
            if p in ("/", "/index.html"):
                html = (WEB / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if p.startswith("/assets/"):
                name = p[len("/assets/"):]
                f = ASSETS / name
                if f.exists() and f.is_file():
                    ct = "image/png" if f.suffix == ".png" else "image/x-icon"
                    return self._send(200, f.read_bytes(), ct)
                return self._err("not found", 404)

            if p == "/api/status":
                return self._json(self.status())
            if p == "/api/config/extras":
                return self._json(self.config_extras())
            if p == "/api/personas":
                return self._json(self.personas_info())
            if p == "/api/model":
                cfg = load_bot_config()
                llm = cfg.get("llm") or {}
                return self._json({
                    "base_url": llm.get("base_url", ""),
                    "api_key": llm.get("api_key", ""),
                    "model": llm.get("model", ""),
                    "fallbacks": llm.get("fallbacks") or [],
                })
            if p == "/api/models":
                return self._json({"models": fetch_models()})
            if p == "/api/gateway_check":
                return self._json(gateway_check())
            if p == "/api/logs":
                return self._json({"lines": read_logs()})
            if p == "/api/memory":
                return self._json({"sessions": memory_sessions()})
            if p == "/api/stickers":
                return self._json(sticker_detail())
            if p == "/api/history":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                key = ""
                for kv in q.split("&"):
                    if kv.startswith("key="):
                        from urllib.parse import unquote
                        key = unquote(kv[4:])
                return self._json({"messages": chat_history(key)})
            if p == "/api/export":
                return self._json(export_config())
            if p == "/api/settings":
                cfg = load_bot_config()
                b = cfg.get("behavior") or {}
                return self._json({
                    "group_reply_mode": b.get("group_reply_mode", "at"),
                    "private_enabled": bool(b.get("private_enabled", True)),
                })
            if p == "/api/version":
                return self._json(version_info(force=False))
            if p == "/api/version/check":
                return self._json(version_info(force=True))
            if p == "/api/diagnostics":
                return self._json({"report": diagnostic_report()})
            if p == "/api/crashes":
                return self._json({"crashes": crash_list()})
            if p.startswith("/api/crashes/"):
                name = p[len("/api/crashes/"):]
                return self._json({"name": name, "text": crash_read(name)})
            return self._err("not found", 404)
        except RuntimeError as e:
            return self._err(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s 失败", p)
            return self._err(f"服务端错误: {e}", 500)

    # ---------------- POST ----------------
    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._body()
        try:
            if p == "/api/action/start":
                msgs = [start_gateway(), start_bot(), start_napcat()]
                return self._json({"ok": True, "msgs": msgs})
            if p == "/api/action/stop":
                return self._json({"ok": True, "msg": stop_all()})
            if p == "/api/action/restart":
                return self._json({"ok": True, "msg": restart_bot()})
            if p == "/api/action/test_llm":
                return self._json(test_llm())
            if p == "/api/action/open_gateway":
                webbrowser.open(GW_URL)
                return self._json({"ok": True})
            if p == "/api/action/clear_memory":
                mdir = DATA / "memory"
                if mdir.exists():
                    for f in mdir.glob("*.json"):
                        f.unlink()
                return self._json({"ok": True})
            if p == "/api/action/install_gateway":
                return self._json(self.install_gateway())
            if p == "/api/action/install_napcat":
                return self._json(self.install_napcat())
            if p == "/api/persona/set":
                return self._json(self.set_persona(body.get("persona", "")))
            if p == "/api/persona/add":
                return self._json(self.add_persona(body))
            if p == "/api/persona/delete":
                return self._json(self.delete_persona(body.get("key", "")))
            if p == "/api/persona/prompt":
                return self._json(self.set_prompt(body.get("persona", ""), body.get("prompt", "")))
            if p == "/api/model":
                return self._json(self.save_model(body))
            if p == "/api/quickconfig":
                return self._json(self.quick_config(body))
            if p == "/api/stickers/add_cat":
                return self._json(add_sticker_category(body.get("name", "")))
            if p == "/api/stickers/delete_cat":
                return self._json(delete_sticker_category(body.get("name", "")))
            if p == "/api/stickers/open":
                return self._json(open_sticker_dir())
            if p == "/api/import":
                return self._json(import_config(body))
            if p == "/api/settings":
                return self._json(save_behavior(body))
            if p == "/api/logs/clear":
                return self._json(clear_logs())
            return self._err("not found", 404)
        except RuntimeError as e:
            return self._err(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s 失败", p)
            return self._err(f"服务端错误: {e}", 500)

    # ---------------- 业务逻辑 ----------------
    def status(self) -> Dict[str, Any]:
        cfg = load_bot_config()
        llm = cfg.get("llm") or {}
        nd = napcat_dir()
        personas = load_personas()
        cur = str(llm.get("persona", ""))
        st = sticker_stats()
        return {
            "ports": {"bot": port_open(BOT_PORT), "gateway": port_open(8788)},
            "bot_proc": bool(find_pids_on_port(BOT_PORT)),
            "napcat": {
                "connected": port_open(BOT_PORT) and port_established(BOT_PORT),
                "injected": count_process("QQ.exe") > 0,
                "dir": str(nd) if nd else "",
            },
            "qq_count": count_process("QQ.exe"),
            "gateway": {"up": port_open(8788)},
            "persona": cur,
            "persona_name": (personas.get(cur) or {}).get("name", cur),
            "model": llm.get("model", ""),
            "base_url": llm.get("base_url", ""),
            "stickers": st["total"],
            "sticker_cats": st["cats"],
            "memes": meme_count(),
        }

    def personas_info(self) -> Dict[str, Any]:
        ps = load_personas()
        cur = str((load_bot_config().get("llm") or {}).get("persona", ""))
        clean = {}
        for k, v in ps.items():
            if isinstance(v, dict):
                clean[k] = {
                    "name": v.get("name", k),
                    "desc": v.get("desc", ""),
                    "examples": v.get("examples") or [],
                    "builtin": k in ("tsundere", "girly", "otaku", "maid", "girlfriend", "formal"),
                }
        prompt = (ps.get(cur) or {}).get("system_prompt", "") if isinstance(ps.get(cur), dict) else ""
        return {"personas": clean, "current": cur, "prompt": prompt}

    def set_persona(self, key: str) -> Dict[str, Any]:
        if key not in load_personas():
            raise RuntimeError(f"没有人设: {key}")
        cfg = load_bot_config()
        cfg.setdefault("llm", {})["persona"] = key
        save_bot_config(cfg)
        return {"ok": True, "persona": key}

    def set_prompt(self, key: str, prompt: str) -> Dict[str, Any]:
        ps = load_personas()
        if key not in ps:
            raise RuntimeError(f"没有人设: {key}")
        if not prompt.strip():
            raise RuntimeError("提示词不能为空")
        ps[key]["system_prompt"] = prompt
        save_personas(ps)
        return {"ok": True}

    def add_persona(self, b: Dict[str, Any]) -> Dict[str, Any]:
        key = str(b.get("key", "")).strip()
        name = str(b.get("name", "")).strip()
        prompt = str(b.get("prompt", "")).strip()
        if not key:
            raise RuntimeError("请填写英文 key")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$", key):
            raise RuntimeError("key 只能包含英文字母、数字、-、_，且以字母开头")
        if not name:
            raise RuntimeError("请填写名称")
        if not prompt:
            raise RuntimeError("请填写人格提示词")
        ps = load_personas()
        if key in ps:
            raise RuntimeError(f"已存在同名人设: {key}")
        ps[key] = {
            "name": name,
            "desc": str(b.get("desc", "自定义人格")),
            "system_prompt": prompt,
            "style_hints": [s for s in str(b.get("style_hints", "")).split("\n") if s.strip()],
        }
        save_personas(ps)
        return {"ok": True, "key": key}

    def delete_persona(self, key: str) -> Dict[str, Any]:
        ps = load_personas()
        if key not in ps:
            raise RuntimeError(f"人设不存在: {key}")
        if len(ps) <= 1:
            raise RuntimeError("至少保留一个人设")
        cfg = load_bot_config()
        cur = str((cfg.get("llm") or {}).get("persona", ""))
        if key == cur:
            raise RuntimeError("不能删除正在使用的人设，请先切换到别的")
        del ps[key]
        save_personas(ps)
        return {"ok": True}

    def config_extras(self) -> Dict[str, Any]:
        """首页引导用的额外信息。"""
        # NapCat 是否可用: 配置里指定的路径有效, 或常见位置能找到
        nc = napcat_dir()
        napcat_usable = bool(nc and (nc / "napiloader.dll").exists())
        return {
            "needs_setup": not bool((load_bot_config().get("llm") or {}).get("api_key")),
            "napcat_installed": napcat_usable,
            "napcat_dir": str(nc) if nc else "",
            "gateway_installed": (Path(os.environ.get("USERPROFILE", "")) / "workbuddy-gateway" / "start-wb-proxy.bat").exists(),
            "model_providers": [
                {
                    "name": "WorkBuddy 网关 (默认, 免费额度)",
                    "base_url": "http://127.0.0.1:8788/v1",
                    "need_install": True,
                    "models": "deepseek-v4.1-flash, glm-5.3, gpt-5.6-sol 等 22 个",
                    "desc": "把 WorkBuddy 账号包装成接口，需先运行 tools\\setup-gateway.bat",
                },
                {
                    "name": "DeepSeek 官方",
                    "base_url": "https://api.deepseek.com/v1",
                    "need_install": False,
                    "models": "deepseek-chat, deepseek-reasoner",
                    "desc": "去 platform.deepseek.com 创建 API Key",
                },
                {
                    "name": "自定义 OpenAI 兼容接口",
                    "base_url": "",
                    "need_install": False,
                    "models": "任意",
                    "desc": "Ollama / One API / 其他网关 都可以",
                },
            ],
        }

    def install_gateway(self) -> Dict[str, Any]:
        """启动网关安装脚本 (独立窗口, 用户能看到进度)。"""
        bat = ROOT / "tools" / "setup-gateway.bat"
        if not bat.exists():
            raise RuntimeError("安装脚本不存在: tools\\setup-gateway.bat")
        gw_dir = Path(os.environ.get("USERPROFILE", "")) / "workbuddy-gateway"
        if (gw_dir / "start-wb-proxy.bat").exists():
            webbrowser.open("http://127.0.0.1:8788/")
            return {"ok": True, "msg": "网关已安装，已打开看板"}
        subprocess.Popen(
            ["cmd", "/c", "start", "Gateway Setup", str(bat)],
            cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return {"ok": True, "msg": "安装脚本已启动"}

    def install_napcat(self) -> Dict[str, Any]:
        """启动 NapCat 安装脚本。"""
        bat = ROOT / "tools" / "setup-napcat.bat"
        if not bat.exists():
            raise RuntimeError("安装脚本不存在: tools\\setup-napcat.bat")
        subprocess.Popen(
            ["cmd", "/c", "start", "NapCat Setup", str(bat)],
            cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return {"ok": True, "msg": "安装脚本已启动"}

    def quick_config(self, b: Dict[str, Any]) -> Dict[str, Any]:
        provider = str(b.get("provider", "")).strip()
        key = str(b.get("api_key", "")).strip()
        custom_base = str(b.get("base_url", "")).strip().rstrip("/")
        custom_model = str(b.get("model", "")).strip()

        if provider == "custom":
            if not custom_base or not custom_model:
                raise RuntimeError("自定义模式需要填写接口地址和模型名")
            base, model = custom_base, custom_model
        else:
            found = next(
                (p for p in self.config_extras()["model_providers"] if p["name"] == provider), None
            )
            if not found:
                raise RuntimeError("请选择模型来源")
            base = custom_base or found["base_url"]
            model = custom_model or (found.get("models") or "").split(",")[0].strip()

        if not key and provider != "gateway-none":
            raise RuntimeError("请填写 API Key")

        cfg = load_bot_config()
        llm = cfg.setdefault("llm", {})
        llm["base_url"] = base
        llm["model"] = model
        if key:
            llm["api_key"] = key
        save_bot_config(cfg)

        # 连通性测试 (失败不阻断, 让用户看到原因)
        # 注意: save_bot_config 已把 key 写进 secrets.yaml, test_llm 会重新合并读取
        try:
            r = test_llm()
            return {"ok": True, "model": r["model"], "reply": r["reply"], "tested": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "model": model, "tested": False, "warn": str(e)}

    def save_model(self, b: Dict[str, Any]) -> Dict[str, Any]:
        cfg = load_bot_config()
        llm = cfg.setdefault("llm", {})
        base = str(b.get("base_url", "")).strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise RuntimeError("接口地址必须以 http:// 或 https:// 开头")
        llm["base_url"] = base
        llm["api_key"] = str(b.get("api_key", "")).strip()
        llm["model"] = str(b.get("model", "")).strip()
        fb = b.get("fallbacks") or []
        llm["fallbacks"] = [str(x) for x in fb if str(x).strip()]
        save_bot_config(cfg)
        return {"ok": True}


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="QQ 机器人控制台")
    ap.add_argument("--port", type=int, default=CONSOLE_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    for d in (CONFIG_DIR, DATA, LOGS, ASSETS):
        d.mkdir(parents=True, exist_ok=True)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 58)
    print("  QQ 机器人控制台")
    print("=" * 58)
    print(f"  地址: {url}")
    print("  关闭此窗口即停止控制台 (机器人会继续运行)")
    print("=" * 58)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已关闭")


if __name__ == "__main__":
    main()
