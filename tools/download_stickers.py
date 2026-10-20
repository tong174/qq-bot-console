# -*- coding: utf-8 -*-
"""下载 Apple 风格 emoji 表情包到 stickers/ 目录。独立运行, 不依赖 bot.py。"""
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent / "stickers"
CDN = "https://unpkg.com/emoji-datasource-apple@15.0.1/img/apple/64/{}.png"

CATS = {
    "笑死": ["1f602", "1f923", "1f606", "1f605", "1f604", "1f642"],
    "无语": ["1f611", "1f636", "1f644", "1f610", "1f612", "1f62c"],
    "生气": ["1f620", "1f621", "1f624", "1f92c", "1f47f", "1f4a2"],
    "抱抱": ["1f917", "1f970", "1f932", "1f91d", "1fae1"],
    "撒娇": ["1f97a", "1f63d", "1f431", "1f430", "1f63b"],
    "委屈": ["1f622", "1f62d", "1f97e", "1f625", "1f614"],
    "晚安": ["1f634", "1f31a", "1f319", "1f4a4", "1f31c", "1f6cc"],
    "加油": ["1f4aa", "1f44d", "1f525", "26a1", "1f31f"],
    "害羞": ["1f633", "1f60a", "1f60d", "1f929", "1f975", "1f978"],
    "比心": ["1f496", "1f495", "1f497", "1f49b", "1f49c"],
    "哭": ["1f62d", "1f62a", "1f614", "1f972", "1f630"],
    "震惊": ["1f631", "1f628", "1f626", "1f914", "1f92f"],
    "思考": ["1f914", "1f9d0", "1f440", "1f643"],
    "开心": ["2728", "1f389", "1f38a", "1f917", "1f60e", "1f929"],
    "得意": ["1f60f", "1f61c", "1f608", "1f60e", "1f917"],
    "摸鱼": ["1f41f", "1f9a5", "1f634", "1f4a4", "1f62a"],
    "饿了": ["1f924", "1f37d", "1f35c", "1f96b", "1f358"],
    "666": ["1f44f", "1f64c", "1f3c6", "1f947", "1f4af"],
    "佩服": ["1f64c", "1f642", "1f44f", "1f44d"],
    "害怕": ["1f628", "1f630", "1f976", "1f629", "1f613"],
    "狗头": ["1f436", "1f415", "1f43e"],
    "舔狗": ["1f436", "1f415", "1f43e"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://unpkg.com/",
    "Accept": "image/png,image/*,*/*",
}


def main() -> int:
    total = ok = 0
    failed = []
    for cat, codes in CATS.items():
        folder = BASE / cat
        folder.mkdir(parents=True, exist_ok=True)
        for code in dict.fromkeys(codes):
            total += 1
            dest = folder / f"{cat}_{code}.png"
            if dest.exists() and dest.stat().st_size > 500:
                ok += 1
                continue
            try:
                req = urllib.request.Request(CDN.format(code), headers=HEADERS)
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = r.read()
                if len(data) < 500:
                    raise ValueError(f"too small: {len(data)}")
                dest.write_bytes(data)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append((cat, code, str(exc)[:50]))
            time.sleep(0.05)

    print(f"downloaded {ok}/{total}")
    if failed:
        print(f"failed {len(failed)}:")
        for f in failed[:12]:
            print("  ", f)

    # 校验
    lib = {}
    for d in BASE.iterdir():
        if d.is_dir():
            n = sum(1 for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"))
            if n:
                lib[d.name] = n
    print(f"\nom categories {len(lib)}, total {sum(lib.values())} images")
    for k, v in sorted(lib.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
