"""Reproducible planted-excerpt benchmark using the user-supplied BSS upload.

Downloads are explicit, generated videos stay under data/validation.
Run: python scripts/validate.py --backend dinov2
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.media import digest
from lostmedia.worker import run


def ffmpeg(*args):
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",*map(str,args)],check=True,timeout=300)


def build(root, extended):
    root.mkdir(parents=True,exist_ok=True)
    source=root/"source.mp4"
    if not source.exists():
        subprocess.run([sys.executable,"-m","yt_dlp","--no-playlist","-f","bestvideo[height<=720]/best","-o",str(source),"https://www.youtube.com/watch?v=NndwzzpMunQ"],check=True)
    background=root/"background.mp4"
    if not background.exists():
        urllib.request.urlretrieve("https://archive.org/download/big-buck-bunny-1080-10s-2-mb/Big_Buck_Bunny_1080_10s_2MB.mp4",background)
    ref=root/"reference.mp4"
    if not ref.exists():
        ffmpeg("-ss",30,"-i",source,"-t",12,"-an","-vf","fps=24","-c:v","libx264","-crf",18,ref)
    base=root/"bed.mp4"
    if not base.exists():
        ffmpeg("-stream_loop",-1,"-i",background,"-t",180,"-an","-vf","scale=320:240:force_original_aspect_ratio=decrease,pad=320:240:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1","-c:v","libx264","-preset","fast","-crf",27,base)
    variants={"crop_compressed":"crop=iw*0.8:ih*0.8,scale=320:240"}
    if extended:
        variants.update({"flipped":"hflip,scale=320:240", "reaction":"crop=iw*.85:ih*.85,scale=208:156,pad=320:240:12:60:color=0x303040",
                         "speed125":"setpts=PTS/1.25,scale=320:240"})
    manifest=[]
    for name,filters in variants.items():
        clip=root/(name+"-clip.mp4")
        candidate=root/(name+".mp4")
        duration=9.6 if name=="speed125" else 12
        if not candidate.exists():
            ffmpeg("-i",ref,"-an","-vf",filters+",fps=24,setsar=1","-c:v","libx264","-crf",36,clip)
            graph=f"[0:v]trim=0:73,setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[0:v]trim=start={73+duration},setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1:a=0[out]"
            ffmpeg("-i",base,"-i",clip,"-filter_complex",graph,"-map","[out]","-an","-c:v","libx264","-preset","fast","-crf",30,candidate)
        manifest.append({"path":str(candidate.resolve()),"name":name,"expected":True,"offset":73.,"duration":duration})
    manifest.append({"path":str(base.resolve()),"name":"negative_bunny","expected":False})
    (root/"manifest.json").write_text(json.dumps(manifest,indent=2),"utf-8")
    return ref,manifest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--backend",default="dinov2",choices=["phash","dinov2","clip"])
    parser.add_argument("--extended",action="store_true")
    args=parser.parse_args()
    root=Path("data/validation").resolve()
    reference,manifest=build(root,args.extended)
    settings=Settings(root=root/("run-"+args.backend+"-"+str(time.time_ns())),backend=args.backend,interval=1,batch_size=32)
    db=Database(settings.root)
    db.add("local",digest(reference),"https://www.youtube.com/watch?v=NndwzzpMunQ&t=30","BSS user-supplied reference, 30-42 seconds",local_path=str(reference),reference=True)
    for item in manifest:
        item["id"]=db.add("local",digest(item["path"]),"",item["name"],local_path=item["path"])
    began=time.perf_counter()
    summary=run(settings,downloads=1)
    seconds=time.perf_counter()-began
    results=[]
    for item in manifest:
        matches=db.rows("SELECT payload FROM matches WHERE video_id=? AND signature=?",(item["id"],summary["signature"]))
        best=json.loads(matches[0]["payload"]) if matches else None
        detected=bool(best and best["status"]=="sequence_verified")
        error=abs(best["offset"]-item["offset"]) if best and best.get("offset") is not None and item["expected"] else None
        success=detected and error is not None and error<=2 if item["expected"] else not detected
        results.append({**item,"detected":detected,"offset_error_seconds":error,"success":success,"match":best})
    positives=[r for r in results if r["expected"]]
    negatives=[r for r in results if not r["expected"]]
    report={"source":"https://www.youtube.com/watch?v=NndwzzpMunQ","reference_source_start":30,"reference_length":12,
            "candidate_length":180,"backend":args.backend,"seconds":seconds,"summary":summary,"data_root":str(settings.root),"feature_precision":settings.feature_precision,
            "recall_at_2_seconds":sum(r["success"] for r in positives)/len(positives),
            "false_positive_rate":sum(r["detected"] for r in negatives)/len(negatives),
            "scope":"Controlled planted-excerpt test, not a real-world precision estimate; background repeated from a real public video.","results":results}
    (root/("report-"+args.backend+".json")).write_text(json.dumps(report,indent=2).replace(json.dumps(str(Path.cwd()))[1:-1],"."),"utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="results"},indent=2))
    for r in results:
        print(r["name"],"PASS" if r["success"] else "FAIL",r["offset_error_seconds"])
    return 0 if all(r["success"] for r in results) and not summary["failed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
