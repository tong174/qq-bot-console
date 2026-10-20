# -*- coding: utf-8 -*-
"""生成 QQ 机器人应用图标 —— 现代 SaaS 风格。

设计语言参考 Figma / Linear / Notion：
  - 圆角方形底板（squircle 感）
  - 多层渐变
  - 中心几何图形（对话气泡 + 机器人天线）
  - 柔和内阴影与高光

输出：assets/app.png / app.ico 等（多尺寸）
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)

# 超采样画布，最后缩小获得抗锯齿
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def squircle_path(size: int, radius: float):
    """返回 squircle 的 mask（比圆角矩形更接近 iOS 图标）。"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * radius), fill=255)
    return m


def diagonal_gradient(size: int, c1, c2, c3=None):
    """三段/两段对角渐变。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            # 对角参数 t: 左上 0 -> 右下 1
            t = (x + y) / (2 * size)
            if c3 is None:
                col = lerp(c1, c2, t)
            else:
                if t < 0.5:
                    col = lerp(c1, c2, t * 2)
                else:
                    col = lerp(c2, c3, (t - 0.5) * 2)
            px[x, y] = col
    return img


def draw_icon() -> Image.Image:
    """主图标：紫色渐变底 + 白色对话气泡 + 机器人眼睛与天线。"""
    size = S

    # ---------- 1. 底色：紫 -> 靛蓝 -> 青绿 的对角渐变 ----------
    base = diagonal_gradient(
        size,
        (124, 92, 255),    # 紫罗兰
        (88, 101, 242),    # 靛蓝（Discord 同色系）
        (0, 200, 180),     # 青绿
    )

    # ---------- 2. 圆角裁切 ----------
    mask = squircle_path(size, 0.235)
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)

    # ---------- 3. 顶部柔光（玻璃质感） ----------
    gloss = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-size * 0.45, -size * 0.95, size * 1.45, size * 0.32], fill=110)
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.05))
    gloss = Image.composite(gloss, Image.new("L", (size, size), 0), mask)
    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    icon = Image.composite(white, icon, gloss.point(lambda v: int(v * 0.5)))

    # ---------- 4. 底部暗角（增加体积感） ----------
    shade = Image.new("L", (size, size), 0)
    sd = ImageDraw.Draw(shade)
    sd.ellipse([size * 0.05, size * 1.05, size * 0.95, size * 1.6], fill=70)
    shade = shade.filter(ImageFilter.GaussianBlur(size * 0.10))
    shade = Image.composite(shade, Image.new("L", (size, size), 0), mask)
    black = Image.new("RGBA", (size, size), (30, 20, 60, 255))
    icon = Image.composite(black, icon, shade)

    # ---------- 5. 主体：对话气泡（白色圆角矩形 + 小尾巴） ----------
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # 气泡主体（居中偏上）
    bx0, by0 = size * 0.20, size * 0.245
    bx1, by1 = size * 0.80, size * 0.685
    radius = size * 0.105
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=radius, fill=(255, 255, 255, 255))

    # 气泡尾巴: 简洁三角，直接与气泡底部连成一体
    # 关键: 三角形的上边要嵌进气泡内部, 不能只贴边, 否则会出现缝隙
    tail_pts = [
        (size * 0.300, size * 0.620),   # 左上根（嵌入气泡内部）
        (size * 0.470, size * 0.620),   # 右上根（嵌入气泡内部）
        (size * 0.330, size * 0.800),   # 尖端
    ]
    d.polygon(tail_pts, fill=(255, 255, 255, 255))

    # ---------- 6. 机器人的"眼睛"（两个圆点） ----------
    eye_r = size * 0.045
    eye_y = (by0 + by1) / 2 + size * 0.005
    for ex in (size * 0.39, size * 0.615):
        d.ellipse([ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r],
                  fill=(108, 92, 231, 255))

    # ---------- 7. 天线（气泡上方的小圆点 + 竖线） ----------
    ant_x = size * 0.5
    ant_top = size * 0.115
    ant_bot = by0 - size * 0.005
    d.line([(ant_x, ant_top + size * 0.035), (ant_x, ant_bot)],
           fill=(255, 255, 255, 255), width=int(size * 0.022))
    ant_r = size * 0.042
    d.ellipse([ant_x - ant_r, ant_top - ant_r + size * 0.005,
               ant_x + ant_r, ant_top + ant_r + size * 0.005],
              fill=(255, 255, 255, 255))
    # 天线小球内部高光
    d.ellipse([ant_x - ant_r * 0.45, ant_top - ant_r * 0.35,
               ant_x + ant_r * 0.15, ant_top + ant_r * 0.15],
              fill=(167, 243, 208, 255))

    # ---------- 8. 白块投影（让气泡浮起来） ----------
    sh = Image.new("L", (size, size), 0)
    ImageDraw.Draw(sh).bitmap((0, 0), layer.split()[3])
    shadow_mask = sh.filter(ImageFilter.GaussianBlur(size * 0.028))
    shadow_mask = Image.composite(
        shadow_mask.point(lambda v: int(v * 0.55)),
        Image.new("L", (size, size), 0),
        mask,
    )
    shadow_layer = Image.new("RGBA", (size, size), (20, 10, 50, 255))
    shadow_layer.putalpha(shadow_mask)
    # 下移模拟投影
    shadow_layer = shadow_layer.transform(
        (size, size), Image.AFFINE, (1, 0, 0, 0, 1, -size * 0.018), resample=Image.BICUBIC
    )
    icon = Image.alpha_composite(icon, shadow_layer)

    # 贴主体
    icon = Image.alpha_composite(icon, layer)

    return icon


def make_sizes(icon: Image.Image) -> None:
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    # 主 PNG
    icon.resize((512, 512), Image.LANCZOS).save(OUT / "app.png", "PNG", optimize=True)
    icon.resize((256, 256), Image.LANCZOS).save(OUT / "app_256.png", "PNG", optimize=True)
    # ICO（多尺寸）
    icon.resize((256, 256), Image.LANCZOS).save(
        OUT / "app.ico", format="ICO", sizes=[(s, s) for s in ico_sizes]
    )
    for s in ico_sizes:
        icon.resize((s, s), Image.LANCZOS).save(OUT / f"app_{s}.png", "PNG")
    print("图标已生成:")
    for f in sorted(OUT.glob("app*")):
        print(f"  {f.name}  {f.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    img = draw_icon()
    make_sizes(img)
