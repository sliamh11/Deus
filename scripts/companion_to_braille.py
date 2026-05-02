#!/usr/bin/env python3
"""Convert pixel art to braille/block terminal art for TUI startup screens.

Usage:
    python3 scripts/companion_to_braille.py IMAGE_PATH [--size compact|hero|mini] [--mode braille|halfblock]
    python3 scripts/companion_to_braille.py IMAGE_PATH --all
"""
import argparse
import re
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("pip3 install Pillow", file=sys.stderr)
    sys.exit(1)

SIZES = {
    "mini":    (20, 26),
    "compact": (36, 48),
    "hero":    (52, 68),
    "large":   (70, 92),
}

ANSI_MAP = {
    "ember":     166,
    "flame":     215,
    "deep_teal": 30,
    "ocean":     44,
    "shadow":    130,
    "night":     234,
    "brown":     94,
    "pink":      217,
    "white":     255,
}

BRAND_COLORS = {
    "ember":     (0xE8, 0x72, 0x3A),
    "flame":     (0xF4, 0xA2, 0x61),
    "deep_teal": (0x1B, 0x7A, 0x6E),
    "ocean":     (0x2E, 0xC4, 0xB6),
    "shadow":    (0xC4, 0x5A, 0x2A),
    "night":     (0x1A, 0x1A, 0x2E),
    "brown":     (0x8B, 0x5E, 0x3C),
}


def rgb_distance(c1, c2):
    return sum((a - b) ** 2 for a, b in zip(c1, c2))


def classify_pixel(r, g, b, a):
    if a < 80:
        return None
    # Background detection: near-white or very light pixels are transparent
    if r > 220 and g > 220 and b > 220:
        return None
    # Light gray checker pattern from PNG transparency
    if r > 190 and g > 190 and b > 190 and abs(r - g) < 10 and abs(g - b) < 10:
        return None
    # Pink cheeks
    if r > 200 and 140 < g < 190 and 100 < b < 170:
        return "pink"
    return min(BRAND_COLORS, key=lambda k: rgb_distance((r, g, b), BRAND_COLORS[k]))


def image_to_braille(img, width, height):
    img = img.convert("RGBA").resize((width, height), Image.NEAREST)
    pixels = img.load()
    lines = []
    for y in range(0, height, 4):
        line_chars = []
        for x in range(0, width, 2):
            dots = 0
            colors_seen = {}
            dot_offsets = [(0,0,0x01), (0,1,0x02), (0,2,0x04), (0,3,0x40),
                           (1,0,0x08), (1,1,0x10), (1,2,0x20), (1,3,0x80)]
            for dx, dy, bit in dot_offsets:
                px, py = x + dx, y + dy
                if px < width and py < height:
                    r, g, b, a = pixels[px, py]
                    color_name = classify_pixel(r, g, b, a)
                    if color_name is not None:
                        dots |= bit
                        colors_seen[color_name] = colors_seen.get(color_name, 0) + 1
            braille_char = chr(0x2800 + dots)
            if colors_seen:
                dominant = max(colors_seen, key=colors_seen.get)
                ansi_code = ANSI_MAP.get(dominant, 255)
                line_chars.append(f"\x1b[38;5;{ansi_code}m{braille_char}\x1b[0m")
            else:
                line_chars.append(" ")
        lines.append("".join(line_chars))
    return lines


def image_to_halfblock(img, width, height):
    img = img.convert("RGBA").resize((width, height), Image.NEAREST)
    pixels = img.load()
    lines = []
    for y in range(0, height, 2):
        line_chars = []
        for x in range(width):
            r1, g1, b1, a1 = pixels[x, y]
            if y + 1 < height:
                r2, g2, b2, a2 = pixels[x, y + 1]
            else:
                r2, g2, b2, a2 = 0, 0, 0, 0
            top_vis = classify_pixel(r1, g1, b1, a1) is not None
            bot_vis = classify_pixel(r2, g2, b2, a2) is not None
            if top_vis and bot_vis:
                line_chars.append(f"\x1b[38;2;{r1};{g1};{b1};48;2;{r2};{g2};{b2}m▀\x1b[0m")
            elif top_vis:
                line_chars.append(f"\x1b[38;2;{r1};{g1};{b1}m▀\x1b[0m")
            elif bot_vis:
                line_chars.append(f"\x1b[38;2;{r2};{g2};{b2}m▄\x1b[0m")
            else:
                line_chars.append(" ")
        lines.append("".join(line_chars))
    return lines


def strip_ansi(text):
    return re.sub(r'\x1b\[[^m]*m', '', text)


def main():
    parser = argparse.ArgumentParser(description="Convert pixel art to terminal art")
    parser.add_argument("image", help="Path to input PNG image")
    parser.add_argument("--size", choices=SIZES.keys(), default="compact")
    parser.add_argument("--mode", choices=["braille", "halfblock"], default="braille")
    parser.add_argument("--plain", action="store_true", help="Strip ANSI color codes")
    parser.add_argument("--all", action="store_true", help="Show all sizes and modes")
    args = parser.parse_args()

    img_path = Path(args.image)
    if not img_path.exists():
        print(f"Not found: {img_path}", file=sys.stderr)
        sys.exit(1)

    img = Image.open(img_path)

    if args.all:
        for size_name, (w, h) in SIZES.items():
            for mode in ["braille", "halfblock"]:
                print(f"\n{'='*40}")
                print(f"  {mode.upper()} — {size_name} ({w}x{h})")
                print(f"{'='*40}\n")
                convert = image_to_braille if mode == "braille" else image_to_halfblock
                for line in convert(img, w, h):
                    out = strip_ansi(line) if args.plain else line
                    print(f"  {out}")
                print()
        return

    w, h = SIZES[args.size]
    convert = image_to_braille if args.mode == "braille" else image_to_halfblock
    for line in convert(img, w, h):
        out = strip_ansi(line) if args.plain else line
        print(f"  {out}")


if __name__ == "__main__":
    main()
