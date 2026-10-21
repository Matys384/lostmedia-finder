"""Harder planted-excerpt benchmark: more transforms, BSS hidden in SM64 footage, SM64 hard negatives.

Uses the user-supplied BSS upload (reference = seconds 30-42) and public SM64 videos
under data/validation/negatives (downloaded separately). Runs at the production frame
interval by default (2 s) -- the original validate.py uses 1 s, which is easier.

Run: python scripts/validate_hard.py [--interval 2]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lostmedia.config import Settings  # noqa: E402
from lostmedia.db import Database  # noqa: E402
from lostmedia.media import digest  # noqa: E402
from lostmedia.worker import run  # noqa: E402

FONT = "C\\:/Windows/Fonts/arial.ttf"
OFFSET = 73.0


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True, timeout=600)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


# name -> (ffmpeg filter on the 12 s reference, inserted duration in seconds, note)
POSITIVES = {
    "crop20_crf36": ("crop=iw*0.8:ih*0.8,scale=320:240", 12, "20% crop + heavy compression"),
    "mirror": ("hflip,scale=320:240", 12, "horizontal flip"),
    "pip65": ("crop=iw*.85:ih*.85,scale=208:156,pad=320:240:12:60:color=0x303040", 12, "reaction layout, 65% picture"),
    "pip35_corner": ("scale=112:84,pad=320:240:200:148:color=0x202020", 12, "small 35% picture in corner"),
    "pip35_mirror": ("hflip,scale=112:84,pad=320:240:8:8:color=0x202020", 12, "mirror + small 35% picture"),
    "pip25_tiny": ("scale=80:60,pad=320:240:230:170:color=0x202020", 12, "tiny 25% picture"),
    "lowres_crf45": ("scale=176:132,scale=320:240", 12, "176x132 upscaled + crf 45 (applied below)"),
    "letterbox_169": ("scale=-2:240,pad=428:240:(ow-iw)/2:0,scale=320:180,pad=320:240:0:30", 12, "4:3 in 16:9 frame, letterboxed"),
    "color_noise": ("eq=brightness=0.08:saturation=1.5:gamma=1.2,noise=alls=18:allf=t,scale=320:240", 12, "colour shift + film noise"),
    "watermark_text": (f"scale=320:240,drawbox=x=0:y=200:w=320:h=40:color=black@0.7:t=fill,drawtext=fontfile='{FONT}':text='SUBSCRIBE LOL':fontcolor=white:fontsize=22:x=40:y=208", 12, "caption bar over bottom 17%"),
    "speed075": ("setpts=PTS/0.75,scale=320:240", 16, "slowed to 0.75x"),
    "speed150": ("setpts=PTS/1.5,scale=320:240", 8, "sped up to 1.5x"),
    "fps12": ("fps=12,scale=320:240", 12, "12 fps re-encode"),
}
EXCERPTS = {
    "excerpt5s": ("trim=0:5,setpts=PTS-STARTPTS,scale=320:240", 5, "only the first 5 s of the reference"),
    "cut_middle": ("select='not(between(t,4,8))',setpts=N/24/TB,scale=320:240", 8, "4 s removed from the middle"),
}


def build(root, sm64):
    hard = root / "hard"
    hard.mkdir(parents=True, exist_ok=True)
    ref, bed, source = root / "reference.mp4", root / "bed.mp4", root / "source.mp4"
    if not (ref.exists() and bed.exists()):
        raise SystemExit("Run scripts/validate.py once first: it builds reference.mp4 and bed.mp4")
    manifest = []

    def plant(name, filters, length, note, background, crf=34, clip_crf=36):
        clip, candidate = hard / f"{name}-clip.mp4", hard / f"{name}.mp4"
        if not candidate.exists():
            ffmpeg("-i", ref, "-an", "-vf", filters+",fps=24,setsar=1", "-c:v", "libx264", "-crf", clip_crf, clip)
            graph = (f"[0:v]scale=320:240:force_original_aspect_ratio=decrease,pad=320:240:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,split=2[x][y];"
                     f"[x]trim=0:{OFFSET},setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];"
                     f"[y]trim=start={OFFSET},setpts=PTS-STARTPTS,trim=0:{max(30., 107-length)}[c];[a][b][c]concat=n=3:v=1:a=0[out]")
            ffmpeg("-stream_loop", -1, "-i", background, "-i", clip, "-filter_complex", graph, "-map", "[out]", "-an",
                   "-c:v", "libx264", "-preset", "fast", "-crf", crf, candidate)
        manifest.append({"path": str(candidate.resolve()), "name": name, "expected": True, "offset": OFFSET,
                         "duration": length, "note": note, "background": background.name})

    for name, (filters, length, note) in {**POSITIVES, **EXCERPTS}.items():
        plant(name, filters, length, note, bed, crf=45 if name == "lowres_crf45" else 34, clip_crf=45 if name == "lowres_crf45" else 36)
    # BSS hidden inside other SM64 gameplay: same palette, same HUD, much harder than cartoon background.
    for i, background in enumerate(sm64[:3]):
        plant(f"in_sm64_{i}", "crop=iw*0.9:ih*0.9,scale=320:240", 12, f"BSS inside {background.name}", background)
    plant("in_sm64_pip_mirror", "hflip,scale=144:108,pad=320:240:168:124:color=0x101010", 12, "mirror 45% PiP over SM64", sm64[3] if len(sm64) > 3 else bed)
    for background in sm64:
        manifest.append({"path": str(background.resolve()), "name": "neg_" + background.stem, "expected": False,
                         "note": "public SM64 video (not BSS): hard negative"})
    manifest.append({"path": str(bed.resolve()), "name": "neg_bunny", "expected": False, "note": "cartoon background only"})
    # Adjacent BSS footage that is not in the reference (source 0-28 s and 42-47.7 s). Informational only:
    # a hit here is the same lost video, so it is reported but not counted as a false positive.
    outside = hard / "bss_outside_reference.mp4"
    if not outside.exists():
        ffmpeg("-i", source, "-an", "-vf", "select='not(between(t,28,44))',setpts=N/30/TB,scale=320:240,fps=24,setsar=1",
               "-c:v", "libx264", "-crf", 30, outside)
    manifest.append({"path": str(outside.resolve()), "name": "bss_outside_reference", "expected": None,
                     "note": "BSS frames outside the 30-42 s reference window (informational)"})
    return ref, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="dinov2", choices=["phash", "dinov2", "clip"])
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--crops", default="true", choices=["true", "fine"])
    parser.add_argument("--reference-interval", type=float, default=0.25)
    args = parser.parse_args()
    root = Path("data/validation").resolve()
    sm64 = sorted((root / "negatives").glob("*.mp4"))
    if len(sm64) < 4:
        raise SystemExit("Put at least 4 public SM64 videos (not BSS) in data/validation/negatives")
    reference, manifest = build(root, sm64)
    crops = True if args.crops == "true" else "fine"
    settings = Settings(root=root / f"hard-{args.backend}-{args.interval:g}s-{args.crops}-{time.time_ns()}", backend=args.backend,
                        interval=args.interval, batch_size=128, downloads=4, processes=6, crops=crops,
                        reference_interval=args.reference_interval)
    db = Database(settings.root)
    db.add("local", digest(reference), "https://www.youtube.com/watch?v=NndwzzpMunQ&t=30",
           "BSS user-supplied reference, 30-42 seconds", local_path=str(reference), reference=True)
    for item in manifest:
        item["id"] = db.add("local", digest(item["path"]), "", item["name"], local_path=item["path"])
    began = time.perf_counter()
    summary = run(settings)
    seconds = time.perf_counter()-began
    results = []
    for item in manifest:
        rows = db.rows("SELECT payload FROM matches WHERE video_id=? AND signature=?", (item["id"], summary["signature"]))
        best = json.loads(rows[0]["payload"]) if rows else None
        detected = bool(best and best["status"] == "sequence_verified")
        error = abs(best["offset"]-item["offset"]) if detected and best.get("offset") is not None and item["expected"] else None
        if item["expected"] is True:
            success = detected and error is not None and error <= 2
        elif item["expected"] is False:
            success = not detected
        else:
            success = None
        results.append({**item, "detected": detected, "offset_error_seconds": error, "success": success,
                        "score": best["score"] if best else None, "status": best["status"] if best else None,
                        "temporal_frames": best["temporal_frames"] if best else 0})
    positives = [r for r in results if r["expected"] is True]
    negatives = [r for r in results if r["expected"] is False]
    report = {"backend": args.backend, "interval": args.interval, "crops": args.crops, "reference_interval": args.reference_interval, "seconds": round(seconds, 1), "summary": summary,
              "data_root": str(settings.root), "positives": len(positives), "negatives": len(negatives),
              "recall_at_2_seconds": sum(r["success"] for r in positives)/len(positives),
              "false_positives": sum(r["detected"] for r in negatives),
              "scope": "Controlled planted excerpts and 7 public SM64 hard negatives; not a web-scale precision estimate.",
              "results": results}
    out = Path("reports") / f"validation-hard-{args.backend}-{args.interval:g}s-ref{args.reference_interval:g}-{args.crops}.json"
    # Reports are committed: keep paths relative to the project, never the local home directory.
    text = json.dumps(report, indent=2).replace(json.dumps(str(Path.cwd()))[1:-1], ".")
    out.write_text(text, "utf-8")
    for r in results:
        verdict = {True: "PASS", False: "FAIL", None: "info"}[r["success"]]
        print(f"{verdict:4} {r['name']:24} detected={r['detected']!s:5} err={r['offset_error_seconds']} frames={r['temporal_frames']} score={r['score'] and round(r['score'], 3)}  {r.get('note', '')}")
    print(json.dumps({k: v for k, v in report.items() if k not in ("results", "summary")}, indent=2))


if __name__ == "__main__":
    main()
