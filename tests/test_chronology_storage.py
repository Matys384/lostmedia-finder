from datetime import date

import pytest

from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.chronology import initialize,split,tick,progress
from lostmedia.storage import enforce,StorageFull,cleanup_video


def test_partial_august_and_no_expansion(tmp_path):
    db=Database(Settings(root=tmp_path).root)
    initialize(db)
    periods=db.rows('SELECT * FROM periods ORDER BY start')
    assert periods[0]['start']=='2007-08-15'
    assert periods[0]['end']=='2007-08-31'
    assert periods[-1]['end']=='2012-12-31'
    assert len(periods)==65
    with pytest.raises(ValueError):
        initialize(db,end=date(2013,1,1))


def test_split_is_disjoint_and_reaches_daily(tmp_path):
    db=Database(Settings(root=tmp_path).root)
    initialize(db,providers=('dailymotion',))
    node=db.rows('SELECT * FROM frontier ORDER BY start LIMIT 1')[0]
    split(db,node)
    weeks=db.rows("SELECT * FROM frontier WHERE period_id=? AND state='pending' ORDER BY start",(node['period_id'],))
    assert [(w['start'],w['end']) for w in weeks]==[('2007-08-15','2007-08-21'),('2007-08-22','2007-08-28'),('2007-08-29','2007-08-31')]
    split(db,weeks[0])
    day=db.rows("SELECT * FROM frontier WHERE start=end ORDER BY start LIMIT 1")[0]
    split(db,day)
    # Dailymotion days continue into hour windows (see test_saturated_dailymotion_day_splits_into_hours).
    assert db.rows('SELECT state FROM frontier WHERE id=?',(day['id'],))[0]['state']=='split'


def test_resume_period_waits_for_analysis(tmp_path,monkeypatch):
    import lostmedia.chronology as module
    s=Settings(root=tmp_path,backend='phash',campaign_through='2012-12-31')
    db=Database(s.root)
    monkeypatch.delenv('YOUTUBE_API_KEY',raising=False)
    calls=[]
    def fake(db,provider,q,start,end,cursor,budget):
        calls.append((provider,start))
        return [dict(source=provider,external_id='one',url='https://example.org',title='unrelated',published='2007-08-16')],None
    monkeypatch.setattr(module,'page',fake)
    tick(s,discovery_pages=3,analysis_limit=0)
    p=db.rows('SELECT * FROM periods ORDER BY start LIMIT 1')[0]
    assert p['state']=='active'
    assert len(calls)==2
    tick(s,discovery_pages=3,analysis_limit=0)
    assert len(calls)==2
    assert db.rows('SELECT COUNT(*) n FROM videos')[0]['n']==2
    db.execute("UPDATE jobs SET state='done'")
    tick(s,discovery_pages=3,analysis_limit=0)
    assert db.rows('SELECT state FROM periods WHERE id=?',(p['id'],))[0]['state']=='exhausted'
    tick(s,discovery_pages=3,analysis_limit=0)
    assert calls[-1][1]=='2007-09-01'


def test_cleanup_never_deletes_user_media_or_references(tmp_path):
    s=Settings(root=tmp_path)
    db=Database(s.root)
    user=tmp_path/'original.mp4';user.write_bytes(b'local')
    vid=db.add('local','x','','user',local_path=str(user))
    cleanup_video(s,db,vid)
    assert user.exists()
    owned=s.root/'media'/'download.mp4';owned.write_bytes(b'download')
    remote=db.add('youtube','y','','download',local_path=str(owned))
    cleanup_video(s,db,remote)
    assert not owned.exists()
    assert db.rows('SELECT local_path FROM videos WHERE id=?',(remote,))[0]['local_path'] is None


def test_disk_budget_stops_without_deleting(tmp_path):
    s=Settings(root=tmp_path,disk_budget_gb=.000001,min_free_gb=0)
    file=tmp_path/'important';file.write_bytes(b'x'*2000)
    with pytest.raises(StorageFull):
        enforce(s)
    assert file.exists()



def test_saturated_dailymotion_day_splits_into_hours(tmp_path):
    from lostmedia.discovery import span, moment
    db=Database(Settings(root=tmp_path).root)
    initialize(db,providers=('dailymotion',))
    pid=db.rows("SELECT id FROM periods ORDER BY start LIMIT 1")[0]['id']
    db.execute("INSERT INTO frontier(period_id,provider,start,end,query) VALUES(?,?,?,?,?)",(pid,'dailymotion','2007-08-16','2007-08-16',''))
    day=db.rows("SELECT * FROM frontier WHERE start='2007-08-16' AND end='2007-08-16'")[0]
    split(db,day)
    blocks=db.rows("SELECT start,end FROM frontier WHERE start LIKE '2007-08-16T%' ORDER BY start")
    assert [(b['start'],b['end']) for b in blocks][:2]==[('2007-08-16T00','2007-08-16T03'),('2007-08-16T04','2007-08-16T07')] and len(blocks)==6
    split(db,db.rows("SELECT * FROM frontier WHERE start='2007-08-16T04'")[0])
    hours=db.rows("SELECT start,end FROM frontier WHERE start LIKE '2007-08-16T%' AND start=end ORDER BY start")
    assert [h['start'] for h in hours]==['2007-08-16T04','2007-08-16T05','2007-08-16T06','2007-08-16T07']
    split(db,db.rows("SELECT * FROM frontier WHERE start='2007-08-16T05' AND end='2007-08-16T05'")[0])
    assert db.rows("SELECT state FROM frontier WHERE start='2007-08-16T05' AND end='2007-08-16T05'")[0]['state']=='saturated'
    a,b=span(moment('2007-08-16T04'),moment('2007-08-16T07'))
    assert (b-a).total_seconds()==4*3600 and a.hour==4
    a,b=span(moment('2007-08-16'),moment('2007-08-16'))
    assert (b-a).days==1
