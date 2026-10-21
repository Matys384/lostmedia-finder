"""Render docs/architecture.png and docs/user-flow.png (used for the YouTube API audit form)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1]/"docs"
W, H = 1920, 1080
INK, MUTED, LINE = (24, 28, 36), (90, 98, 112), (70, 80, 96)
FILL = {"api": (225, 236, 252), "local": (232, 245, 233), "store": (255, 243, 224), "person": (243, 232, 250)}


def font(size, bold=False):
    name = "arialbd.ttf" if bold else "arial.ttf"
    try:
        return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
    except OSError:
        return ImageFont.load_default()


def box(d, x, y, w, h, title, lines, kind):
    d.rounded_rectangle((x, y, x+w, y+h), 18, fill=FILL[kind], outline=LINE, width=3)
    d.text((x+20, y+16), title, font=font(30, True), fill=INK)
    for i, line in enumerate(lines):
        d.text((x+20, y+64+i*34), line, font=font(24), fill=MUTED)
    return (x, y, w, h)


def arrow(d, a, b, label="", side="right"):
    ax, ay = a
    bx, by = b
    d.line((ax, ay, bx, by), fill=LINE, width=4)
    import math
    angle = math.atan2(by-ay, bx-ax)
    for s in (2.6, -2.6):
        d.line((bx, by, bx-22*math.cos(angle+s/6), by-22*math.sin(angle+s/6)), fill=LINE, width=4)
    if label:
        d.text((min(ax, bx)+8, (ay+by)/2-32), label, font=font(22), fill=INK)


def architecture():
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.text((60, 36), "Lostmedia Finder — architecture (runs locally, no server, no user accounts)", font=font(40, True), fill=INK)
    yt = box(d, 60, 140, 520, 250, "YouTube Data API v3", ["read-only, API key (no OAuth)", "search.list (date windows, keywords)", "videos.list (duration, channel)", "channels / playlistItems.list"], "api")
    other = box(d, 60, 440, 520, 200, "Dailymotion & Internet Archive", ["public search APIs", "duration / date filters"], "api")
    disc = box(d, 700, 140, 560, 250, "Discovery + pacing", ["date-window frontier, resumable", "runtime target 5:12 +-5 s", "quota counted in units (< 10k/day)", "429 / Retry-After / anti-bot = pause"], "local")
    db = box(d, 700, 440, 560, 200, "Local SQLite catalog", ["public IDs, titles, dates, durations", "jobs, priorities, results"], "store")
    work = box(d, 1380, 140, 480, 330, "Analysis worker", ["YouTube: API thumbnail only,", "never the video", "Archive/Dailymotion: temporary", "file, deleted after analysis", "GPU DINOv2 + checks vs. reference"], "local")
    ui = box(d, 1380, 540, 480, 230, "Local review page", ["http://127.0.0.1 only", "YouTube candidates by thumbnail,", "links to youtube.com watch page"], "local")
    user = box(d, 700, 760, 560, 200, "Researcher (local user)", ["reviews candidates", "follows link to the original page"], "person")
    arrow(d, (580, 265), (700, 265), "metadata")
    arrow(d, (580, 540), (700, 540))
    arrow(d, (980, 390), (980, 440))
    arrow(d, (1260, 540), (1380, 330), "queue")
    arrow(d, (1620, 470), (1620, 540), "results")
    arrow(d, (1380, 680), (1260, 860))
    d.text((60, 1000), "Stored: public metadata and compact visual fingerprints only. No YouTube data is published, sold or shared; nothing leaves the user's machine.", font=font(24), fill=MUTED)
    im.save(OUT/"architecture.png")


def user_flow():
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.text((60, 36), "Lostmedia Finder — user flow", font=font(40, True), fill=INK)
    steps = [("1. Install", ["clone the public repository", "python + ffmpeg, own API key in .env"]),
             ("2. Add reference", ["import the known clip of", "the lost video (local file)"]),
             ("3. Discover", ["lostmedia target-discover", "API calls paced, quota-capped"]),
             ("4. Analyze", ["YouTube: score API thumbnails", "(no video download)"]),
             ("5. Review", ["127.0.0.1/candidates: thumbnails,", "duplicates grouped, mark verdict"]),
             ("6. Watch on YouTube", ["open the original watch page", "(youtube.com/watch?v=...)"])]
    x0, y0, w, h, gap = 60, 200, 560, 230, 70
    for i, (title, lines) in enumerate(steps):
        col, row = i % 3, i//3
        x, y = x0+col*(w+gap), y0+row*(h+150)
        box(d, x, y, w, h, title, lines, "person" if i in (0, 4, 5) else "local")
        if col < 2:
            arrow(d, (x+w, y+h/2), (x+w+gap, y+h/2))
    arrow(d, (x0+2*(w+gap)+w/2, y0+h), (x0+w/2, y0+h+150), "")
    d.text((60, 1000), "No sign-in, no accounts, no OAuth. Users only see public video metadata and links to the original platform pages.", font=font(24), fill=MUTED)
    im.save(OUT/"user-flow.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    architecture()
    user_flow()
    print(OUT/"architecture.png", OUT/"user-flow.png")
