# -*- coding: utf-8 -*-
"""
表情图片转码工具：把一张 480x320 的 1-bit（黑白）PNG 表情图，转成 ESP32 固件
所用的 C 数组头文件（.h），格式与固件完全一致。

固件渲染约定（见 1.3.ino 的 drawExpression）：
    byteIdx = y * (IMG_W/8) + (x/8)
    bitIdx  = 7 - (x%8)          # 每个字节第 7 位(MSB)是行内最左像素
    总数    = 480 * 320 / 8 = 19200 字节

用法（在本目录 robot_esp32/1.3/ 下运行）：
    python png_to_h.py --src new_stable.png --slot stable
    python png_to_h.py --src new_face.png --slot focus

slot 可选：focus / reminder / curious / stable。各槽位对应文件及数组名见下表；
转码只覆盖所选槽位，其余三个表情保持不变（原 4 个表情均保留，做测试可用）。
生成后重新编译烧录固件即可显示新表情。

依赖：Pillow（pip install pillow）。不依赖 torch / whisper。
"""
import argparse
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    raise SystemExit("缺少 Pillow，请先安装：python -m pip install pillow")

IMG_W = 480
IMG_H = 320
N_BYTES = IMG_W * IMG_H // 8  # 19200

# 槽位 -> (头文件名, 数组名)
SLOTS = {
    "focus": ("image_array.h", "focusImage"),
    "reminder": ("reminder_image.h", "reminderImage"),
    "curious": ("curious_image.h", "curiousImage"),
    "stable": ("stable_image.h", "stableImage"),
}


def png_to_bytes(png_path):
    """把 1-bit PNG 读成按固件约定的 19200 字节 C 数组内容。"""
    with Image.open(png_path) as im:
        if im.size != (IMG_W, IMG_H):
            raise ValueError(
                f"图片尺寸应为 {IMG_W}x{IMG_H}，实际 {im.size[0]}x{im.size[1]}"
            )
        # 转成 1-bit，白色=1（显示白），黑色=0（关）。
        im1 = im.convert("1")

    data = bytearray(N_BYTES)
    pixels = im1.load()
    for y in range(IMG_H):
        for x in range(IMG_W):
            # convert("1") 里白色为 True(1)，黑色为 False(0)
            bit = 1 if pixels[x, y] else 0
            byte_idx = y * (IMG_W // 8) + (x // 8)
            bit_idx = 7 - (x % 8)
            if bit:
                data[byte_idx] |= 1 << bit_idx
    return bytes(data)


def bytes_to_h(data, array_name, png_name, slot):
    """把字节数组格式化成与现有 .h 一致的内容。

    focus 槽位（image_array.h）特例：它是唯一定义 IMG_W/IMG_H 宏的头，固件
    drawExpression 依赖这两个宏（第 126/132-140 行）。其它三个槽位不定义宏，
    只依赖 image_array.h 先被 include。因此 focus 必须保留 #define IMG_W/IMG_H。
    """
    lines = [f"// Auto-generated from {png_name} ({IMG_W}x{IMG_H} 1-bit)"]
    lines.append(f"// {len(data)} bytes ({len(data)/1024:.1f} KB)")
    lines.append("")
    if slot == "focus":
        # 保持与现有 image_array.h 相同的宏定义（IMG_W/IMG_H 供固件 dRawExpression 使用）。
        lines.append(f"#define IMG_W {IMG_W}")
        lines.append(f"#define IMG_H {IMG_H}")
        lines.append("")
    else:
        lines.append("#pragma once")
        lines.append("")
    lines.append(f"PROGMEM const uint8_t {array_name}[{len(data)}] = {{")

    # 每行 30 个字节值，与现有文件排版风格一致。
    for i in range(0, len(data), 30):
        chunk = data[i : i + 30]
        row = ",".join(f"0x{b:02X}" for b in chunk)
        lines.append(f"    {row},")
    lines.append("};")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="把表情 PNG 转成 ESP32 固件用的 C 数组 .h 文件"
    )
    parser.add_argument("--src", required=True, help="源 PNG 图片路径（480x320 1-bit）")
    parser.add_argument(
        "--slot",
        required=True,
        choices=sorted(SLOTS),
        help="要覆盖的表情槽位：focus / reminder / curious / stable",
    )
    parser.add_argument(
        "--out",
        default="",
        help="输出 .h 路径（默认写到本目录、覆盖所选槽位对应的文件）",
    )
    args = parser.parse_args()

    filename, array_name = SLOTS[args.slot]
    out_path = Path(args.out) if args.out else Path(__file__).resolve().parent / filename

    png_name = Path(args.src).name
    data = png_to_bytes(args.src)
    content = bytes_to_h(data, array_name, png_name, args.slot)

    out_path.write_text(content, encoding="utf-8")
    print(f"[OK] {args.slot} -> {out_path}  ({N_BYTES} bytes, 数组名 {array_name})")
    print(f"     原 {args.slot} 已生成的 {filename} 被覆盖；其余表情保留。")
    print("     重新编译并烧录固件后即可显示新表情。")


if __name__ == "__main__":
    main()
