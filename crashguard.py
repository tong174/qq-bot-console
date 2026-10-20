# -*- coding: utf-8 -*-
"""崩溃捕获与诊断报告。

功能:
  1. 全局异常钩子: 捕获未处理异常, 写入 crash 日志
  2. 诊断报告: 汇总环境信息 + 最近日志 + 配置状态, 供用户反馈问题
  3. 自动清理: 只保留最近 20 个崩溃记录

用法:
    from crashguard import install_guard, make_diagnostic_report
    install_guard(ROOT, LOGS)
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_installed = False
_crash_dir: Optional[Path] = None
_app_dir: Optional[Path] = None


def _safe_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _crash_filename() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"crash-{ts}.txt"


def _format_crash(exc_type, exc_value, exc_tb) -> str:
    lines = [
        "=" * 70,
        "QQ 机器人 崩溃报告",
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        "",
        "【异常】",
        f"{exc_type.__name__}: {exc_value}",
        "",
        "【调用栈】",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        "",
        "【环境】",
        *_env_lines(),
        "",
        "【建议】",
        "  把这份文件的完整内容发给开发者，能大幅加快定位。",
        "  文件位置: " + str(_crash_dir or "?"),
    ]
    return "\n".join(lines)


def _env_lines() -> List[str]:
    lines = [
        f"Python   : {sys.version.split()[0]}",
        f"平台     : {platform.platform()}",
        f"架构     : {platform.machine()}",
        f"打包运行 : {getattr(sys, 'frozen', False)}",
        f"工作目录 : {os.getcwd()}",
        f"可执行   : {sys.executable}",
    ]
    return lines


def install_guard(app_dir: Path, crash_dir: Path) -> None:
    """安装全局异常钩子。"""
    global _installed, _crash_dir, _app_dir
    if _installed:
        return
    _installed = True
    _crash_dir = crash_dir
    _app_dir = app_dir

    def _hook(exc_type, exc_value, exc_tb):
        # Ctrl+C 不算崩溃
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            text = _format_crash(exc_type, exc_value, exc_tb)
            _safe_write(crash_dir / _crash_filename(), text)
            _cleanup_old_crashes(crash_dir)
        except Exception:  # noqa: BLE001
            pass
        # 仍然打印到 stderr (打包后可能看不到)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    # 子线程里的未捕获异常也要记录
    try:
        import threading

        def _thread_hook(args):
            if args.exc_type is SystemExit:
                return
            _hook(args.exc_type, args.exc_value, args.exc_traceback)

        threading.excepthook = _thread_hook
    except AttributeError:
        pass


def _cleanup_old_crashes(crash_dir: Path, keep: int = 20) -> None:
    try:
        files = sorted(crash_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def list_crashes(crash_dir: Path) -> List[Dict[str, Any]]:
    """列出崩溃记录。"""
    out: List[Dict[str, Any]] = []
    try:
        for f in sorted(crash_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({
                "name": f.name,
                "size": st.st_size,
                "time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def read_crash(crash_dir: Path, name: str) -> str:
    """读取一条崩溃记录的全文 (限制大小)。"""
    f = crash_dir / name
    if not f.exists() or not f.name.startswith("crash-"):
        return ""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
        return text[:20000]
    except Exception:  # noqa: BLE001
        return ""


def make_diagnostic_report(root: Path) -> str:
    """生成一份可复制的诊断报告 (用于反馈问题)。"""
    lines: List[str] = [
        "=" * 70,
        "QQ 机器人 诊断报告",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        "",
        "【环境】",
        *_env_lines(),
        f"安装目录 : {root}",
        "",
    ]

    # 依赖版本
    lines.append("【依赖】")
    for mod in ("websockets", "yaml", "PIL"):
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "?")
        except ImportError:
            v = "未安装"
        lines.append(f"{mod:<12}: {v}")
    lines.append("")

    # 配置摘要 (不泄露 key)
    lines.append("【配置摘要】")
    try:
        import yaml as _yaml

        cfg_path = root / "config" / "bot.yaml"
        if cfg_path.exists():
            cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            llm = cfg.get("llm") or {}
            nap = cfg.get("napcat") or {}
            beh = cfg.get("behavior") or {}
            lines += [
                f"接口地址 : {llm.get('base_url', '-')}",
                f"模型     : {llm.get('model', '-')}",
                f"备用模型 : {', '.join(llm.get('fallbacks') or []) or '-'}",
                f"当前人格 : {llm.get('persona', '-')}",
                f"连接模式 : {nap.get('mode', '-')}",
                f"群聊模式 : {beh.get('group_reply_mode', '-')}",
                f"Key 已配 : {'是' if llm.get('api_key') else '否'}",
            ]
        else:
            lines.append("bot.yaml 不存在")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"读取配置失败: {exc}")
    lines.append("")

    # 资源统计
    lines.append("【资源】")
    try:
        stk = root / "data" / "stickers"
        n_cat = n_img = 0
        if stk.exists():
            for d in stk.iterdir():
                if d.is_dir():
                    n = sum(1 for f in d.iterdir()
                            if f.suffix.lower() in (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"))
                    if n:
                        n_cat += 1
                        n_img += n
        lines.append(f"表情包   : {n_img} 张 / {n_cat} 类")

        mem = root / "data" / "memory"
        n_mem = len(list(mem.glob("*.json"))) if mem.exists() else 0
        lines.append(f"记忆会话 : {n_mem} 个")

        p_path = root / "data" / "personas.yaml"
        if p_path.exists():
            ps = _yaml.safe_load(p_path.read_text(encoding="utf-8")) or {}
            lines.append(f"人格数量 : {len(ps)}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"统计失败: {exc}")
    lines.append("")

    # 最近日志
    lines.append("【最近日志 (最多 60 行)】")
    log_file = root / "logs" / "bot.log"
    if log_file.exists():
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            tail = text.splitlines()[-60:]
            lines += tail
        except Exception as exc:  # noqa: BLE001
            lines.append(f"读取日志失败: {exc}")
    else:
        lines.append("(没有日志文件)")
    lines.append("")

    # 崩溃记录
    crashes = list_crashes(root / "logs" / "crash")
    if crashes:
        lines.append(f"【崩溃记录】共 {len(crashes)} 条，最近一次 {crashes[0]['time']}")
    else:
        lines.append("【崩溃记录】无")

    lines += ["", "=" * 70, "报告结束"]
    return "\n".join(lines)
