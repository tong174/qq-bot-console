"""QQ 机器人桌面应用入口。

窗口方案：调用系统浏览器以 --app 模式打开控制台（无边框，像桌面软件）。

关键设计：
  - 多浏览器依次尝试，每个都验证渲染进程（无 renderer = 白屏，跳过）
  - 单实例：第二次双击只唤起窗口
  - 主进程常驻：open_window 出异常也不退出
  - 日志写 logs/app.log，用户可见
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 打包后 _internal 是只读目录，运行时数据放 exe 旁边
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = ROOT

URL = "http://127.0.0.1:8789"
PORT = 8789
APP_TITLE = "QQ 机器人"


# --------------------------------------------------------------------------- #
def _state_dir() -> Path:
    base = APP_DIR if getattr(sys, "frozen", False) else ROOT
    d = base / "runtime"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001
        d = Path(tempfile.gettempdir()) / "QQBot-runtime"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    for path in (APP_DIR / "logs" / "app.log", _state_dir() / "app.log"):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
            return
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------- #
def port_listening(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server() -> None:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.path.insert(0, str(ROOT))
    import console as console_mod

    srv = console_mod.ThreadingHTTPServer(("127.0.0.1", PORT), console_mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(60):
        if port_listening():
            _log("控制台服务已就绪")
            return
        time.sleep(0.2)
    raise RuntimeError("控制台服务启动超时")


# --------------------------------------------------------------------------- #
def list_browsers() -> list:
    paths = []
    for c in [
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tencent", "QQBrowser", "QQBrowser.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "360", "360se6", "Application", "360se.exe"),
    ]:
        if c and os.path.exists(c) and c not in paths:
            paths.append(c)
    _log(f"可用浏览器: {[Path(p).name for p in paths]}")
    return paths


def _find_console_window():
    """查找已打开的控制台窗口（按标题匹配），返回 hwnd 或 None。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value
        if t == APP_TITLE or t.startswith(APP_TITLE) or "127.0.0.1:8789" in t:
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


def _focus_window(hwnd) -> None:
    import ctypes

    try:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _verify_page_ready(timeout: float = 15.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL + "/", timeout=2) as r:
                if r.status == 200 and b"<title>" in r.read(2000):
                    _log("页面自检通过")
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    _log("页面自检失败")
    return False


def _launch_browser_app(browser: str) -> bool:
    """以 --app 模式启动浏览器（普通 Popen，不带特殊 creationflags）。"""
    try:
        subprocess.Popen(
            [browser, f"--app={URL}", "--window-size=1280,880",
             "--no-first-run", "--no-default-browser-check"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"启动 {Path(browser).name} 失败: {exc!r}")
        return False


def open_window(force: bool = False, quick: bool = False) -> None:
    """打开控制台窗口。

    三层降级，每层之间用"窗口是否真的出现"验证；全程不 kill 任何进程：
      1. Chrome --app
      2. Edge --app
      3. explorer.exe 打开 URL（走系统 shell，用户会话里必然可见）
    """
    _log(f"open_window(force={force})")

    hwnd = _find_console_window()
    if hwnd is not None and not force:
        _log("已有窗口，置前")
        _focus_window(hwnd)
        return

    if not _verify_page_ready():
        _log("页面不可用，放弃开窗口")
        return

    browsers = [b for b in list_browsers() if "firefox" not in Path(b).name.lower()]

    if quick:
        # 第二实例专用：快速唤起，最多等 2.5 秒，然后交给调用方退出
        if browsers:
            _launch_browser_app(browsers[0])
            deadline = time.time() + 2.5
            while time.time() < deadline:
                h2 = _find_console_window()
                if h2 is not None:
                    _focus_window(h2)
                    _log("quick: 窗口已置前")
                    return
                time.sleep(0.4)
        _log("quick: cmd start + startfile 打开")
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", URL],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=0x08000000)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.startfile(URL)  # noqa: S606
        except Exception:  # noqa: BLE001
            pass
        return

    for idx, browser in enumerate(browsers[:2], 1):
        name = Path(browser).name
        _log(f"[层{idx}] {name} --app")
        if not _launch_browser_app(browser):
            continue
        # 等窗口出现（最多 6 秒）
        deadline = time.time() + 6
        while time.time() < deadline:
            if _find_console_window() is not None:
                _log(f"[层{idx}] 窗口已出现")
                return
            time.sleep(0.5)
        _log(f"[层{idx}] 未出现窗口，进入下一层（不 kill）")

    # 层3: cmd /c start（console 进程链，实测在用户会话里成功过）
    _log("[层3] cmd /c start URL")
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", URL],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"cmd start 失败: {exc!r}")
    deadline = time.time() + 4
    while time.time() < deadline:
        if _find_console_window() is not None:
            _log("[层3] 窗口已出现")
            return
        time.sleep(0.5)

    # 层4: os.startfile（与 console.py  proven 调用一致）
    _log("[层4] os.startfile URL")
    try:
        os.startfile(URL)  # noqa: S606
    except Exception as exc:  # noqa: BLE001
        _log(f"startfile 失败: {exc!r}")
    deadline = time.time() + 4
    while time.time() < deadline:
        if _find_console_window() is not None:
            _log("[层4] 窗口已出现")
            return
        time.sleep(0.5)

    # 层5: 原生 MessageBox 提示，保证用户永远得到反馈
    _log("[层5] 弹提示框")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "控制台服务已启动，但没能自动打开浏览器窗口。\n\n"
            "请手动在浏览器访问：\nhttp://127.0.0.1:8789\n\n"
            "或改用目录里的「启动QQ机器人.bat」启动。",
            APP_TITLE,
            0x40,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"MessageBox 失败: {exc!r}")


def _idle() -> int:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- #
def _setup_temp() -> None:
    """把 TEMP 指到用户目录下的可写路径。

    PyInstaller onefile 的引导程序会在 %TEMP% 解压，若系统 TEMP 被沙箱 ACL
    限制会弹 "Could not create temporary directory!" 并直接退出。
    必须在写日志前完成。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    target = Path(base) / "QQBot" / "temp"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".ok"
        probe.write_text("1", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        return
    os.environ["TEMP"] = str(target)
    os.environ["TMP"] = str(target)
    try:
        tempfile.tempdir = str(target)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _setup_temp()
    _log("=== main ===")
    _log(f"frozen={getattr(sys, 'frozen', False)} APP_DIR={APP_DIR}")
    _log(f"TEMP={os.environ.get('TEMP')}")

    try:
        sys.path.insert(0, str(ROOT))
        import crashguard
        cd = _state_dir() / "crash"
        cd.mkdir(parents=True, exist_ok=True)
        crashguard.install_guard(APP_DIR, cd)
    except Exception as exc:  # noqa: BLE001
        _log(f"崩溃保护失败: {exc!r}")

    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true")
    args = ap.parse_args()

    # 单实例：端口为准
    if port_listening():
        _log("端口已占用 -> 已有实例，快速唤起后退出")
        open_window(force=True, quick=True)
        return 0

    try:
        import msvcrt
        lock = _state_dir() / "instance.lock"
        lock.touch(exist_ok=True)
        fh = open(lock, "r+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            _log("已获得实例锁")
        except OSError:
            _log("锁被占用（继续）")
    except Exception as exc:  # noqa: BLE001
        _log(f"锁文件不可用: {exc!r}（继续）")

    try:
        start_server()
    except Exception as exc:  # noqa: BLE001
        _log(f"服务启动失败: {exc!r}")
        return 1

    if args.browser:
        import webbrowser
        webbrowser.open(URL)
        return _idle()

    try:
        open_window()
    except Exception:  # noqa: BLE001
        import traceback
        _log(f"open_window 异常: {traceback.format_exc()[:800]}")

    _log("主循环开始（常驻）")
    return _idle()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
