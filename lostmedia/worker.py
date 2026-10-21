import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path

from .db import Database
from .discovery import Cooldown, Throttled, cooldown, cooling_until
from .features import Encoder, decode, encode_staged, extract, load, single_threaded
from .matching import match
from .media import acquire,restore_thumbnails
from .storage import cleanup_completed,cleanup_video,enforce,StorageFull
from .tuning import Tuner, classify

log = logging.getLogger(__name__)

INACCESSIBLE = ("private video","video unavailable","video is not available","has been removed","not available in your country","http error 404","http error 403")
# Pause applied when a stage reports a cooldown without a persisted one.
BASE_PAUSE = 60
# Lower concurrency where the host is slow or sensitive to parallel transfers.
SOURCE_SLOTS = {"archive": 3}

# Login, age or membership gates: recorded, never bypassed.
RESTRICTED = ("confirm your age","access-restricted","members-only","join this channel","requires payment","login required")
UNSUPPORTED = ("no video file",)
OVER_LIMIT = ("exceeds limit","size limit","duration limit","exceeds configured","within download size")


def prepare(job, settings, db, encoder):
    """Download thread: returns cached features, or the local media path to decode."""
    cached = db.rows("SELECT path FROM features WHERE video_id=? AND signature=?", (job["id"], encoder.signature))
    if cached and Path(cached[0]["path"]).is_file():
        return {"cached": cached[0]["path"]}, {"download": 0., "extract": 0., "decode": 0., "gpu": 0., "media_bytes": None}
    path = acquire(job, settings, db)
    # Measured from the paced start, so queueing behind the spacing is not counted as latency.
    began = job.get("_fetch_started", time.monotonic())
    return {"path": str(path)}, {"download": time.monotonic()-began, "extract": 0., "decode": 0., "gpu": 0., "media_bytes": path.stat().st_size}


def run(settings, limit=0, downloads=0, period_id=None,source=None,cached_only=False):
    db = Database(settings.root)
    cleanup_completed(settings,db)
    enforce(settings)
    refs = db.rows("SELECT * FROM videos WHERE is_reference=1")
    if not refs:
        raise ValueError("Import at least one reference image or clip first")
    encoder = Encoder(settings)
    # References are sampled much more densely than candidates: a candidate frame taken every
    # `interval` seconds then lands within reference_interval/2 of a reference frame instead of
    # up to interval/2 away. Candidate features (and their cache signature) are unchanged.
    from dataclasses import replace
    # Small corner windows only make sense on candidates; on the reference they would index HUD corners.
    ref_settings = replace(settings, interval=settings.reference_interval or settings.interval, crops=bool(settings.crops))
    ref_signature = f"{encoder.signature}-ref{ref_settings.interval:g}"
    references = [(r, load(extract(r, r["local_path"], encoder, db, ref_settings, ref_signature))) for r in refs]
    downloads = max(1, downloads or settings.downloads)
    # Pool threads overlap network, decoding and preprocessing; the encoder lock
    # keeps a single GPU forward at a time. Matching and commits stay on this thread.
    stop, active, active_lock = threading.Event(), {}, threading.Lock()

    def heartbeat():
        while not stop.wait(max(1,settings.lease_seconds/3)):
            with active_lock:
                jobs = list(active.values())
            for job in jobs:
                db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND state='running'",
                           (time.time()+settings.lease_seconds, job["job_id"],job["owner"]))

    # Round-robin over platforms spreads the per-platform request spacing.
    if source:
        sources = [source]
    else:
        sources = [r["source"] for r in db.rows("SELECT DISTINCT v.source FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.state IN ('queued','running') ORDER BY v.source")]
    tuner = Tuner(db, settings.adaptive_pacing)
    # A cached-only rematch makes no platform request, so platform pauses do not apply.
    cooling = {s: 0 if cached_only else cooling_until(db, "download:"+s) for s in sources}

    def pause(name, seconds, why):
        cooldown(db, "download:"+name, seconds)
        cooling[name] = time.time()+seconds
        log.warning("%s paused %.0fs: %s", name, seconds, why[:200])
    turn = 0
    busy = {}

    def claim():
        nonlocal turn
        while True:
            # Per-platform slot cap: slow hosts (Archive at ~1 MB/s) would otherwise
            # end up holding every slot, since fast jobs finish and free theirs.
            open_sources = [s for s in sources if cooling.get(s, 0) <= time.time()]
            cap = -(-downloads // max(1, len(open_sources)))
            live = [s for s in open_sources if busy.get(s, 0) < min(cap, SOURCE_SLOTS.get(s, cap), settings.max_slots_per_source)]
            if not live:
                return None
            name = live[turn % len(live)]
            turn += 1
            job = db.claim(settings.lease_seconds,period_id,name,encoder.signature if cached_only else None)
            if job:
                return job
            sources.remove(name)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    # Stage 1 (threads): network. Stage 2 (processes): decoding, crops, pHash -- CPU work
    # that serializes on the GIL in threads. Stage 3 (this thread): GPU, matching, commits.
    processes = max(1, settings.processes or min(12, (os.cpu_count() or 4)-2))
    began_run = time.monotonic()
    count, errors, started, halted = 0, 0, 0, None
    fetching, decoding = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=downloads) as pool, ProcessPoolExecutor(max_workers=processes, initializer=single_threaded) as cpu:
            while True:
                # Backpressure: downloaded files waiting for a CPU worker occupy disk.
                while (not halted and len(fetching) < downloads and len(decoding) < 2*processes
                       and not (limit and started >= limit)):
                    if (settings.root/"STOP").exists():
                        halted = "stop file"
                        break
                    job = claim()
                    if job is None:
                        break
                    with active_lock:
                        active[job["job_id"]] = job
                    busy[job["source"]] = busy.get(job["source"], 0)+1
                    fetching[pool.submit(prepare, job, settings, db, encoder)] = job
                    started += 1
                if not fetching and not decoding:
                    waiting = [until for s, until in cooling.items() if s in sources and until > time.time()]
                    if halted or (limit and started >= limit) or not waiting or not settings.resume_paused:
                        break
                    # Only paused platforms still have work: wait for the earliest pause to end.
                    log.info("All remaining platforms paused; resuming in %.0fs", min(waiting)-time.time())
                    while time.time() < min(waiting) and not (settings.root/"STOP").exists():
                        time.sleep(1)
                    if (settings.root/"STOP").exists():
                        halted = "stop file"
                    continue
                finished, _ = wait([*fetching, *decoding], return_when=FIRST_COMPLETED)
                for future in finished:
                    if future in fetching:
                        job = fetching.pop(future)
                        busy[job["source"]] -= 1
                        spacing = job.get("_spacing")
                        elapsed = time.monotonic()-job["_fetch_started"] if "_fetch_started" in job else 0.
                        try:
                            staged, timings = future.result()
                        except Throttled as exc:
                            wait_for = tuner.record(job["source"], spacing, elapsed, "throttled", detail=str(exc), wait=exc.retry_after)
                            if wait_for:
                                pause(job["source"], wait_for, str(exc))
                            outcome = exc
                        except Cooldown as exc:
                            outcome = exc
                        except BaseException as exc:
                            kind = classify(str(exc)) if spacing is not None else "content"
                            wait_for = tuner.record(job["source"], spacing, elapsed, kind, detail=str(exc)) if spacing is not None else None
                            if wait_for:
                                pause(job["source"], wait_for, f"repeated transient errors: {exc}")
                                outcome = Cooldown(f"{job['source']}: host unreachable or failing; job kept")
                            else:
                                outcome = exc
                        else:
                            if "path" in staged:
                                tuner.record(job["source"], spacing, timings["download"], "ok", timings["media_bytes"])
                                job["_timings"] = timings
                                decoding[cpu.submit(decode, job["id"], staged["path"], settings, encoder.signature)] = job
                                continue
                            outcome = (staged["cached"], timings)
                    else:
                        job = decoding.pop(future)
                        timings = job.pop("_timings")
                        try:
                            staged = future.result()
                            began = time.monotonic()
                            feature_path = encode_staged(job["id"], staged, encoder, db, settings)
                            timings["decode"] = staged.get("seconds", 0.)
                            timings["gpu"] = time.monotonic()-began
                            timings["extract"] = timings["decode"]+timings["gpu"]
                            outcome = (feature_path, timings)
                        except BaseException as exc:
                            outcome = exc
                    try:
                        result = handle(job, outcome, settings, db, encoder, references)
                        if result == "failed":
                            errors += 1
                        count += 1
                    except Cooldown as exc:
                        log.info("%s; job %s requeued", str(exc)[:160], job["id"])
                        db.requeue(job)
                        if cooling.get(job["source"], 0) <= time.time():
                            # Raised by a persisted pause (or a stage without one): honour it.
                            cooling[job["source"]] = cooling_until(db, "download:"+job["source"]) or time.time()+BASE_PAUSE
                    except StorageFull:
                        db.requeue(job)
                        halted = "storage"
                    finally:
                        with active_lock:
                            active.pop(job["job_id"],None)
    finally:
        stop.set()
        thread.join(timeout=2)
    if halted == "storage":
        raise StorageFull("Disk budget or free-space floor reached; processing paused, no evidence deleted")
    wall = time.monotonic()-began_run
    return {"processed":count,"failed":errors,"signature":encoder.signature,"wall_seconds":round(wall,3),
            "videos_per_minute":round(count*60/wall,2) if wall else 0,"downloads":downloads,"processes":processes,
            "cooling":sorted(s for s, until in cooling.items() if until > time.time()),"halted":halted,
            "pacing":tuner.report()}


def handle(job, outcome, settings, db, encoder, references):
    """outcome is (feature_path, timings) or the exception raised by an earlier stage."""
    try:
        if isinstance(outcome, BaseException):
            raise outcome
        feature_path, timings = outcome
        db.execute("INSERT OR IGNORE INTO processing_metrics(video_id,accessible) VALUES(?,1)",(job["id"],))
        began = time.monotonic()
        candidate = load(feature_path)
        # Replace only after successful feature extraction. No stale hits on rerun.
        db.execute("DELETE FROM matches WHERE video_id=? AND signature=?", (job["id"],encoder.signature))
        keep=[]
        for ref, features in references:
            result = match(candidate,features,settings.root,settings.backend,settings.interval)
            if result and result["temporal_frames"]>=3 and any(not (settings.root/p["candidate_thumb"]).exists() for p in result["pairs"]):
                try:
                    downloaded=acquire(job,settings,db)
                    restore_thumbnails(downloaded,job,encoder,settings,[p["candidate_thumb"] for p in result["pairs"]])
                    result=match(candidate,features,settings.root,settings.backend,settings.interval)
                except Exception as exc:
                    result["verification_error"]=str(exc)[:500]
            if result:
                db.save_match(job["id"],ref["id"],encoder.signature,result)
                keep.extend(p["candidate_thumb"] for p in result["pairs"])
        matching = time.monotonic()-began
        db.finish(job)
        cleanup_video(settings,db,job["id"],keep)
        db.execute("UPDATE processing_metrics SET accessible=1,seconds=?,frames=?,feature_bytes=?,completed=?,download_seconds=?,extract_seconds=?,match_seconds=?,media_bytes=COALESCE(?,media_bytes),decode_seconds=?,gpu_seconds=? WHERE video_id=?",
                   (timings["download"]+timings["extract"]+matching,len(candidate["times"]),Path(feature_path).stat().st_size,time.time(),
                    timings["download"],timings["extract"],matching,timings["media_bytes"],timings.get("decode"),timings.get("gpu"),job["id"]))
        log.info("Analyzed %s (%s): %s",job["id"],job["source"],job["title"])
        return "done"
    except (Cooldown, StorageFull):
        raise
    except Exception as exc:
        message=str(exc).lower()
        log.warning("Failed video %s: %s",job["id"],str(exc)[:300])
        db.finish(job,f"{type(exc).__name__}: {exc}"[:2000])
        for state, terms in (("restricted",RESTRICTED),("ignored",UNSUPPORTED)):
            if any(term in message for term in terms):
                db.execute("UPDATE jobs SET state=? WHERE id=? AND owner=?",(state,job["job_id"],job["owner"]))
                cleanup_video(settings,db,job["id"])
                return state
        if any(term in message for term in INACCESSIBLE):
            db.execute("UPDATE jobs SET state='inaccessible' WHERE id=? AND owner=?",(job["job_id"],job["owner"]))
            cleanup_video(settings,db,job["id"])
            return "inaccessible"
        if any(term in message for term in OVER_LIMIT):
            # Deferred, not rejected: `retry --deferred` with larger limits picks these up.
            db.execute("UPDATE jobs SET state='deferred',error=? WHERE id=? AND owner=?",
                       (f"{exc} (limits {settings.max_download_mb} MB / {settings.max_duration} s)"[:2000],job["job_id"],job["owner"]))
            cleanup_video(settings,db,job["id"])
            return "deferred"
        return "failed"
