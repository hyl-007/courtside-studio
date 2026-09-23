#!/usr/bin/env python3
"""zba_cli.py: the ONLY tool the Studio's Claude assistant is allowed to run.

It talks to the local HYL's Studio server (127.0.0.1) for one project and offers a small, validated
set of commands. Every edit goes through the server's validation, so a bad edit is refused instead
of breaking the project, and every change lands in the undo history.

  outline                       the edit as a readable table (times, titles, what is said, on-screen text)
  project                       the full project.json
  changes                       what has been changed since the original (including the user's own edits)
  transcript --from S --to E    words spoken between two source times (seconds)
  find "text"                   where a phrase is spoken (source times, with context)
  media                         files in the media bin
  patch                         apply edits: read a JSON list of operations from stdin (see the instructions)
  caption "text"                save a caption suggestion for the user to review
  preview                       render a fast 540p preview (never the final) and wait for it
  set PATH VALUE                change one value (number, true/false or text), e.g. set segments/session/zoom 1.1
  word MS TEXT / unword MS      change / restore the caption text of the word that starts at MS milliseconds
  delseg ID / moveseg ID after|before OTHER
  qa                            run the automatic quality check on the latest preview (beat sync, pacing, hook, loudness, dead air...)
                                and print a scorecard with what to fix. Use it in a loop: preview -> qa -> patch -> preview -> qa.
  beats MEDIA_ID                beat times of a music file from the media bin (seconds) and the tempo
"""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("ZBA_STUDIO_URL", "http://127.0.0.1:8765").rstrip("/")
SLUG = os.environ.get("ZBA_SLUG", "")
HDR = {"X-Actor": "assistant", "Content-Type": "application/json"}


def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(f"{BASE}/api/p/{SLUG}/{path}", method=method, headers=HDR,
                                 data=(json.dumps(body).encode() if body is not None else None))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:  # noqa: BLE001
            msg = e.reason
        print(f"ERROR ({e.code}): {msg}", file=sys.stderr); sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach the Studio server ({e.reason})", file=sys.stderr); sys.exit(1)


def t(s):
    s = max(0.0, float(s or 0)); return f"{int(s // 60)}:{s % 60:04.1f}"


def cmd_outline():
    rows = call("GET", "outline")
    for i, r in enumerate(rows, 1):
        head = f"#{i} [{r['id']}] {r['kind']}"
        if r.get("start") is not None:
            head += f"  reel {t(r['start'])}-{t(r['start'] + r['dur'])} ({r['dur']:.1f}s)"
        if r.get("src"):
            head += f"  source {r['src'][0]:.2f}-{r['src'][1]:.2f}"
        print(head + (f"  \"{r['title']}\"" if r.get("title") else ""))
        if r.get("speech"):
            sp = r["speech"]; print("     says:", sp if len(sp) < 260 else sp[:257] + "...")
        for o in r.get("onscreen") or []:
            print("     on screen:", o)
        for f in r.get("freezes") or []:
            print(f"     freeze [{f['name']}] at source {f['at']:.2f}: " + " | ".join(f["labels"]))


def words():
    return call("GET", "transcript")


def cmd_transcript(a):
    lo, hi = 0.0, 1e9
    if "--from" in a: lo = float(a[a.index("--from") + 1])
    if "--to" in a: hi = float(a[a.index("--to") + 1])
    ws = [w for w in words() if w[0] >= lo - 0.01 and w[1] <= hi + 0.01]
    if not ws:
        print("(no words in that range)"); return
    line, start = [], ws[0][0]
    for w in ws:
        line.append(w[2])
        if len(line) >= 12 or w[2].endswith((".", "?", "!")):
            print(f"{start:8.2f}  " + " ".join(line)); line = []; start = None
        if start is None: start = w[0]
    if line: print(f"{start:8.2f}  " + " ".join(line))


def cmd_find(q):
    q = q.lower().strip(); ws = words(); hits = 0
    toks = q.split()
    for i in range(len(ws) - len(toks) + 1):
        if [w[2].lower().strip(".,?!") for w in ws[i:i + len(toks)]] == [x.strip(".,?!") for x in toks]:
            ctx = " ".join(w[2] for w in ws[max(0, i - 6):i + len(toks) + 8])
            print(f"{ws[i][0]:8.2f}  ...{ctx}..."); hits += 1
            if hits >= 25: break
    if not hits: print("(not found)")


def cmd_patch():
    raw = sys.stdin.read().strip()
    if not raw: print("ERROR: send a JSON list of operations on stdin", file=sys.stderr); sys.exit(1)
    try:
        ops = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: that is not valid JSON ({e})", file=sys.stderr); sys.exit(1)
    r = call("POST", "patch", {"ops": ops})
    print("OK" if r.get("saved") else "OK (nothing changed)")
    for line in r.get("summary", []): print(" -", line)


def cmd_preview():
    r = call("POST", "render", {"draft": True})
    if not r.get("started"): print("A render is already running; try again shortly."); return
    last = ""
    for _ in range(400):
        time.sleep(2); st = call("GET", "status")
        msg = f"{st['done']}/{st['total']} {st.get('current') or ''}".strip()
        if msg != last: print("  rendering", msg, flush=True); last = msg
        if not st["running"]:
            print("Preview ready." if st.get("ok") else "Preview failed: " + " / ".join(st["log"][-3:])); return
    print("Still rendering after 13 minutes; it will finish in the background.")


def apply_ops(ops):
    r = call("POST", "patch", {"ops": ops})
    print("OK" if r.get("saved") else "OK (nothing changed)")
    for line in r.get("summary", []): print(" -", line)


def parse_val(txt):
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


def cmd_qa():
    r = call("POST", "qa_run", {})
    if not r.get("started"):
        print("A quality check is already running; try again shortly."); return
    for _ in range(300):
        time.sleep(3); st = call("GET", "qa")
        if not st["running"]:
            break
    if st.get("error"):
        print("QA failed:", st["error"]); return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import qa
    print(qa.report_text(st["report"]))
    if len(st["history"]) > 1:
        print("  score history:", " -> ".join(str(h["score"]) for h in st["history"][-6:]))


def cmd_beats(mid):
    b = call("GET", f"beats?media={mid}")
    print(f"tempo {b['tempo']} BPM, {len(b['beats'])} beats"); print(" ".join(f"{x:.2f}" for x in b["beats"][:120]))


def main():
    a = sys.argv[1:]
    if not SLUG: print("ERROR: no project selected", file=sys.stderr); sys.exit(1)
    if not a or a[0] in ("-h", "--help"): print(__doc__); return
    c = a[0]
    if c == "outline": cmd_outline()
    elif c == "project": print(json.dumps(call("GET", "project"), indent=1))
    elif c == "changes":
        for line in call("GET", "changes") or ["(nothing changed since the original)"]: print("-", line)
    elif c == "transcript": cmd_transcript(a[1:])
    elif c == "find" and len(a) > 1: cmd_find(" ".join(a[1:]))
    elif c == "media":
        for m in call("GET", "media"): print(f"{m['id']}  {m['kind']:5s} {m['duration']:7.1f}s  {m['name']}")
    elif c == "patch": cmd_patch()
    elif c == "caption" and len(a) > 1: call("POST", "caption_suggestion", {"text": " ".join(a[1:])}); print("Caption suggestion saved for the user to review.")
    elif c == "preview": cmd_preview()
    elif c == "set" and len(a) > 2: apply_ops([{"op": "set", "path": a[1], "value": parse_val(" ".join(a[2:]))}])
    elif c == "word" and len(a) > 2: apply_ops([{"op": "set_caption", "ms": int(a[1]), "text": " ".join(a[2:])}])
    elif c == "unword" and len(a) > 1: apply_ops([{"op": "clear_caption", "ms": int(a[1])}])
    elif c == "delseg" and len(a) > 1: apply_ops([{"op": "delete_segment", "id": a[1]}])
    elif c == "moveseg" and len(a) > 3 and a[2] in ("after", "before"): apply_ops([{"op": "move_segment", "id": a[1], a[2]: a[3]}])
    elif c == "qa": cmd_qa()
    elif c == "beats" and len(a) > 1: cmd_beats(a[1])
    else: print("Unknown command. Run with --help.", file=sys.stderr); sys.exit(2)


if __name__ == "__main__":
    main()
