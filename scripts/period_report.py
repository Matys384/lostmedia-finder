"""Period audit: discovery coverage, processing outcomes, throughput per stage/platform, pacing, validation.

Writes reports/period-<start>.json and reports/period-<start>.md. Every figure is read from the
catalog database or from validation reports at run time; nothing is extrapolated.
Run: python scripts/period_report.py --period 2007-08-15
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lostmedia.cli import settings  # noqa: E402
from lostmedia.db import Database  # noqa: E402

STATES = ["done", "queued", "running", "failed", "deferred", "ignored", "restricted", "inaccessible"]


def table(db, name):
    return bool(db.rows("SELECT name FROM sqlite_master WHERE name=?", (name,)))


def collect(db, period):
    pid = db.rows("SELECT id FROM periods WHERE start=?", (period["start"],))[0]["id"]
    out = {"period": period, "generated": time.strftime("%Y-%m-%d %H:%M:%S")}
    # Discovery coverage: what each configured method returned, and which windows remain open.
    out["discovered"] = {r["provider"]: r["n"] for r in db.rows("SELECT provider,COUNT(*) n FROM period_videos WHERE period_id=? GROUP BY provider", (pid,))}
    out["windows"] = db.rows("SELECT provider,state,COUNT(*) windows,SUM(pages) pages FROM frontier WHERE period_id=? GROUP BY provider,state ORDER BY provider,state", (pid,))
    out["open_or_saturated_windows"] = db.rows("SELECT provider,start,end,query,pages,state,error FROM frontier WHERE period_id=? AND state IN ('pending','blocked','saturated','unavailable') ORDER BY provider,start", (pid,))
    out["api_reported_totals"] = db.rows("SELECT provider,start,end,query,total,endpoint FROM observed_counts WHERE start>=? AND end<=? ORDER BY provider,start", (period["start"], period["end"])) if table(db, "observed_counts") else []
    out["discovery_methods"] = db.rows("SELECT provider,method,COUNT(*) calls,SUM(returned) returned,SUM(new) new,SUM(seconds) seconds FROM discovery_events WHERE start>=? AND end<=? GROUP BY provider,method", (period["start"], period["end"]+"T99")) if table(db, "discovery_events") else []
    out["discovery_quality"] = db.rows("SELECT provider,method,COUNT(*) calls,SUM(returned) returned,SUM(new) new,SUM(duplicates) duplicates,SUM(filtered) filtered FROM discovery_quality GROUP BY provider,method") if table(db, "discovery_quality") else []
    out["youtube_api_calls_by_day"] = db.rows("SELECT day,calls FROM quota WHERE provider='youtube' ORDER BY day")
    # Processing outcomes per platform; the rows sum to the discovered count.
    rows = db.rows("SELECT pv.provider,j.state,COUNT(*) n FROM period_videos pv JOIN jobs j ON j.video_id=pv.video_id WHERE pv.period_id=? GROUP BY pv.provider,j.state", (pid,))
    states = {}
    for r in rows:
        states.setdefault(r["provider"], {s: 0 for s in STATES})[r["state"]] = r["n"]
    out["states"] = states
    out["errors"] = db.rows("""SELECT pv.provider,j.state,substr(j.error,1,90) error,COUNT(*) n FROM period_videos pv JOIN jobs j ON j.video_id=pv.video_id
        WHERE pv.period_id=? AND j.state NOT IN ('done','queued','running') GROUP BY pv.provider,j.state,substr(j.error,1,60) ORDER BY n DESC LIMIT 40""", (pid,))
    out["stages"] = db.rows("""SELECT v.source,COUNT(*) videos,AVG(m.download_seconds) download_s,AVG(m.decode_seconds) decode_s,AVG(m.gpu_seconds) gpu_s,
        AVG(m.match_seconds) match_s,AVG(m.media_bytes) media_bytes,AVG(m.feature_bytes) feature_bytes,AVG(m.frames) views
        FROM processing_metrics m JOIN videos v ON v.id=m.video_id JOIN period_videos pv ON pv.video_id=v.id
        WHERE pv.period_id=? AND m.download_seconds>0 GROUP BY v.source""", (pid,))
    if table(db, "download_attempts"):
        out["download_attempts"] = db.rows("SELECT source,spacing,outcome,COUNT(*) n,AVG(seconds) seconds FROM download_attempts GROUP BY source,spacing,outcome ORDER BY source,spacing DESC,outcome")
        hourly = db.rows("""SELECT source,strftime('%Y-%m-%d %H:00',finished,'unixepoch','localtime') hour,COUNT(*) attempts,
            SUM(outcome='ok') ok,SUM(outcome='throttled') throttled,SUM(outcome='transient') transient FROM download_attempts GROUP BY source,hour ORDER BY hour""")
        out["download_attempts_by_hour"] = hourly
    if table(db, "tuning"):
        from lostmedia.tuning import LADDERS
        out["pacing"] = {r["source"]: {"spacing": LADDERS[r["source"]][r["step"]], "stable": LADDERS[r["source"]][r["stable"]],
                                       "fastest_allowed": LADDERS[r["source"]][r["ceiling"]], "throttles": r["throttles"],
                                       "history": json.loads(r["history"] or "[]")} for r in db.rows("SELECT * FROM tuning") if r["source"] in LADDERS}
    out["matches"] = db.rows("""SELECT v.source,v.external_id,v.title,m.score,json_extract(m.payload,'$.status') status,json_extract(m.payload,'$.temporal_frames') frames,
        json_extract(m.payload,'$.offset') offset FROM matches m JOIN videos v ON v.id=m.video_id JOIN period_videos pv ON pv.video_id=v.id
        WHERE pv.period_id=? ORDER BY m.score DESC LIMIT 25""", (pid,))
    out["validation"] = {}
    for path in sorted(Path("reports").glob("validation-hard-*.json")):
        report = json.loads(path.read_text("utf-8"))
        out["validation"][path.stem] = {k: report.get(k) for k in ("interval", "reference_interval", "crops", "positives", "negatives", "recall_at_2_seconds", "false_positives")}
        out["validation"][path.stem]["missed"] = [r["name"] for r in report["results"] if r["expected"] is True and not r["success"]]
    return out


def markdown(r):
    lines = [f"# Bilan de période {r['period']['start']} → {r['period']['end']}", "", f"Généré le {r['generated']} depuis la base. Aucun chiffre extrapolé.", ""]
    lines += ["## Découverte", "", "| Plateforme | Vidéos uniques |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(r["discovered"].items())] + [f"| **Total** | **{sum(r['discovered'].values())}** |", ""]
    lines += ["| Plateforme | État des fenêtres | Fenêtres | Pages lues |", "|---|---|---:|---:|"]
    lines += [f"| {w['provider']} | {w['state']} | {w['windows']} | {w['pages']} |" for w in r["windows"]] + [""]
    if r["open_or_saturated_windows"]:
        lines += ["Fenêtres non épuisées ou saturées (couverture résiduelle inconnue) :", ""]
        lines += [f"- {w['provider']} {w['start']}→{w['end']} `{w['query']}` : {w['state']} ({w['error'] or ''})" for w in r["open_or_saturated_windows"]] + [""]
    lines += ["## Traitement par plateforme", "", "| Plateforme | " + " | ".join(STATES) + " | Somme |", "|---|" + "---:|"*(len(STATES)+1)]
    for provider, s in sorted(r["states"].items()):
        lines.append(f"| {provider} | " + " | ".join(str(s[k]) for k in STATES) + f" | {sum(s.values())} |")
    lines += ["", "## Temps moyens par étape (vidéos téléchargées)", "", "| Plateforme | Vidéos | Téléchargement s | Décodage CPU s | GPU s | Comparaison s | Mo média |", "|---|---:|---:|---:|---:|---:|---:|"]
    f = lambda v, d=1: "–" if v is None else f"{v:.{d}f}"
    lines += [f"| {x['source']} | {x['videos']} | {f(x['download_s'])} | {f(x['decode_s'])} | {f(x['gpu_s'], 2)} | {f(x['match_s'], 2)} | {f((x['media_bytes'] or 0)/1e6)} |" for x in r["stages"]] + [""]
    if r.get("pacing"):
        lines += ["## Cadence des téléchargements", "", "| Plateforme | Intervalle actuel | Stable | Plus rapide autorisé | Limitations |", "|---|---:|---:|---:|---:|"]
        lines += [f"| {k} | {v['spacing']} s | {v['stable']} s | {v['fastest_allowed']} s | {v['throttles']} |" for k, v in r["pacing"].items()] + [""]
        for k, v in r["pacing"].items():
            for h in v["history"][-8:]:
                lines.append(f"- {k} {time.strftime('%H:%M', time.localtime(h['at']))} : {h['from']} s → {h['to']} s — {h['reason'][:140]}")
        lines.append("")
    if r["validation"]:
        lines += ["## Validation BSS contrôlée", "", "| Rapport | Intervalle | Réf. | Vues | Positifs | Rappel | Faux positifs / négatifs | Manqués |", "|---|---:|---:|---|---:|---:|---:|---|"]
        lines += [f"| {k} | {v['interval']} s | {v['reference_interval']} s | {v['crops']} | {v['positives']} | {v['recall_at_2_seconds']:.0%} | {v['false_positives']}/{v['negatives']} | {', '.join(v['missed'])} |" for k, v in r["validation"].items()] + [""]
    lines += ["## Correspondances", ""]
    verified = [m for m in r["matches"] if m["status"] == "sequence_verified"]
    lines.append(f"{len(verified)} séquence(s) vérifiée(s) ; {len(r['matches'])-len(verified)} candidat(s) visuel(s) faible(s) listés dans le JSON.")
    return "\n".join(lines)+"\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/chronological")
    parser.add_argument("--period", default="2007-08-15")
    args = parser.parse_args()
    db = Database(settings(Path(args.data)).root)
    period = db.rows("SELECT start,end,state FROM periods WHERE start=?", (args.period,))[0]
    report = collect(db, period)
    stem = Path("reports")/f"period-{args.period}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    stem.with_suffix(".md").write_text(markdown(report), "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
