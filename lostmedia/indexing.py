"""Immutable disk-backed FAISS shards. Atomic manifest publication.

Only one shard resides in RAM during cross-corpus search. Existing feature
artifacts remain the source of truth; rebuilding does not re-encode videos.
"""
import heapq
import json
import os
import uuid

import faiss
import numpy as np
from filelock import FileLock

from .features import load


def build(db,settings,signature,shard_size=100000):
    if shard_size<100:
        raise ValueError("shard_size must be >=100")
    root=settings.root/"indexes"/signature
    root.mkdir(exist_ok=True)
    with FileLock(str(root/"build.lock")):
        generation=uuid.uuid4().hex[:12]
        manifest={"signature":signature,"generation":generation,"shards":[]}
        vectors,metadata=[],[]
        count=0

        def flush():
            nonlocal vectors,metadata,count
            if not vectors:
                return
            data=np.ascontiguousarray(np.stack(vectors),dtype=np.float32)
            # HNSW supports incremental ANN without a training corpus.
            index=faiss.IndexHNSWFlat(data.shape[1],32,faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efConstruction=100
            index.hnsw.efSearch=64
            index.add(data)
            stem=f"{generation}-{len(manifest['shards']):05d}"
            faiss.write_index(index,str(root/(stem+".faiss")))
            (root/(stem+".json")).write_text(json.dumps(metadata),"utf-8")
            manifest["shards"].append({"stem":stem,"count":len(data)})
            count+=len(data)
            vectors,metadata=[],[]

        last_id=0
        while rows:=db.rows("SELECT video_id,path FROM features WHERE signature=? AND video_id>? ORDER BY video_id LIMIT 100",(signature,last_id)):
            for row in rows:
                f=load(row["path"])
                for i,vec in enumerate(f["vectors"]):
                    vectors.append(vec)
                    metadata.append({"video_id":row["video_id"],"time":float(f["times"][i]),"crop":str(f["labels"][i]),"thumb":str(f["thumbs"][i])})
                    if len(vectors)>=shard_size:
                        flush()
            last_id=rows[-1]["video_id"]
        flush()
        temporary=root/"manifest.tmp"
        temporary.write_text(json.dumps(manifest),"utf-8")
        os.replace(temporary,root/"manifest.json")
        return {"vectors":count,"shards":len(manifest["shards"])}


def search(settings,signature,vectors,k=10):
    root=settings.root/"indexes"/signature
    manifest=json.loads((root/"manifest.json").read_text("utf-8"))
    heaps=[[] for _ in vectors]
    serial=0
    for shard in manifest["shards"]:
        stem=shard["stem"]
        index=faiss.read_index(str(root/(stem+".faiss")))
        metadata=json.loads((root/(stem+".json")).read_text("utf-8"))
        distances,ids=index.search(np.ascontiguousarray(vectors,dtype=np.float32),min(k,index.ntotal))
        for qi,(scores,neighbors) in enumerate(zip(distances,ids)):
            for score,idx in zip(scores,neighbors):
                if idx<0:
                    continue
                serial+=1
                item=(float(score),serial,metadata[int(idx)])
                if len(heaps[qi])<k:
                    heapq.heappush(heaps[qi],item)
                elif score>heaps[qi][0][0]:
                    heapq.heapreplace(heaps[qi],item)
        del index,metadata
    return [[{"score":s,**m} for s,_,m in sorted(h,reverse=True)] for h in heaps]
