import time
from datetime import date

import cv2
import numpy as np
import pytest

from lostmedia import worker
from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.discovery import Cooldown, START, reserve
from lostmedia.storage import cleanup_video


def write_video(path, images, fps=2):
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (160, 120))
    for image in images:
        for _ in range(fps):
            out.write(image)
    out.release()


@pytest.fixture
def corpus(tmp_path):
    rng = np.random.default_rng(7)
    noise = lambda: cv2.resize(rng.integers(0, 256, (12, 16, 3), dtype=np.uint8), (160, 120), interpolation=cv2.INTER_NEAREST)
    clip = [noise() for _ in range(8)]
    s = Settings(root=tmp_path / "data", backend="phash", interval=1, downloads=3)
    db = Database(s.root)
    write_video(tmp_path / "ref.mp4", clip)
    db.add("local", "ref", "", "ref", local_path=str(tmp_path / "ref.mp4"), reference=True)
    for n in range(4):
        planted = n == 2
        write_video(tmp_path / f"c{n}.mp4", [noise() for _ in range(5)] + (clip if planted else []) + [noise() for _ in range(5)])
        db.add("local", f"c{n}", "", f"c{n}", local_path=str(tmp_path / f"c{n}.mp4"))
    return s, db


def test_parallel_pipeline_finishes_all_and_locates_insert(corpus):
    s, db = corpus
    report = worker.run(s)
    assert report["processed"] == 4 and report["failed"] == 0 and report["downloads"] == 3
    assert {r["state"] for r in db.rows("SELECT state FROM jobs")} == {"done"}
    best = db.rows("SELECT v.external_id, m.payload FROM matches m JOIN videos v ON v.id=m.video_id ORDER BY m.score DESC LIMIT 1")[0]
    assert best["external_id"] == "c2"
    import json
    assert abs(json.loads(best["payload"])["offset"] - 5) <= 1
    stages = db.rows("SELECT download_seconds, extract_seconds, match_seconds FROM processing_metrics")
    assert len(stages) == 4 and all(r["extract_seconds"] is not None for r in stages)


def test_cooldown_requeues_without_burning_attempts(corpus, monkeypatch):
    s, db = corpus
    db.execute("UPDATE videos SET source='youtube' WHERE external_id='c0'")
    original = worker.prepare

    def throttled(job, *args):
        if job["source"] == "youtube":
            raise Cooldown("youtube: throttled")
        return original(job, *args)

    monkeypatch.setattr(worker, "prepare", throttled)
    report = worker.run(s)
    assert report["cooling"] == ["youtube"] and report["processed"] == 3
    job = db.rows("SELECT j.state, j.attempts FROM jobs j JOIN videos v ON v.id=j.video_id WHERE v.external_id='c0'")[0]
    assert job == {"state": "queued", "attempts": 0}


def test_stop_file_halts_before_claiming(corpus):
    s, db = corpus
    (s.root / "STOP").touch()
    assert worker.run(s)["halted"] == "stop file"
    assert {r["state"] for r in db.rows("SELECT state FROM jobs")} == {"queued"}


def test_long_cooldown_raises_instead_of_sleeping(tmp_path):
    from lostmedia.discovery import cooldown
    db = Database(Settings(root=tmp_path).root)
    cooldown(db, "download:youtube", 3600)
    began = time.monotonic()
    with pytest.raises(Cooldown):
        reserve(db, "download:youtube", 3.0)
    assert time.monotonic() - began < 1


def test_terminal_cleanup_drops_partial_downloads(tmp_path):
    s = Settings(root=tmp_path)
    db = Database(s.root)
    vid = db.add("dailymotion", "x1", "https://www.dailymotion.com/video/x1", "x1")
    folder = s.root / "media" / str(vid)
    folder.mkdir()
    (folder / "video.mp4.part").write_bytes(b"0" * 10)
    cleanup_video(s, db, vid)
    assert not folder.exists()


def test_direct_api_defaults_start_on_release_date():
    import inspect
    from lostmedia import catalogs, discovery
    assert START == date(2007, 8, 15)
    for function in (discovery.discover, catalogs.import_catalog, catalogs.playlist):
        assert inspect.signature(function).parameters["start"].default == START


def test_archive_prefers_small_derivative_and_separates_unsupported(tmp_path, monkeypatch):
    from lostmedia import discovery
    db = Database(Settings(root=tmp_path).root)
    item = {"external_id": "old-item"}
    files = [{"name": "orig.mpeg", "size": "900000000", "source": "original"},
             {"name": "orig.ia.mp4", "size": "40000000", "source": "derivative"},
             {"name": "orig_512kb.mp4", "size": "20000000", "source": "derivative"}]
    monkeypatch.setattr(discovery, "request", lambda *a, **k: {"files": files})
    assert discovery.archive_file(db, item, 300).endswith("/orig_512kb.mp4")
    files[:] = [{"name": "talk.wmv", "size": "5000000"}]
    assert discovery.archive_file(db, item, 300).endswith("/talk.wmv")
    files[:] = [{"name": "notes.pdf", "size": "5000"}]
    with pytest.raises(ValueError, match="no video file"):
        discovery.archive_file(db, item, 300)
    files[:] = [{"name": "dark.mp4", "size": "5000", "private": "true"}]
    with pytest.raises(ValueError, match="access-restricted"):
        discovery.archive_file(db, item, 300)
    files[:] = [{"name": "huge.avi", "size": str(2 * 1024**3)}]
    with pytest.raises(ValueError, match="within download size"):
        discovery.archive_file(db, item, 300)


def test_age_gate_is_restricted_not_retried(corpus, monkeypatch):
    s, db = corpus
    monkeypatch.setattr(worker, "prepare", lambda *a: (_ for _ in ()).throw(RuntimeError("ERROR: Sign in to confirm your age")))
    worker.run(s, limit=1)
    assert db.rows("SELECT state FROM jobs WHERE state!='queued'")[0]["state"] == "restricted"


def test_archive_duration_checked_before_download(tmp_path, monkeypatch):
    from lostmedia import discovery
    db = Database(Settings(root=tmp_path).root)
    files = [{"name": "long.mp4", "size": "5000", "length": "1:10:00"}, {"name": "short.mp4", "size": "9000", "length": "95.5"}]
    monkeypatch.setattr(discovery, "request", lambda *a, **k: {"files": files})
    assert discovery.archive_file(db, {"external_id": "x"}, 300, 1800).endswith("/short.mp4")
    files.pop()
    with pytest.raises(ValueError, match="duration limit"):
        discovery.archive_file(db, {"external_id": "x"}, 300, 1800)


def test_slow_platform_cannot_hold_every_slot(corpus, monkeypatch):
    s, db = corpus
    db.execute("UPDATE videos SET source='archive' WHERE external_id IN ('c0','c1','c3')")
    s.downloads = 2
    seen, original = [], worker.prepare

    def tracked(job, *args):
        seen.append(job["source"])
        return original(job, *args)

    monkeypatch.setattr(worker, "prepare", tracked)
    worker.run(s)
    assert seen[:2] != ["archive", "archive"]


def test_repeated_transient_errors_pause_platform_and_keep_jobs(tmp_path, monkeypatch):
    rng = np.random.default_rng(3)
    s = Settings(root=tmp_path / "data", backend="phash", interval=1, downloads=1)
    db = Database(s.root)
    write_video(tmp_path / "ref.mp4", [rng.integers(0, 256, (120, 160, 3), dtype=np.uint8) for _ in range(4)])
    db.add("local", "ref", "", "ref", local_path=str(tmp_path / "ref.mp4"), reference=True)
    for n in range(5):
        db.add("archive", f"a{n}", f"https://archive.org/details/a{n}", f"a{n}")

    def offline(job, *args):
        job["_spacing"], job["_fetch_started"] = 1.5, time.monotonic()
        raise RuntimeError("ConnectTimeout: [WinError 10060] no answer")

    monkeypatch.setattr(worker, "prepare", offline)
    report = worker.run(s)
    states = [r["state"] for r in db.rows("SELECT state FROM jobs ORDER BY id")]
    # Two isolated timeouts are ordinary failures; the third in a row pauses the host.
    assert states[:2] == ["failed", "failed"] and set(states[2:]) == {"queued"}
    assert report["cooling"] == ["archive"]
    assert db.rows("SELECT next_at FROM rate_limits WHERE name='cooldown:download:archive'")[0]["next_at"] > time.time()+30


def test_tuner_climbs_only_after_full_healthy_window_and_backs_off_on_throttle(tmp_path, monkeypatch):
    from lostmedia import tuning
    db = Database(Settings(root=tmp_path).root)
    tuner = tuning.Tuner(db)
    assert tuner.spacing("dailymotion") == 2.0
    monkeypatch.setattr(tuning, "WINDOW_SECONDS", 0)
    monkeypatch.setattr(tuning, "WINDOW_ATTEMPTS", 3)
    for _ in range(3):
        tuner.record("dailymotion", 2.0, 5.0, "ok", 1000)
    assert tuner.spacing("dailymotion") == 1.5
    for _ in range(3):
        tuner.record("dailymotion", 1.5, 5.0, "ok", 1000)
    assert tuner.spacing("dailymotion") == 1.0
    pause = tuner.record("dailymotion", 1.0, 1.0, "throttled", detail="HTTP Error 429", wait=120)
    assert pause == 120 and tuner.spacing("dailymotion") == 1.5
    state = tuner.report()["dailymotion"]
    assert state["fastest_allowed"] == 1.5 and state["throttles"] == 1
    # Never climbs back above the refused pace.
    for _ in range(6):
        tuner.record("dailymotion", 1.5, 5.0, "ok", 1000)
    assert tuner.spacing("dailymotion") == 1.5
    assert tuner.record("youtube", 3.0, 1.0, "throttled", detail="Sign in to confirm you’re not a bot") == tuning.BOT_CHECK_BACKOFF
    assert tuner.spacing("youtube") == 4.0


def test_tuner_reverts_when_faster_pace_degrades(tmp_path, monkeypatch):
    from lostmedia import tuning
    db = Database(Settings(root=tmp_path).root)
    tuner = tuning.Tuner(db)
    monkeypatch.setattr(tuning, "WINDOW_SECONDS", 0)
    monkeypatch.setattr(tuning, "WINDOW_ATTEMPTS", 4)
    for _ in range(4):
        tuner.record("dailymotion", 2.0, 5.0, "ok")
    assert tuner.spacing("dailymotion") == 1.5
    for outcome in ("ok", "transient", "transient", "ok"):
        tuner.record("dailymotion", 1.5, 5.0, outcome)
    assert tuner.spacing("dailymotion") == 2.0 and tuner.report()["dailymotion"]["fastest_allowed"] == 2.0


def test_classify_and_retry_after():
    from lostmedia import tuning
    assert tuning.classify(None) == "ok"
    assert tuning.classify("ERROR: HTTP Error 429: Too Many Requests") == "throttled"
    assert tuning.classify("[WinError 10060] connexion") == "transient"
    assert tuning.classify("This video is not available") == "content"

    class Response:
        headers = {"Retry-After": "90"}

    class HTTPError(Exception):
        response = Response()

    wrapper = RuntimeError("wrapped")
    wrapper.exc_info = (HTTPError, HTTPError(), None)
    assert tuning.retry_after(wrapper) == 90
