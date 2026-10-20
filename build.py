# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本。

生成 dist\\QQBot\\QQBot.exe（文件夹模式，启动快、不被杀软拦）。

用法:
    python build.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"


def prepare_clean_config() -> Path:
    """准备不含敏感信息的配置目录。"""
    clean = BUILD / "clean-config"
    if clean.exists():
        shutil.rmtree(clean, ignore_errors=True)
    clean.mkdir(parents=True, exist_ok=True)

    for f in (ROOT / "config").iterdir():
        if f.name in ("secrets.yaml", "local.yaml", ".update-check.json"):
            continue
        if f.is_file():
            shutil.copy2(f, clean / f.name)

    bot_yaml = clean / "bot.yaml"
    if bot_yaml.exists():
        try:
            import yaml
            data = yaml.safe_load(bot_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(data.get("llm"), dict):
                data["llm"]["api_key"] = ""
            if isinstance(data.get("napcat"), dict):
                data["napcat"]["dir"] = ""
            bot_yaml.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                               default_flow_style=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"  [WARN] 清理 bot.yaml 失败: {exc}")

    print(f"  干净配置: {clean}")
    return clean


def prepare_clean_data() -> Path:
    """准备不含运行时数据的 data 目录。"""
    clean = BUILD / "clean-data"
    if clean.exists():
        shutil.rmtree(clean, ignore_errors=True)
    shutil.copytree(
        ROOT / "data",
        clean,
        ignore=shutil.ignore_patterns("memory", ".windowprofile", "*.tmp", "__pycache__"),
    )
    (clean / "memory").mkdir(parents=True, exist_ok=True)
    print(f"  干净数据: {clean}")
    return clean


def main() -> int:
    for d in (BUILD, DIST):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    spec = ROOT / "QQBot.spec"
    if spec.exists():
        spec.unlink()

    print("=" * 60)
    print("  打包 QQ 机器人")
    print("=" * 60)

    clean_config = prepare_clean_config()
    clean_data = prepare_clean_data()

    sep = ";"
    datas = [
        f"{ROOT / 'web'}{sep}web",
        f"{clean_config}{sep}config",
        f"{ROOT / 'assets'}{sep}assets",
        f"{ROOT / 'core'}{sep}core",
        f"{clean_data}{sep}data",
        f"{ROOT / 'tools'}{sep}tools",
    ]

    hidden = ["console", "crashguard", "updater"]
    excludes = ["webview", "pythonnet", "clr", "clr_loader", "tray", "tkinter", "unittest"]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--onedir",
        "--name", "QQBot",
        "--icon", str(ROOT / "assets" / "app.ico"),
        "--paths", str(ROOT),
        str(ROOT / "app.py"),
    ]
    for d in datas:
        cmd += ["--add-data", d]
    for h in hidden:
        cmd += ["--hidden-import", h]
    for e in excludes:
        cmd += ["--exclude-module", e]

    print("打包中...")
    r = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        print("[ERROR] 打包失败")
        print(r.stdout[-3000:])
        return r.returncode

    exe = DIST / "QQBot" / "QQBot.exe"
    if exe.exists():
        size = exe.stat().st_size
        total = sum(f.stat().st_size for f in (DIST / "QQBot").rglob("*") if f.is_file())
        print()
        print("=" * 60)
        print(f"  打包完成: {exe}")
        print(f"  exe: {size / 1024 / 1024:.1f} MB   总大小: {total / 1024 / 1024:.1f} MB")
        print("=" * 60)
    else:
        print("打包完成，但未找到 exe")

    return 0


if __name__ == "__main__":
    sys.exit(main())
