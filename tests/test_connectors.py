from datetime import date

from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia import discovery


def test_archive_advancedsearch_cursor_progresses(tmp_path,monkeypatch):
    db=Database(Settings(root=tmp_path).root)
    calls=[]
    def request(db,provider,url,params,*args):
        calls.append(params)
        n=params['page']
        return {'response':{'numFound':201,'docs':[{'identifier':str(n),'title':'clip','publicdate':'2007-08-20T00:00:00Z'}]}}
    monkeypatch.setattr(discovery,'request',request)
    args=(db,'archive','',date(2007,8,15),date(2007,8,31))
    a,cursor=discovery.page(*args,None,80)
    b,cursor=discovery.page(*args,cursor,80)
    c,cursor=discovery.page(*args,cursor,80)
    assert [a[0]['external_id'],b[0]['external_id'],c[0]['external_id']]==['1','2','3']
    assert cursor is None
    assert 'publicdate:' in calls[0]['q']


def test_dailymotion_broad_search_omits_empty_search(tmp_path,monkeypatch):
    db=Database(Settings(root=tmp_path).root)
    calls=[]
    monkeypatch.setattr(discovery,'request',lambda db,provider,url,p: calls.append(p) or {'list':[],'has_more':False,'total':0})
    discovery.page(db,'dailymotion','',date(2007,8,15),date(2007,8,31),None,80)
    assert 'search' not in calls[0]
    assert calls[0]['created_after']==1187136000
    assert calls[0]['created_before']==1188604799


def test_key_not_exposed_in_forbidden_error(tmp_path,monkeypatch):
    import httpx
    import pytest
    db=Database(Settings(root=tmp_path).root)
    monkeypatch.setattr(discovery,'reserve',lambda *a:None)
    monkeypatch.setattr(discovery.httpx,'get',lambda *a,**k:httpx.Response(403,json={'error':{'errors':[{'reason':'quotaExceeded'}]}}))
    with pytest.raises(RuntimeError) as exc:
        discovery.request(db,'youtube','https://googleapis.com',{'key':'secret-not-for-logs'})
    assert 'quotaExceeded' in str(exc.value)
    assert 'secret-not-for-logs' not in str(exc.value)
