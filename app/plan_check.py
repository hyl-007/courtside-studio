#!/usr/bin/env python3
"""plan_check.py: dry-run an edit plan (a full project.json) without touching any real project or rendering anything.

  python3 plan_check.py PLAN.json [--source /path/to/video.mp4] [--transcript /path/to/transcript.json]

Prints:
  - whether the server would accept the project (the same validation the Studio uses),
  - the exact length of every segment and of the whole reel (hook + clips + outro, including the padding and transitions),
  - what is actually SAID inside each cut, so you can check every cut starts and ends on a whole sentence.
Writes only to a temporary folder. The default source is the coach-sim raw video.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "library"
DEF_SRC = LIB / "coach-sim-1on1" / "source" / "coach_sim_1on1_raw.mp4"
DEF_TR = LIB / "coach-sim-1on1" / "transcripts" / "coach_sim_1on1_raw.json"


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); return 0
    plan = Path(a[0]); opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    src = Path(opt("--source", str(DEF_SRC))); tr = Path(opt("--transcript", str(DEF_TR)))
    proj = json.loads(plan.read_text())
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout.strip())
    sys.path.insert(0, str(HERE))
    import server                                                     # only for check_project; nothing is started
    err = server.check_project(proj, dur, {})
    print("VALIDATION:", "OK, the Studio would accept this plan" if not err else f"REJECTED: {err}")
    if err:
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="hyl_plan_")); (tmp / "transcripts").mkdir()
    (tmp / "meta.json").write_text(json.dumps({"name": "plan check", "slug": "plan-check", "source": str(src), "duration": dur}))
    (tmp / "transcripts" / f"{src.stem}.json").symlink_to(tr)
    (tmp / "project.json").write_text(json.dumps(proj))
    env = dict(os.environ, ZBA_PDIR=str(tmp), ZBA_PROJECT=str(tmp / "project.json"))
    r = subprocess.run([sys.executable, str(HERE / "build.py"), "--durations"], cwd=HERE, env=env, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print("DURATIONS FAILED:", (r.stderr or r.stdout)[-600:]); return 1
    rows = json.loads(r.stdout.strip().splitlines()[-1])
    words = [w for w in json.loads(tr.read_text())["words"] if w.get("type") == "word" and w.get("start") is not None]
    total = sum(x["dur"] for x in rows)
    print(f"\nTOTAL REEL LENGTH: {total:.1f} s  ({int(total // 60)}:{total % 60:04.1f})\n")
    segs = {s["id"]: s for s in proj["segments"]}
    for x in rows:
        s = segs[x["id"]]; line = f"{x['start']:6.1f}-{x['start'] + x['dur']:6.1f}  {x['dur']:5.1f}s  {x['id']:8s} {x['kind']:5s}"
        if "a1" in x:
            line += f"  source {x['a1']:.2f}-{x['b1']:.2f}"
        if s.get("tip_title"):
            line += f"  [{s.get('tip')}: {s['tip_title']}]"
        if s.get("fz"):
            line += f"  freeze x{len(s['fz'])}"
        print(line)
        if "a1" in x:
            said = " ".join(w["text"] for w in words if w["start"] >= x["a1"] - 0.01 and w["end"] <= x["b1"] + 0.01)
            print(f"           says: {said[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
