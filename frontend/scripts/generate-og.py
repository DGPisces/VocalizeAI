"""
generate-og.py — VocalizeAI OG share card generator

Produces:
  frontend/public/og/og-zh.png  (1200×630, Chinese)
  frontend/public/og/og-en.png  (1200×630, English)

Design system: Apple-style design tokens
  - Accent:  #007aff (blue) / #5e5ce6 (purple)
  - BG:      linear gradient from #0a0a0f → #1a1a2e
  - Text:    #f5f5f7 (primary), #98989d (soft)
  - Font:    SFNS / Heiti SC for CJK
"""

import math
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# -- Paths -------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "..", "public", "og")
os.makedirs(OUT_DIR, exist_ok=True)

W, H = 1200, 630

# -- Colors (design system tokens, dark palette) ----------------------------
BG_TOP       = (10, 10, 15)      # very dark navy
BG_BOT       = (20, 18, 42)      # deep purple-navy
ACCENT_BLUE  = (0, 122, 255)     # #007aff
ACCENT_PURP  = (94, 92, 230)     # #5e5ce6
TEXT_PRI     = (245, 245, 247)   # #f5f5f7
TEXT_SOFT    = (152, 152, 157)   # #98989d
WHITE        = (255, 255, 255)

# -- Font paths ---------------------------------------------------------------
FONT_EN  = "/System/Library/Fonts/SFNS.ttf"
FONT_ZH  = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_MONO = "/System/Library/Fonts/SFNSMono.ttf"

# ---------------------------------------------------------------------------

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def make_gradient_bg(w, h):
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        c = lerp_color(BG_TOP, BG_BOT, t)
        draw.line([(0, y), (w, y)], fill=c)
    return img


def add_radial_glow(img, cx, cy, radius, color_rgb, alpha_max=80):
    """Overlay a soft radial glow blob."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    steps = 24
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(alpha_max * (1 - i / steps) ** 1.6)
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=color_rgb + (a,),
        )
    base = img.convert("RGBA")
    merged = Image.alpha_composite(base, overlay)
    return merged.convert("RGB")


def draw_phone_illustration(draw, img, x, y, size=200):
    """
    Simple, clean phone-call illustration:
    - Rounded-rect phone body
    - Speaker ring / signal arcs on the right
    - Small chat-bubble above phone
    Style: outline + fill using accent colors.
    """
    # -- Phone body ----------------------------------------------------------
    pw, ph = int(size * 0.44), int(size * 0.78)
    px, py = x - pw // 2, y - ph // 2

    # Shadow / glow behind phone
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for pad in range(18, 0, -2):
        a = int(60 * (1 - pad / 18))
        gd.rounded_rectangle(
            [px - pad, py - pad, px + pw + pad, py + ph + pad],
            radius=int((pw * 0.28) + pad),
            fill=ACCENT_BLUE + (a,),
        )
    glow_blur = glow.filter(ImageFilter.GaussianBlur(12))
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, glow_blur)
    img_converted = img_rgba.convert("RGB")
    # copy pixels back into the draw surface
    img.paste(img_converted)

    # Outer phone body fill
    draw.rounded_rectangle(
        [px, py, px + pw, py + ph],
        radius=int(pw * 0.28),
        fill=(30, 30, 50),
    )
    # Border
    draw.rounded_rectangle(
        [px, py, px + pw, py + ph],
        radius=int(pw * 0.28),
        outline=ACCENT_BLUE + (180,) if False else ACCENT_BLUE,
        width=2,
    )

    # Screen area (inset rect)
    scr_pad = int(pw * 0.1)
    scr_top = int(ph * 0.14)
    scr_bot = int(ph * 0.78)
    draw.rounded_rectangle(
        [px + scr_pad, py + scr_top, px + pw - scr_pad, py + scr_bot],
        radius=6,
        fill=(0, 90, 200),
    )

    # Waveform lines on screen (3 horizontal bars, representing audio)
    wline_y_offsets = [0.40, 0.50, 0.60]
    wline_widths    = [0.60, 0.80, 0.55]
    for wy_frac, ww_frac in zip(wline_y_offsets, wline_widths):
        wy = py + int(ph * wy_frac)
        ww = int((pw - 2 * scr_pad) * ww_frac)
        wx_start = px + scr_pad + int(((pw - 2 * scr_pad) - ww) / 2)
        draw.rounded_rectangle(
            [wx_start, wy - 2, wx_start + ww, wy + 2],
            radius=2,
            fill=WHITE,
        )

    # Home button / notch
    nb_w = int(pw * 0.30)
    nb_h = 4
    nb_x = px + (pw - nb_w) // 2
    nb_y = py + int(ph * 0.87)
    draw.rounded_rectangle(
        [nb_x, nb_y, nb_x + nb_w, nb_y + nb_h],
        radius=2,
        fill=(80, 80, 100),
    )

    # -- Signal arcs to the right of phone -----------------------------------
    arc_cx = px + pw + int(size * 0.07)
    arc_cy = y
    for i, (r, a) in enumerate([(30, 200), (50, 140), (70, 80)]):
        alpha_color = tuple(int(c * a // 255) for c in ACCENT_PURP)
        # PIL arc uses bounding box; draw as arc
        bb = [arc_cx - r, arc_cy - r, arc_cx + r, arc_cy + r]
        draw.arc(bb, start=-60, end=60, fill=alpha_color, width=2 + (2 - i))

    # -- Chat bubble above-right of phone ------------------------------------
    bx = px + pw - int(pw * 0.15)
    by = py - int(size * 0.18)
    bw = int(size * 0.30)
    bh = int(size * 0.17)
    draw.rounded_rectangle(
        [bx, by, bx + bw, by + bh],
        radius=10,
        fill=ACCENT_PURP,
    )
    # Tail
    tail_pts = [
        (bx + int(bw * 0.2), by + bh),
        (bx + int(bw * 0.08), by + bh + int(bh * 0.5)),
        (bx + int(bw * 0.38), by + bh),
    ]
    draw.polygon(tail_pts, fill=ACCENT_PURP)

    # Dot-dot-dot inside bubble
    dot_y = by + bh // 2
    for di in range(3):
        dx = bx + int(bw * (0.28 + di * 0.22))
        draw.ellipse([dx - 3, dot_y - 3, dx + 3, dot_y + 3], fill=WHITE)


def generate(lang: str, tagline: str, out_path: str):
    # 1. Gradient background
    img = make_gradient_bg(W, H)

    # 2. Radial glows
    img = add_radial_glow(img, cx=W // 4, cy=H // 2, radius=320, color_rgb=ACCENT_BLUE, alpha_max=55)
    img = add_radial_glow(img, cx=int(W * 0.72), cy=int(H * 0.35), radius=260, color_rgb=ACCENT_PURP, alpha_max=45)

    draw = ImageDraw.Draw(img)

    # 3. Subtle noise / grain dots (adds texture, keeps file size up)
    import random
    rng = random.Random(42)
    for _ in range(3000):
        gx = rng.randint(0, W - 1)
        gy = rng.randint(0, H - 1)
        brightness = rng.randint(18, 42)
        draw.point((gx, gy), fill=(brightness, brightness, brightness + 6))

    # 4. Thin top-bar gradient line
    for gx in range(W):
        t = gx / W
        c = lerp_color(ACCENT_BLUE, ACCENT_PURP, t)
        a = 200 if 0.05 < t < 0.95 else 80
        draw.point((gx, 0), fill=c)
        draw.point((gx, 1), fill=c)
        draw.point((gx, 2), fill=c)

    # 5. Logo + wordmark (top-left)
    logo_x, logo_y = 52, 48

    # Logo icon: rounded square with accent gradient (simulated as two overlaid rects)
    icon_sz = 44
    # Background square
    draw.rounded_rectangle(
        [logo_x, logo_y, logo_x + icon_sz, logo_y + icon_sz],
        radius=12,
        fill=ACCENT_BLUE,
    )
    # Diagonal accent overlay (top-right triangle)
    draw.polygon(
        [
            (logo_x + icon_sz // 2, logo_y),
            (logo_x + icon_sz, logo_y),
            (logo_x + icon_sz, logo_y + icon_sz // 2),
        ],
        fill=ACCENT_PURP,
    )
    # Phone glyph inside icon (simplified: two stacked rounded rects)
    glyph_x, glyph_y = logo_x + 14, logo_y + 10
    draw.rounded_rectangle(
        [glyph_x, glyph_y, glyph_x + 16, glyph_y + 24],
        radius=4,
        fill=WHITE,
    )

    # Wordmark
    try:
        font_logo = ImageFont.truetype(FONT_EN, 22)
    except Exception:
        font_logo = ImageFont.load_default()

    draw.text((logo_x + icon_sz + 12, logo_y + 11), "VocalizeAI", font=font_logo, fill=TEXT_PRI)

    # 6. Tagline (center-left, two lines)
    tag_x = 52
    tag_y = H // 2 - 70  # vertically centered leaning up

    if lang == "zh":
        # Split zh tagline at Chinese comma if present, otherwise at natural break
        line1 = "用自然语言描述电话任务"
        line2 = "AI 帮你打"
        try:
            font_tag1 = ImageFont.truetype(FONT_ZH, 54)
            font_tag2 = ImageFont.truetype(FONT_ZH, 54)
        except Exception:
            font_tag1 = font_tag2 = ImageFont.load_default()

        draw.text((tag_x, tag_y), line1, font=font_tag1, fill=TEXT_PRI)
        # Line 2: highlight "AI" in accent blue
        draw.text((tag_x, tag_y + 72), line2, font=font_tag2, fill=TEXT_PRI)
        # Accent underline under line1
        try:
            bb = draw.textbbox((tag_x, tag_y), line1, font=font_tag1)
            uw = bb[2] - bb[0]
        except Exception:
            uw = 400
        draw.rounded_rectangle(
            [tag_x, tag_y + 66, tag_x + uw, tag_y + 69],
            radius=2,
            fill=ACCENT_BLUE,
        )
    else:
        # English: two shorter lines
        line1 = "Describe a phone task"
        line2 = "in natural language."
        line3 = "The AI handles the call."
        try:
            font_tag = ImageFont.truetype(FONT_EN, 46)
            font_tag3 = ImageFont.truetype(FONT_EN, 46)
        except Exception:
            font_tag = font_tag3 = ImageFont.load_default()

        draw.text((tag_x, tag_y - 20), line1, font=font_tag, fill=TEXT_PRI)
        draw.text((tag_x, tag_y + 38), line2, font=font_tag, fill=TEXT_PRI)
        # Third line accent colored
        draw.text((tag_x, tag_y + 96), line3, font=font_tag3, fill=ACCENT_BLUE)

    # 7. Sub-caption
    cap_y = tag_y + (200 if lang == "zh" else 175)
    cap_text = "VocalizeAI" if False else (
        "AI 电话助手 · 浏览器音频桥" if lang == "zh" else "Browser audio bridge · AI phone agent"
    )
    try:
        font_cap = ImageFont.truetype(FONT_EN if lang == "en" else FONT_ZH, 18)
    except Exception:
        font_cap = ImageFont.load_default()

    draw.text((tag_x, cap_y), cap_text, font=font_cap, fill=TEXT_SOFT)

    # 8. Phone illustration (right side)
    ill_cx = int(W * 0.795)
    ill_cy = int(H * 0.50)
    draw_phone_illustration(draw, img, ill_cx, ill_cy, size=220)

    # 9. Bottom domain strip
    strip_y = H - 48
    try:
        font_url = ImageFont.truetype(FONT_MONO, 15)
    except Exception:
        font_url = ImageFont.load_default()
    draw.text((tag_x, strip_y), "vocalize.example.com", font=font_url, fill=TEXT_SOFT)

    # 10. Decorative divider line (vertical, separating text from illustration)
    div_x = int(W * 0.60)
    for gy in range(int(H * 0.12), int(H * 0.88)):
        alpha = 0.35 * math.sin(math.pi * (gy - H * 0.12) / (H * 0.76))
        a = int(alpha * 255)
        # Draw as faint vertical line
        px_val = img.getpixel((div_x, gy))
        blended = tuple(int(px_val[c] * (1 - alpha) + ACCENT_PURP[c] * alpha) for c in range(3))
        img.putpixel((div_x, gy), blended)

    # Save
    img.save(out_path, "PNG", optimize=True, compress_level=6)
    size_kb = os.path.getsize(out_path) // 1024
    print(f"  Saved {out_path}  ({size_kb} KB)")


if __name__ == "__main__":
    print("Generating OG cards...")
    generate(
        lang="zh",
        tagline="用自然语言描述电话任务，AI 帮你打",
        out_path=os.path.join(OUT_DIR, "og-zh.png"),
    )
    generate(
        lang="en",
        tagline="Describe a phone task in natural language. The AI handles the call.",
        out_path=os.path.join(OUT_DIR, "og-en.png"),
    )
    print("Done.")
