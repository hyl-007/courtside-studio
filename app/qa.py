#!/usr/bin/env python3
"""qa.py: automatic quality check for a finished edit (the "self QA loop").

  python3 qa.py analyze VIDEO [--kind highlight|micd] [--project PROJECT.json] [--ref PROFILE.json] [--out DIR] [--music FILE] [--boundaries FILE.json]
  python3 qa.py profile VIDEO [--out profile.json]       measure a reference video (what the good edits look like)
  python3 qa.py beats AUDIO_OR_VIDEO                     beat times of a song

It measures what makes a highlight / mic'd-up reel work on Instagram and TikTok: do the cuts land on the beat, is the pacing tight,
is there a hook in the first second, is the sound loud but not clipped, is there dead air, black frames, an abrupt end.
It writes a JSON report and a contact sheet (a picture of every cut, marked on-beat / off-beat) so a person or Claude can look at it.
Everything is read-only on the video. Nothing leaves the machine.
"""
from __future__ import annotations
import json, math, os, re, subprocess, sys, tempfile
from pathlib import Path

import numpy as np

TOL_MS = 50            # a cut within 50 ms of a beat (about 1.5 frames at 30 fps) counts as "on the beat"
KINDS = {"highlight": "highlight reel set to music", "micd": "mic'd-up session"}


def sh(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels,rotation:stream_side_data=rotation",
            "-of", "json", str(path)]); j = json.loads(r.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s["codec_type"] == "video"), {}); a = next((s for s in j.get("streams", []) if s["codec_type"] == "audio"), {})
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    for sd in v.get("side_data_list", []) or []:
        if abs(int(sd.get("rotation", 0))) in (90, 270):
            w, h = h, w
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
    return dict(duration=float(j.get("format", {}).get("duration", 0)), width=w, height=h, fps=round(float(num) / max(float(den), 1), 2), has_audio=bool(a),
                vcodec=v.get("codec_name"), size_mb=round(int(j.get("format", {}).get("size", 0)) / 1e6, 1))


# ------------------------------------------------------------------ measurements

def audio_wav(path, sr=22050):
    tmp = Path(tempfile.mkdtemp(prefix="qa_")) / "a.wav"
    r = sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), str(tmp)])
    return tmp if tmp.exists() else None


def beat_info(path):
    """Beat times of the soundtrack. Uses librosa's beat tracker, then nudges each beat onto the nearest real onset."""
    import librosa
    wav = audio_wav(path)
    if not wav:
        return dict(tempo=0, beats=[], onsets=[], energy=[], hop=0.25)
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    hop = 256
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=hop, units="time", tightness=120)
    onsets = librosa.onset.onset_detect(onset_envelope=oenv, sr=sr, hop_length=hop, units="time", backtrack=False)
    tempo = float(np.atleast_1d(tempo)[0])
    snapped = []
    for b in beats:                                              # the tracker works on a coarse grid; a real drum hit within 45 ms is the true beat
        near = onsets[np.argmin(np.abs(onsets - b))] if len(onsets) else b
        snapped.append(float(near if abs(near - b) <= 0.045 else b))
    rms = librosa.feature.rms(y=y, frame_length=int(sr * 0.25), hop_length=int(sr * 0.25))[0]
    try:
        os.remove(wav); os.rmdir(wav.parent)
    except OSError:
        pass
    return dict(tempo=round(tempo, 1), beats=[round(b, 3) for b in snapped], onsets=[round(float(o), 3) for o in onsets], energy=[round(float(x), 5) for x in rms], hop=0.25)


def detect_cuts(path, fps_probe=60.0, thr=0.16):
    """Hard cuts (and flash cuts) from the picture. Frame-accurate to about 1/60 s. Soft dissolves are not counted."""
    r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf", f"fps={fps_probe},scale=180:-2:flags=fast_bilinear,select='gt(scene,{thr})',showinfo", "-f", "null", "-"])
    ts = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr)]
    out = []
    for t in ts:
        if not out or t - out[-1] > 0.18:                          # a flash or a two-frame dissolve is one cut, not two
            out.append(round(t, 3))
    return [t for t in out if t > 0.08]


def loudness(path):
    r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    t = r.stderr[r.stderr.rfind("Summary:"):] if "Summary:" in r.stderr else ""
    g = lambda pat: (float(m.group(1)) if (m := re.search(pat, t)) else None)
    return dict(lufs=g(r"I:\s+(-?[0-9.]+)\s+LUFS"), lra=g(r"LRA:\s+(-?[0-9.]+)\s+LU"), true_peak=g(r"Peak:\s+(-?[0-9.]+)\s+dBFS"))


def silences(path, thresh=-45, min_d=0.4):
    r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", f"silencedetect=noise={thresh}dB:d={min_d}", "-f", "null", "-"])
    st = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", r.stderr)]; en = [float(x) for x in re.findall(r"silence_end: (-?[0-9.]+)", r.stderr)]
    return [dict(start=max(0.0, a), end=b) for a, b in zip(st, en)] + ([dict(start=max(0.0, st[-1]), end=None)] if len(st) > len(en) else [])


def black_frames(path):
    r = sh(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf", "fps=30,scale=160:-2,blackdetect=d=0.1:pix_th=0.12", "-f", "null", "-"])
    return [dict(start=float(a), end=float(b)) for a, b in re.findall(r"black_start:([0-9.]+) black_end:([0-9.]+)", r.stderr)]


def visual_stats(path, dur):
    import cv2
    cap = cv2.VideoCapture(str(path)); n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0); fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(fps / 2)); br, sat, shp, clip = [], [], [], []
    for i in range(0, n, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i); ok, f = cap.read()
        if not ok:
            continue
        f = cv2.resize(f, (360, int(360 * f.shape[0] / f.shape[1]))); g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY); hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        br.append(float(g.mean())); sat.append(float(hsv[..., 1].mean())); shp.append(float(cv2.Laplacian(g, cv2.CV_64F).var())); clip.append(float((g > 250).mean()))
    cap.release()
    m = lambda a: round(float(np.mean(a)), 2) if a else None
    return dict(brightness=m(br), saturation=m(sat), sharpness=m(shp), blown_pct=round(100 * m(clip), 2) if clip else None)


def contact_sheet(path, cuts, beats, out_png, dur, max_tiles=30):
    """A picture of the edit: the first frame of every shot, with the time and whether the cut hits the beat (green) or misses it (red)."""
    from PIL import Image, ImageDraw
    times = [0.05] + [c + 0.06 for c in cuts]
    if len(times) > max_tiles:
        times = [times[int(i * (len(times) - 1) / (max_tiles - 1))] for i in range(max_tiles)]
    tw, cols = 170, 6; frames = []
    for t in times:
        tmp = Path(tempfile.mkdtemp(prefix="qa_")) / "f.png"
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(max(t, 0), max(dur - 0.05, 0)):.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={tw}:-2", str(tmp)])
        if tmp.exists():
            frames.append((t, Image.open(tmp).convert("RGB")))
    if not frames:
        return None
    th = frames[0][1].height; rows = math.ceil(len(frames) / cols); sheet = Image.new("RGB", (cols * tw, rows * (th + 16)), (14, 18, 48)); d = ImageDraw.Draw(sheet)
    bt = np.array(beats) if len(beats) else None
    for i, (t, im) in enumerate(frames):
        x, y = (i % cols) * tw, (i // cols) * (th + 16); sheet.paste(im, (x, y + 16))
        off = float(np.min(np.abs(bt - (t - 0.06)))) * 1000 if bt is not None and i > 0 else None
        col = (120, 220, 140) if off is not None and off <= TOL_MS else (240, 110, 110) if off is not None else (200, 200, 220)
        d.text((x + 4, y + 2), f"{t - 0.06 if i else 0:5.2f}s" + (f"  {off:+.0f}ms".replace("+", "") if off is not None else ""), fill=col)
    sheet.save(out_png)
    return str(out_png)


# ------------------------------------------------------------------ analysis

def sync_stats(cuts, beats, tempo):
    if not cuts or not len(beats):
        return dict(on_beat_pct=None, on_grid_pct=None, median_abs_ms=None, mean_signed_ms=None, per_cut=[])
    b = np.array(beats); grid = np.array(sorted(set(list(beats) + [(x + y) / 2 for x, y in zip(beats[:-1], beats[1:])])))    # beats plus off-beat eighths
    per = []
    for c in cuts:
        i = int(np.argmin(np.abs(b - c))); off = (c - b[i]) * 1000; g = float(np.min(np.abs(grid - c)) * 1000)
        per.append(dict(t=round(c, 3), beat=round(float(b[i]), 3), off_ms=round(off, 1), on_beat=abs(off) <= TOL_MS, on_grid=g <= TOL_MS))
    offs = np.array([p["off_ms"] for p in per])
    return dict(on_beat_pct=round(100 * float(np.mean([p["on_beat"] for p in per])), 1), on_grid_pct=round(100 * float(np.mean([p["on_grid"] for p in per])), 1),
                median_abs_ms=round(float(np.median(np.abs(offs))), 1), mean_signed_ms=round(float(np.mean(offs[np.abs(offs) < 250])) if np.any(np.abs(offs) < 250) else 0.0, 1), per_cut=per)


def drop_info(energy, hop, cuts):
    """The biggest jump in loudness after a quieter stretch: the 'drop'. A great highlight has a cut or a hero moment right on it."""
    e = np.array(energy)
    if len(e) < 8:
        return None
    sm = np.convolve(e, np.ones(2) / 2, mode="same"); best, bi = 0.0, None
    for i in range(3, len(sm) - 1):
        rise = sm[i + 1] - np.mean(sm[max(0, i - 4):i])
        if rise > best:
            best, bi = rise, i + 1
    if bi is None or best < 0.35 * (np.max(e) + 1e-9):
        return None
    t = bi * hop; near = min(cuts, key=lambda c: abs(c - t)) if cuts else None
    return dict(t=round(t, 2), nearest_cut=round(near, 2) if near is not None else None, gap_ms=round(abs(near - t) * 1000) if near is not None else None)


def analyze(video, kind="highlight", project=None, ref=None, out_dir=None, music=None, sheet=True, boundaries=None):
    video = Path(video); info = probe(video); dur = info["duration"]
    cuts = detect_cuts(video)
    for b in (boundaries or []):                                   # clip edges the project knows about, including soft dissolves the picture scan cannot see
        if 0.1 < b < dur - 0.1 and all(abs(b - c) > 0.25 for c in cuts):
            cuts.append(round(float(b), 3))
    cuts.sort()
    beat = beat_info(music or video) if info["has_audio"] or music else dict(tempo=0, beats=[], onsets=[], energy=[], hop=0.25)
    sync = sync_stats(cuts, beat["beats"], beat["tempo"])
    shots = np.diff([0.0] + cuts + [dur]) if dur else np.array([])
    lo = loudness(video) if info["has_audio"] else dict(lufs=None, lra=None, true_peak=None)
    sil = silences(video) if info["has_audio"] else []
    blk = black_frames(video); vis = visual_stats(video, dur)
    third = dur / 3 if dur else 1; cr = lambda a, b: sum(1 for c in cuts if a <= c < b) / max(b - a, 1e-6)
    m = dict(file=str(video), kind=kind, info=info, cuts=cuts, n_cuts=len(cuts), asl=round(float(shots.mean()), 2) if len(shots) else None,
             median_shot=round(float(np.median(shots)), 2) if len(shots) else None, max_shot=round(float(shots.max()), 2) if len(shots) else None,
             short_shots_pct=round(100 * float(np.mean(shots < 0.5)), 1) if len(shots) else None, first_cut=cuts[0] if cuts else None,
             cut_rate_first_third=round(cr(0, third), 2), cut_rate_last_third=round(cr(2 * third, dur + 1), 2),
             beat=dict(tempo=beat["tempo"], n=len(beat["beats"]), times=beat["beats"]), sync=sync, drop=drop_info(beat["energy"], beat["hop"], cuts),
             cuts_per_beat=round(len(cuts) / max(len(beat["beats"]), 1), 2) if beat["beats"] else None, loudness=lo, silences=sil, black=blk, visual=vis)
    if info["has_audio"] and beat["energy"]:
        e = np.array(beat["energy"]); m["audio_start_db"] = round(20 * math.log10(max(float(e[0]), 1e-6)), 1); tail = float(e[-1]); body = float(np.median(e))
        m["ending_drop_db"] = round(20 * math.log10(max(tail, 1e-6) / max(body, 1e-6)), 1)
    if project:
        p = json.loads(Path(project).read_text()); st = p.get("settings", {})
        m["captions"] = dict(lead=st.get("caption_lead", 0.22), tail=st.get("caption_tail", 0.6), min_on_screen=st.get("caption_min", 1.1), intense=bool(st.get("caption_intense")), words=st.get("caption_words", 3))
    m["rules"], m["score"] = grade(m, kind, ref)
    m["fixes"] = [dict(cut=c["t"], nearest_beat=c["beat"], shift_ms=round(-c["off_ms"])) for c in sync["per_cut"] if not c["on_beat"] and abs(c["off_ms"]) < 220]
    if out_dir:
        out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
        if sheet:
            m["sheet"] = contact_sheet(video, cuts, beat["beats"], out / "sheet.png", dur)
        (out / "report.json").write_text(json.dumps(m, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    return m


RULES_W = {"highlight": dict(beat_sync=30, sync_bias=4, rhythm=6, pacing=10, hook=8, drop=10, build=5, loudness=10, ending=5, dead_air=5, format=3, length=4),
           "micd": dict(hook=15, pacing=14, loudness=16, dead_air=12, black=6, captions=17, format=6, length=8, ending=6)}


def _st(ok, warn=False):
    return "pass" if ok else "warn" if warn else "fail"


def grade(m, kind, ref=None):
    r = []; add = lambda id_, title, value, target, status, advice: r.append(dict(id=id_, title=title, value=value, target=target, status=status, advice=advice))
    s, lo, info = m["sync"], m["loudness"], m["info"]
    ref_on = (ref or {}).get("sync", {}).get("on_beat_pct")
    if kind == "highlight":
        tgt = max(80.0, (ref_on or 0) + 10)                                                       # aim to beat the reference, not just match it
        v = s["on_beat_pct"]
        add("beat_sync", "Cuts land on the beat", None if v is None else f"{v}% within ±{TOL_MS} ms", f"≥ {tgt:.0f}%",
            "fail" if v is None else _st(v >= tgt, v >= tgt - 20), "Snap every cut to the nearest beat (Studio: Beat ▸ Snap cuts to beats). Cuts listed in `fixes` are off." if v is not None else "No beats found: is the music audible?")
        b = s["mean_signed_ms"]
        add("sync_bias", "Cuts a hair early, not late", None if b is None else f"{b:+.0f} ms average", "−40 … +15 ms", "fail" if b is None else _st(-40 <= b <= 15, -70 <= b <= 40), "Cuts that land after the beat feel sluggish. Shift the picture cuts 1–2 frames earlier than the drum hit.")
        cpb = m["cuts_per_beat"]
        add("rhythm", "One cut every 1–2 beats", None if cpb is None else f"{cpb} cuts per beat", "0.35 … 1.1", "fail" if cpb is None else _st(0.35 <= cpb <= 1.1, 0.2 <= cpb <= 1.5), "Too many cuts feels frantic, too few loses the beat. Aim for a cut every beat or two, faster near the drop.")
        a = m["asl"]
        add("pacing", "Tight pacing", None if a is None else f"{a}s average shot, longest {m['max_shot']}s", "0.6 … 2.0 s, none over 3.5 s", "fail" if a is None else _st(0.6 <= a <= 2.0 and m["max_shot"] <= 3.5, 0.4 <= a <= 2.6 and m["max_shot"] <= 5), "Trim shots that outstay the beat; cut on the action, not after it.")
        fc = m["first_cut"]; a0 = m.get("audio_start_db")
        add("hook", "Grabs in the first second", f"first cut {fc}s, opening sound {a0} dB" if fc is not None else "no cut found", "cut ≤ 1.5 s, sound from frame 1", _st(fc is not None and fc <= 1.5 and (a0 is None or a0 > -40), fc is not None and fc <= 2.5), "Open on your best play with the music already hitting.")
        d = m["drop"]
        add("drop", "A cut on the drop", "no drop found" if d is None else f"drop at {d['t']}s, nearest cut {d['gap_ms']} ms away" if d["gap_ms"] is not None else "no cuts", "cut within 100 ms of the biggest beat drop",
            _st(d is None or (d["gap_ms"] is not None and d["gap_ms"] <= 100), d is not None and d["gap_ms"] is not None and d["gap_ms"] <= 250), "Save your best moment for the drop and cut on it exactly.")
        f, l = m["cut_rate_first_third"], m["cut_rate_last_third"]
        add("build", "Energy builds", f"{f} cuts/s first third → {l} last third", "last third ≥ first third", _st(l >= f, l >= f * 0.8), "Let the cuts speed up towards the end so it feels like it is building.")
    else:
        a = m["asl"]; fc = m["first_cut"]
        add("hook", "Grabs in the first 2 seconds", f"first cut / change at {fc}s" if fc is not None else "no change in the opening", "a cut, title or zoom by 2 s", _st(fc is not None and fc <= 2.0, fc is not None and fc <= 3.5), "Start on the best line or a title that flies in, not a long quiet intro.")
        add("pacing", "Something changes every few seconds", None if a is None else f"{a}s average shot, longest {m['max_shot']}s", "average ≤ 6 s, none over 12 s", "fail" if a is None else _st(a <= 6 and m["max_shot"] <= 12, a <= 9 and m["max_shot"] <= 18), "Add a punch-in, freeze frame or caption pop where a shot runs long.")
        c = m.get("captions")
        add("captions", "Captions readable", "no project file given" if not c else f"appear {c['lead']}s early, stay {c['tail']}s, at least {c['min_on_screen']}s", "lead ≥ 0.15, min ≥ 1.0 s", "warn" if not c else _st(c["lead"] >= 0.15 and c["min_on_screen"] >= 1.0, c["min_on_screen"] >= 0.8), "Raise Min. on screen and Appear early in the Captions panel.")
    lu, tp = lo["lufs"], lo["true_peak"]
    add("loudness", "Loud enough, not clipping", None if lu is None else f"{lu} LUFS, peak {tp} dBTP", "−16 … −11 LUFS, peak ≤ −1 dBTP", "fail" if lu is None else _st(-16 <= lu <= -11 and (tp is None or tp <= -0.9), -18 <= lu <= -9 and (tp is None or tp <= 0.0)), "Loudness normalise to −14 LUFS with a −1 dB limiter; lower the music under speech.")
    long_sil = [x for x in m["silences"] if (x["end"] is None or x["end"] - x["start"] >= (0.4 if kind == "highlight" else 1.2))]
    add("dead_air", "No dead air", f"{len(long_sil)} silent gaps" if m["info"]["has_audio"] else "no audio", "none", "fail" if not m["info"]["has_audio"] else _st(not long_sil, len(long_sil) <= 1), "Fill the gap with music or a sound effect, or cut it out.")
    if kind != "highlight":
        bl = [b for b in m["black"] if b["start"] > 0.3 and b["end"] < m["info"]["duration"] - 0.3]
        add("black", "No black frames", f"{len(bl)} black moments", "none", _st(not bl, len(bl) <= 1), "A black frame usually means a bad cut or a missing clip.")
    ed = m.get("ending_drop_db")
    if kind == "highlight":
        add("ending", "Clean ending", "no audio" if ed is None else f"final level {ed:+.0f} dB vs body", "fade out ≥ 8 dB or end on a beat", "warn" if ed is None else _st(ed <= -8, ed <= -4), "Fade the music out over the last half second, or end on the last beat with a hit.")
    else:
        ed = m.get("ending_drop_db")
        add("ending", "Clean ending", "no audio" if ed is None else f"final level {ed:+.0f} dB vs body", "fade ≥ 4 dB", "warn" if ed is None else _st(ed <= -4, ed <= -1), "End with the outro card and a short fade.")
    okfmt = info["height"] >= 1.7 * info["width"] and info["height"] >= 1920 and info["fps"] >= 29
    if Path(m["file"]).name.startswith("preview") and info["height"] >= 1.7 * info["width"]:                # a fast draft is small on purpose
        add("format", "Vertical, full quality", f"{info['width']}x{info['height']} draft preview (the final renders at 1080x1920)", "9:16", "pass", "")
    else:
      add("format", "Vertical, full quality", f"{info['width']}x{info['height']} @ {info['fps']} fps", "9:16, ≥ 1080x1920, ≥ 30 fps", _st(okfmt, info["height"] >= 1.7 * info["width"]), "Export 1080x1920 at 30 fps.")
    d0 = info["duration"]; lo_, hi_ = (12, 35) if kind == "highlight" else (30, 180)
    add("length", "Right length", f"{d0:.1f}s", f"{lo_}–{hi_} s", _st(lo_ <= d0 <= hi_, lo_ * 0.7 <= d0 <= hi_ * 1.3), "Highlights work best at 15–30 s; longer clips lose viewers before the end." if kind == "highlight" else "Keep mic'd-up reels between 45 s and 2.5 min.")
    w = RULES_W[kind]; tot = sum(w.get(x["id"], 0) for x in r) or 1
    score = 100 * sum(w.get(x["id"], 0) * {"pass": 1, "warn": 0.5, "fail": 0}[x["status"]] for x in r) / tot
    return r, round(score)


def profile(video, out=None, kind="highlight"):
    m = analyze(video, kind=kind, sheet=False)
    prof = dict(source=str(video), kind=kind, info=m["info"], tempo=m["beat"]["tempo"], n_cuts=m["n_cuts"], asl=m["asl"], median_shot=m["median_shot"], max_shot=m["max_shot"], first_cut=m["first_cut"],
                cuts_per_beat=m["cuts_per_beat"], sync={k: v for k, v in m["sync"].items() if k != "per_cut"}, drop=m["drop"], loudness=m["loudness"], visual=m["visual"],
                cut_rate_first_third=m["cut_rate_first_third"], cut_rate_last_third=m["cut_rate_last_third"], ending_drop_db=m.get("ending_drop_db"), short_shots_pct=m["short_shots_pct"])
    if out:
        Path(out).write_text(json.dumps(prof, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    return prof


def report_text(m):
    icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"QA score {m['score']}/100  ({KINDS.get(m['kind'], m['kind'])}, {m['info']['duration']:.1f}s, {m['n_cuts']} cuts, {m['beat']['tempo']} BPM)"]
    for x in m["rules"]:
        lines.append(f"  {icon[x['status']]}  {x['title']}: {x['value']}   (target {x['target']})")
        if x["status"] != "pass":
            lines.append(f"        -> {x['advice']}")
    if m.get("fixes"):
        lines.append(f"  {len(m['fixes'])} cuts are off the beat. Nearest-beat corrections: " + ", ".join(f"{f['cut']}s {f['shift_ms']:+d}ms" for f in m["fixes"][:12]))
    if m.get("sheet"):
        lines.append(f"  Contact sheet: {m['sheet']}  (green = cut on the beat, red = off the beat. Look at it: is the subject in frame, is anything cut off, do the shots flow?)")
    return "\n".join(lines)


def main(a):
    if len(a) < 2 or a[0] in ("-h", "--help"):
        print(__doc__); return 0
    cmd, target = a[0], a[1]; opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    if cmd == "analyze":
        ref = json.loads(Path(opt("--ref")).read_text()) if opt("--ref") and Path(opt("--ref")).exists() else None
        bnd = json.loads(Path(opt("--boundaries")).read_text()) if opt("--boundaries") and Path(opt("--boundaries")).exists() else None
        m = analyze(target, kind=opt("--kind", "highlight"), project=opt("--project"), ref=ref, out_dir=opt("--out"), music=opt("--music"), boundaries=bnd)
        print(report_text(m)); return 0
    if cmd == "profile":
        p = profile(target, opt("--out"), opt("--kind", "highlight")); print(json.dumps(p, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))); return 0
    if cmd == "beats":
        b = beat_info(target); print(json.dumps(dict(tempo=b["tempo"], beats=b["beats"]))); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
