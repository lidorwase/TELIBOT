"""
יצירת סרטון טיקטוק אנכי מתמונות המוצר: זום איטי על כל תמונה + טקסטים בעברית.
בלי מוזיקה - עדיף להוסיף סאונד טרנדי ישירות באפליקציה של טיקטוק.
"""
import io
import os
import random
import re
import logging
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

import requests
import imageio_ffmpeg
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 720, 1280
FPS = 30
SLIDE_SECONDS = 2.6
CTA_SECONDS = 3.6   # השקופית האחרונה (קריאה לפעולה) ארוכה יותר כדי שיספיקו לקרוא
ZOOM = 0.10  # כמה התמונה מתקרבת לאורך כל שקופית
MAX_SLIDES = 5

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Heebo.ttf")

WHITE = (255, 255, 255)
YELLOW = (255, 214, 0)
GRAY = (200, 200, 200)
COLORS = {"white": WHITE, "yellow": YELLOW, "gray": GRAY}
DEFAULT_CTA = [("הקישור בטלגרם", 70, "white"), ("בביו", 84, "yellow")]

HOOKS = [
    "לא תאמינו כמה זה עולה",
    "מצאתי את זה באלי אקספרס",
    "המוצר שכולם מזמינים עכשיו",
    "דיל שאסור לפספס",
    "חייבים את זה בבית",
]

# שורה בשקופית: (טקסט, גודל פונט, צבע)
Line = Tuple[str, int, Tuple[int, int, int]]


def _font(size: int) -> ImageFont.FreeTypeFont:
    # מנוע BASIC + get_display: תוצאה זהה בכל שרת (עם או בלי libraqm)
    font = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.BASIC)
    try:
        font.set_variation_by_name("ExtraBold")
    except Exception:
        pass
    return font


def _clean(text: str) -> str:
    """מסיר אימוג'ים - הפונט לא יודע לצייר אותם."""
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿️‍]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(get_display(candidate)) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _text_overlay(lines: List[Line]) -> Image.Image:
    """שכבה שקופה עם הטקסטים בתוך מלבן מעוגל כהה, בשליש העליון של המסך."""
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    if not lines:
        return overlay
    max_text_width = int(WIDTH * 0.82)

    rendered = []  # (שורה מוצגת, פונט, צבע)
    for text, size, color in lines:
        text = _clean(text)
        if not text:
            continue
        font = _font(size)
        for part in _wrap(text, font, max_text_width):
            rendered.append((get_display(part), font, color))
    if not rendered:
        return overlay

    gap = 14
    heights = [f.getbbox(t)[3] - f.getbbox(t)[1] for t, f, _ in rendered]
    widths = [f.getlength(t) for t, f, _ in rendered]
    box_w = int(max(widths)) + 70
    box_h = sum(heights) + gap * (len(rendered) - 1) + 60
    top = int(HEIGHT * 0.13)
    left = (WIDTH - box_w) // 2

    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (left, top, left + box_w, top + box_h), radius=36, fill=(0, 0, 0, 170)
    )
    y = top + 30
    for (text, font, color), h, w in zip(rendered, heights, widths):
        offset = font.getbbox(text)[1]
        draw.text(((WIDTH - w) / 2, y - offset), text, font=font, fill=color)
        y += h + gap
    return overlay


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    resized = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _background(img: Image.Image) -> Image.Image:
    bg = _cover(img, WIDTH, HEIGHT).filter(ImageFilter.GaussianBlur(28))
    return ImageEnhance.Brightness(bg).enhance(0.55)


def _download(url: str) -> Optional[Image.Image]:
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        return Image.open(io.BytesIO(res.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"סרטון: לא הצלחתי להוריד תמונה {url}: {e}")
        return None


def _fmt(value: float) -> str:
    return f"{value:.0f}" if value >= 100 or value == int(value) else f"{value:.2f}"


def build_slides_text(data: Dict, price_info, cta_lines=None) -> List[List[Line]]:
    """הטקסטים לכל שקופית: הוק, מחיר, הוכחה חברתית, קריאה לפעולה."""
    slides: List[List[Line]] = [[(random.choice(HOOKS), 64, WHITE)]]

    price, original, saving, percent = price_info(data)
    if price is not None and original:
        slides.append([
            (f"במקום {_fmt(original)} ₪", 50, GRAY),
            (f"רק {_fmt(price)} ₪!", 84, YELLOW),
            (f"הנחה של {percent}%", 54, WHITE),
        ])
    elif price is not None:
        slides.append([(f"רק {_fmt(price)} ₪!", 84, YELLOW)])

    social: List[Line] = []
    try:
        orders = int(float(str(data.get("orders") or 0)))
    except ValueError:
        orders = 0
    if orders >= 100:
        social.append((f"מעל {orders:,} הזמנות", 62, WHITE))
    rating = str(data.get("rating") or "").replace("%", "").strip()
    try:
        if float(rating) >= 80:
            social.append((f"דירוג חיובי של {float(rating):.0f}%", 52, YELLOW))
    except ValueError:
        pass
    if social:
        slides.append(social)

    slides.append([(t, size, COLORS.get(c, WHITE)) for t, size, c in (cta_lines or DEFAULT_CTA)])
    return slides


def make_tiktok_video(data: Dict, price_info, cta_lines=None) -> Optional[str]:
    """יוצר קובץ MP4 אנכי ומחזיר את הנתיב שלו (או None אם נכשל). האחריות למחוק את הקובץ - על הקורא."""
    urls = [u for u in (data.get("images") or [data.get("image")]) if u][:MAX_SLIDES]
    images = [img for img in (_download(u) for u in urls) if img]
    if not images:
        logger.warning("סרטון: אין תמונות זמינות")
        return None

    texts = build_slides_text(data, price_info, cta_lines)
    slide_count = min(max(len(texts), min(len(images), MAX_SLIDES)), MAX_SLIDES)
    # הוק ראשון, קריאה לפעולה אחרונה, שקופיות בלי טקסט באמצע אם יש יותר תמונות
    middle = texts[1:-1] + [[]] * (slide_count - len(texts))
    slide_texts = [texts[0]] + middle[: slide_count - 2] + [texts[-1]]

    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", "-movflags", "+faststart", path,
    ]
    frames_per_slide = int(SLIDE_SECONDS * FPS)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i, lines in enumerate(slide_texts):
            if i == len(slide_texts) - 1:
                frames_per_slide = int(CTA_SECONDS * FPS)
            img = images[i % len(images)]
            bg = _background(img)
            overlay = _text_overlay(lines)
            # התמונה עצמה: עד 92% מהרוחב ו-55% מהגובה, ממורכזת מעט מתחת לאמצע
            base_scale = min(WIDTH * 0.92 / img.width, HEIGHT * 0.55 / img.height)
            for f in range(frames_per_slide):
                scale = base_scale * (1 + ZOOM * f / frames_per_slide)
                fg = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
                frame = bg.copy()
                frame.paste(fg, ((WIDTH - fg.width) // 2, int(HEIGHT * 0.60 - fg.height / 2)))
                frame = Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")
                proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait(timeout=120) != 0:
            raise RuntimeError(proc.stderr.read().decode(errors="ignore")[-500:])
        return path
    except Exception as e:
        logger.error(f"יצירת סרטון נכשלה: {e}", exc_info=True)
        proc.kill()
        try:
            os.remove(path)
        except OSError:
            pass
        return None
