from datetime import date

from lostmedia import targeted
from lostmedia.config import Settings
from lostmedia.db import Database


def test_runtime_tiers_and_iso_durations():
    assert targeted.tier(312) == targeted.tier(307) == targeted.tier(318) == "exact"
    assert targeted.tier(306) == "near" and targeted.tier(250) == "near"
    assert targeted.tier(200) is None and targeted.tier(None) is None
    assert targeted.iso_seconds("PT5M12S") == 312 and targeted.iso_seconds("PT1H2S") == 3602 and targeted.iso_seconds("bad") is None


def test_dailymotion_window_splits_before_api_ceiling_and_queues_priority(tmp_path, monkeypatch):
    db = Database(Settings(root=tmp_path).root)
    targeted.initialize(db, date(2007, 8, 15), date(2007, 9, 30))
    calls = []

    def fake(db, provider, url, params, *a):
        calls.append(params)
        if params["created_before"]-params["created_after"] > 8*86400:
            return {"total": 5000, "list": [], "has_more": True}
        return {"total": 1, "has_more": False, "list": [{"id": "x1", "title": "t", "url": "https://dm/x1", "created_time": 1187200000, "duration": 313}]}

    monkeypatch.setattr(targeted, "request", fake)
    node = db.rows("SELECT * FROM target_frontier WHERE provider='dailymotion' ORDER BY start LIMIT 1")[0]
    assert targeted.dailymotion(db, node)[2] == "split"
    assert abs(calls[0]["longer_than"]*60-307) < 1e-6 and abs(calls[0]["shorter_than"]*60-318) < 1e-6
    week = db.rows("SELECT * FROM target_frontier WHERE provider='dailymotion' AND state='pending' ORDER BY start LIMIT 1")[0]
    assert (week["start"], week["end"]) == ("2007-08-15", "2007-08-21")
    assert targeted.dailymotion(db, week)[2] == "exhausted"
    job = db.rows("SELECT j.priority,v.duration FROM jobs j JOIN videos v ON v.id=j.video_id")[0]
    assert job == {"priority": 100, "duration": 313}


def test_queue_claims_target_runtime_first(tmp_path):
    db = Database(Settings(root=tmp_path).root)
    first = db.add("dailymotion", "a", "u", "a")
    target = db.add("dailymotion", "b", "u", "b")
    db.execute("UPDATE jobs SET priority=100 WHERE video_id=?", (target,))
    assert db.claim()["id"] == target and db.claim()["id"] == first
