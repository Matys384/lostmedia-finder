import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.features import Encoder,extract,load
from lostmedia.matching import temporal_consensus,match
from lostmedia.media import views
from lostmedia.web import create_app


@pytest.fixture
def setup(tmp_path):
    settings=Settings(root=tmp_path,backend="phash",interval=1)
    return settings,Database(tmp_path)


def test_claim_is_atomic_and_expired_leases_recover(setup):
    s,db=setup
    for n in range(12):
        db.add("local",str(n),"",str(n))
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs=list(pool.map(lambda _:db.claim(60),range(12)))
    assert len({j['job_id'] for j in jobs})==12
    assert db.claim() is None
    db.execute("UPDATE jobs SET lease_until=0 WHERE id=?",(jobs[0]['job_id'],))
    recovered=db.claim()
    assert recovered['job_id']==jobs[0]['job_id']
    db.finish(jobs[0])  # stale owner must not finish another worker's lease
    assert db.rows("SELECT state FROM jobs WHERE id=?",(recovered['job_id'],))[0]['state']=='running'
    db.finish(recovered)


def test_temporal_rejects_single_frame_and_reverse():
    repeated=[dict(reference_time=0,candidate_time=float(i),similarity=.99) for i in range(20)]
    assert temporal_consensus(repeated) is None
    backwards=[dict(reference_time=float(i),candidate_time=20.-i,similarity=.99) for i in range(10)]
    assert temporal_consensus(backwards,tolerance=.1) is None


def test_temporal_speed_and_outliers():
    pairs=[dict(reference_time=float(i),candidate_time=73.+i*.8,similarity=.95) for i in range(10)]
    pairs += [dict(reference_time=float(i),candidate_time=140.+i*3,similarity=.8) for i in range(15)]
    result=temporal_consensus(pairs,tolerance=.15)
    assert abs(result['offset']-73)<.2
    assert abs(result['speed']-.8)<.05
    assert len(result['pairs'])==10


def test_cache_reuses_then_invalidates_changed_bytes(setup):
    from PIL import Image
    s,db=setup
    path=s.root/'reference.png'
    image=Image.fromarray(np.random.default_rng(2).integers(0,256,(180,240,3),dtype=np.uint8))
    image.save(path)
    vid=db.add('local','ref','','ref',local_path=str(path),reference=True)
    video=db.rows('SELECT * FROM videos WHERE id=?',(vid,))[0]
    encoder=Encoder(s)
    result=extract(video,path,encoder,db,s)
    before=result.stat().st_mtime_ns
    extract(video,path,encoder,db,s)
    assert result.stat().st_mtime_ns==before
    old=load(result)['sha256'].item()
    Image.new('RGB',(240,180),'blue').save(path)
    extract(video,path,encoder,db,s)
    assert load(result)['sha256'].item()!=old
    assert len(load(result)['vectors'])==7


def test_web_read_and_write_protection(setup):
    s,db=setup
    db.add('local','x','','<script>alert(1)</script>')
    client=TestClient(create_app(s))
    assert client.get('/').status_code==200
    assert client.get('/api/stats').json()['discovered']==1
    assert client.post('/api/retry').status_code==403
    token=client.get('/api/session').json()['token']
    assert client.post('/api/retry',headers={'X-Lostmedia-Token':token}).status_code==200
    assert client.get('/api/stats',headers={'Host':'evil.example'}).status_code==400
    assert client.get('/static/secrets').status_code==404
    assert client.get('/api/media/999').status_code==404


def test_persistent_shards_find_correct_video(setup):
    from lostmedia.indexing import build,search
    s,db=setup
    rng=np.random.default_rng(7)
    vec=rng.normal(size=(220,64)).astype('float32')
    vec/=np.linalg.norm(vec,axis=1,keepdims=True)
    vid=db.add('local','a','','a')
    path=s.root/'features'/'test.npz'
    np.savez(path,vectors=vec,times=np.arange(220),labels=np.array(['full']*220),thumbs=np.array(['x']*220))
    db.execute('INSERT INTO features VALUES(?,?,?,?)',(vid,'test',str(path),220))
    assert build(db,s,'test',100)=={'vectors':220,'shards':3}
    found=search(s,'test',vec[210:211],3)[0]
    assert found[0]['video_id']==vid and found[0]['time']==210
    assert found[0]['score']> .999


def test_import_catalog_period_and_dedup(setup):
    from lostmedia.catalogs import import_catalog
    s,db=setup
    path=s.root/'catalog.jsonl'
    path.write_text('\n'.join(json.dumps(x) for x in [
        {'source':'youtube','id':'abc','published':'2007-01-01'},
        {'source':'youtube','id':'abc','published':'2007-01-01'},
        {'source':'youtube','id':'def','published':'2012-12-31'},
        {'source':'youtube','id':'ghi','published':'2013-01-01'},
        {'source':'youtube','id':'unknown'}]),'utf-8')
    from datetime import date
    report=import_catalog(db,path,start=date(2007,1,1))
    assert report['new']==2
    assert report['outside_period']==1
    assert report['unknown_date']==1


def test_discovery_cursor_and_end_boundary(setup,monkeypatch):
    from lostmedia import discovery
    from datetime import date
    s,db=setup
    calls=[]
    def request(db,provider,url,params,daily_limit=80):
        calls.append(params)
        return {'items':[{'id':{'videoId':'abc'},'snippet':{'title':'hello','publishedAt':'2007-01-01T00:00:00Z'}}]}
    monkeypatch.setenv('YOUTUBE_API_KEY','test-key')
    monkeypatch.setattr(discovery,'request',request)
    report=discovery.discover(db,'youtube',['hello'],date(2007,1,1),date(2007,1,31),1)
    assert report['new']==1
    assert calls[0]['publishedBefore']=='2007-02-01T00:00:00Z'
    assert discovery.discover(db,'youtube',['hello'],date(2007,1,1),date(2007,1,31),1)['calls']==0


def test_playlist_uses_upload_date_not_playlist_date(setup,monkeypatch):
    from lostmedia import catalogs
    s,db=setup
    monkeypatch.setenv('YOUTUBE_API_KEY','test')
    monkeypatch.setattr(catalogs,'request',lambda *a,**k: {'items':[
        {'snippet':{'title':'old clip','publishedAt':'2026-01-01'},'contentDetails':{'videoId':'old','videoPublishedAt':'2008-01-01T00:00:00Z'}},
        {'snippet':{'title':'new clip','publishedAt':'2008-01-01'},'contentDetails':{'videoId':'new','videoPublishedAt':'2026-01-01T00:00:00Z'}}]})
    assert catalogs.playlist(db,'PLtest')['new']==1
    assert db.rows('SELECT external_id FROM videos')[0]['external_id']=='old'
