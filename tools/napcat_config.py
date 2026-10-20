# -*- coding: utf-8 -*-
"""为 NapCat 写入 OneBot 反向 WebSocket 配置。

用法:
    python napcat_config.py <NapCat目录> <项目根目录>

它会找到 config/onebot11_<QQ号>.json, 写入 websocketClients 指向本机 3001 端口。
同时更新 bot.yaml 的 napcat.dir。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

WS_URL = "ws://127.0.0.1:3001"


def update_bot_yaml(root: Path, napcat_dir: Path) -> None:
    cfg_path = root / "config" / "bot.yaml"
    if not cfg_path.exists():
        print(f"  [WARN] 找不到 {cfg_path}, 跳过")
        return
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] 读取 bot.yaml 失败: {exc}")
        return
    cfg.setdefault("napcat", {})["dir"] = str(napcat_dir)
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    cfg_path.write_text(text, encoding="utf-8")
    print(f"  已更新 bot.yaml: napcat.dir = {napcat_dir}")


def patch_onebot(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] {path.name} 解析失败: {exc}")
        return False

    net = data.setdefault("network", {})
    clients = net.setdefault("websocketClients", [])

    # 已有指向 3001 的配置就更新, 否则新增
    for c in clients:
        if isinstance(c, dict) and "3001" in str(c.get("url", "")):
            c["enable"] = True
            c["url"] = WS_URL
            c["messagePostFormat"] = "array"
            print(f"  {path.name}: 已更新现有配置 -> {WS_URL}")
            break
    else:
        clients.append({
            "name": "qq-bot",
            "enable": True,
            "url": WS_URL,
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "reconnectInterval": 5000,
            "token": "",
            "debug": False,
            "heartInterval": 30000,
        })
        print(f"  {path.name}: 已写入反向 WS 配置 -> {WS_URL}")

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def patch_webui(path: Path) -> None:
    """把 WebUI 密码改成好记的, 方便用户登录。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if data.get("token") and len(str(data.get("token"))) > 20:
        # 已是自定义密码, 不动
        return
    data["token"] = "qqbot123"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  WebUI 密码已设为: qqbot123")


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python napcat_config.py <NapCat目录> <项目根目录>")
        return 1

    napcat_dir = Path(sys.argv[1])
    root = Path(sys.argv[2])
    cfg_dir = napcat_dir / "config"

    print(f"NapCat 目录: {napcat_dir}")
    if not napcat_dir.exists():
        print("  [ERROR] 目录不存在")
        return 1

    update_bot_yaml(root, napcat_dir)

    if not cfg_dir.exists():
        print(f"  [i] 还没有 config 目录, 首次登录 QQ 后会自动生成")
        print(f"      登录后重新运行本脚本, 或直接在 NapCat WebUI 里配")
        return 0

    onebots = sorted(cfg_dir.glob("onebot11_*.json"))
    if not onebots:
        print("  [i] 还没有 onebot11_<QQ号>.json")
        print("      先登录一次 QQ, 再重新运行 tools\\setup-napcat.bat")
        return 0

    ok = 0
    for f in onebots:
        if patch_onebot(f):
            ok += 1

    webui = cfg_dir / "webui.json"
    if webui.exists():
        patch_webui(webui)

    print(f"\n完成: 已配置 {ok} 个 QQ 账号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
