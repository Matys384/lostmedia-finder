"""Compare actual BSS feature retrieval before enabling aggressive compression."""
import json
import sys
import time
from pathlib import Path

import faiss
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lostmedia.features import load
from lostmedia.matching import match


def main():
    faiss.omp_set_num_threads(4)
    # Keep the initial full-float extraction for a true FP32/FP16 comparison.
    root=Path('data/validation/run-dinov2')
    files=sorted((root/'features').glob('*/*.npz'),key=lambda p:int(p.parent.name))
    reference=load(files[0])['vectors'].astype('float32')
    real_files=list(Path('data/chronological/features').glob('*/*.npz'))
    real=[load(p)['vectors'].astype('float32') for p in real_files if int(p.parent.name)!=1]
    corpus=np.concatenate([load(p)['vectors'].astype('float32') for p in files[1:]]+real)
    d=corpus.shape[1]
    exact=faiss.IndexFlatIP(d);exact.add(corpus)
    scores,truth=exact.search(reference,10)
    planted=[load(p) for p in files[1:]]
    report=[]
    for method in ['float32','float16','int8','pq96x8']:
        begin=time.perf_counter()
        if method=='float32':
            index=exact;size=corpus.nbytes
        elif method=='float16':
            compact=corpus.astype('float16');index=faiss.IndexFlatIP(d);index.add(compact.astype('float32'));size=compact.nbytes
        elif method=='int8':
            scale=np.max(np.abs(corpus),axis=1,keepdims=True)/127
            compact=np.round(corpus/np.maximum(scale,1e-8)).astype('int8')
            index=faiss.IndexFlatIP(d);index.add(compact.astype('float32')*scale);size=compact.nbytes+scale.nbytes
        else:
            index=faiss.IndexPQ(d,96,8,faiss.METRIC_INNER_PRODUCT)
            index.train(corpus);index.add(corpus);size=len(faiss.serialize_index(index))
        built=time.perf_counter()-begin
        begin=time.perf_counter();_,ids=index.search(reference,10);elapsed=time.perf_counter()-begin
        overlap=np.mean([len(set(a)&set(b))/10 for a,b in zip(ids,truth)])
        top1=np.mean(ids[:,0]==truth[:,0])
        detection=[]
        cursor=0
        for case_number,case in enumerate(planted):
            n=len(case['vectors'])
            altered=dict(case)
            if method=='float32':
                altered['vectors']=corpus[cursor:cursor+n]
            elif method=='float16':
                altered['vectors']=corpus[cursor:cursor+n].astype('float16').astype('float32')
            elif method=='int8':
                altered['vectors']=compact[cursor:cursor+n].astype('float32')*scale[cursor:cursor+n]
            else:
                altered['vectors']=index.reconstruct_n(cursor,n)
            result=match(altered,load(files[0]),root,'dinov2',1)
            detected=bool(result and result['status']=='sequence_verified')
            error=abs(result['offset']-73) if result and result['offset'] is not None and case_number<4 else None
            detection.append({'case':case_number,'expected':case_number<4,'detected':detected,'offset_error':error})
            cursor+=n
        report.append(dict(method=method,stored_vector_bytes=int(size),bytes_per_vector=size/len(corpus),top10_overlap=float(overlap),top1_agreement=float(top1),build_seconds=built,query_seconds=elapsed,detection=detection))
    result={'vectors':len(corpus),'queries':len(reference),'dimensions':d,'results':report,
            'real_video_feature_files':len(real),
            'scope':'Controlled BSS positives plus real 2007 platform videos as distractors. Real features were already FP16 on disk; planted features retain original FP32. This is not representative of the whole public web. PQ trains on the candidate corpus; query frames come from the clean reference. Float16/int8 sizes are payload only; FAISS Flat expands to float32 in RAM.',
            'default':'float16; do not switch to PQ by default without independent large-scale recall evaluation'}
    Path('reports').mkdir(exist_ok=True)
    Path('reports/compression.json').write_text(json.dumps(result,indent=2),'utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
