#!/usr/bin/env python3
"""ZBA coaching reel builder. Lives in edit/, never inside the video-use repo (Hard Rule 12).

Pipeline (video-use hard rules): per-segment extract -> lossless -c copy concat (2),
30ms audio fades at every boundary (3), overlays baked with frame 0 == segment start (4),
captions burned LAST in each segment's filter chain (1), word-boundary cuts with padding (6,7).

  python build.py --list                  # print snapped cut ranges for review
  python build.py --fz-preview            # contact sheet of the annotated freeze frames
  python build.py [--draft] [--only a,b]  # build (draft = 540x960 ultrafast)
"""
from __future__ import annotations
import hashlib, json, math, os, re, subprocess, sys, shutil, wave
from collections import Counter
from functools import lru_cache
from multiprocessing import Pool
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = Path(__file__).resolve().parent                       # .../HYL Studio/app
STUDIO_ROOT = HERE.parent
PDIR = Path(os.environ["ZBA_PDIR"]).resolve()                # one project folder under library/
META = json.loads((PDIR / "meta.json").read_text())
SRC = Path(META["source"]) if os.path.isabs(META["source"]) else (PDIR / META["source"])
LOGO = STUDIO_ROOT / "brand" / "logo.png"
DRAFT = "--draft" in sys.argv
WORK = PDIR / ("work_draft" if DRAFT else "work")
OUT_DIR = PDIR / "out"
# W, H, FPS are set further down, once SETTINGS (export_w/export_h/export_fps) is loaded from project.json.
IMPACT = "/System/Library/Fonts/Supplemental/Impact.ttf"
FONTS_DIR = "/System/Library/Fonts/Supplemental"

NAVY, DARK, BLUE = (32, 40, 96), (20, 26, 64), (64, 88, 144)
GOLD, WHITE, RED = (248, 200, 128), (255, 255, 255), (233, 77, 82)
STUDIO_GRADE = "eq=contrast=1.05:saturation=1.10,curves=master='0/0 0.25/0.235 0.75/0.765 1/1'"
GRADE = STUDIO_GRADE     # replaced below once settings are loaded

_tp = PDIR / "transcripts" / f"{SRC.stem}.json"
TRANSCRIPT = json.loads(_tp.read_text()) if _tp.exists() else {"words": []}
WORDS = [w for w in TRANSCRIPT["words"] if w.get("type") == "word" and w.get("start") is not None]


# ------------------------------------------------------------------ project (edit decisions live in project.json)
PROJECT_PATH = Path(os.environ.get("ZBA_PROJECT") or (PDIR / "project.json"))
PROJECT = json.loads(PROJECT_PATH.read_text())
BUILD_REV = "26"          # bump when rendering code changes; forces every segment to re-render
FORCE = "--force" in sys.argv


def hexc(h):
    h = h.lstrip("#"); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


SEGS = PROJECT["segments"]
DEFAULT_SETTINGS = dict(caption_size=92, caption_margin_v=540, caption_words=0, caption_case="upper", caption_highlight="#f8c880",
                        caption_outline="#202860", grade="studio", audio_highpass=90, audio_denoise=False, loudness=-14.0,
                        audio_voice="off", audio_denoise_nr=10, caption_intense=False,
                        cut_pad_pre=0.07, cut_pad_post=0.12, transition="none", transition_dur=0.35,
                        caption_lead=0.22, caption_tail=0.6, caption_min=1.1,
                        export_w=1080, export_h=1920, export_fps=30)
SETTINGS = {**DEFAULT_SETTINGS, **PROJECT.get("settings", {})}     # normalized, so saved defaults never invalidate the cache
# export quality is per-project (default matches every already-delivered video exactly, so nothing changes unasked).
# W/H must be set before anything below computes a canvas-relative default (fonts, drawing helpers); FPS must be set
# before any duration is turned into a frame count — so this has to happen right after SETTINGS, not at import time.
W, H, FPS = int(SETTINGS.get("export_w", 1080)), int(SETTINGS.get("export_h", 1920)), int(SETTINGS.get("export_fps", 30))
# freeze-frame annotations (pts/stamp) are always authored on a fixed 1080x1920 reference canvas — the browser's
# annotation editor scales screen pixels by stageWidth/1080 regardless of the project's own export size, so the
# numbers stored in project.json never know about export_w/export_h. This scales that fixed reference into
# whatever the current export resolution actually is, the same way the ASS caption track's own fixed PlayResX/Y
# already gets scaled by the output frame size.
ANNO_REF_W, ANNO_REF_H = 1080.0, 1920.0
ANNO_SCALE = W / ANNO_REF_W
sys.path.insert(0, str(Path.home() / "Developer" / "video-use" / "helpers"))
try:
    from grade import PRESETS as GRADE_PRESETS      # video-use's own presets
except Exception:                                    # noqa: BLE001
    GRADE_PRESETS = {}


def resolve_grade(name):
    if name in (None, "", "studio"):
        return STUDIO_GRADE
    return GRADE_PRESETS.get(name) or "null"        # "none"/empty preset -> no-op filter


GRADE = resolve_grade(SETTINGS.get("grade"))
_mj = PDIR / "media.json"
MEDIA = json.loads(_mj.read_text()) if _mj.exists() else {}          # id -> {name, file, kind, duration, has_audio}
AUDIO = PROJECT.get("audio") or {}                                  # {"tracks": [...], "duck": "medium"}
CAPTION_BREAKS = {int(x) for x in (PROJECT.get("caption_breaks") or [])}       # a new caption starts at the word that begins at this many ms
CAPTION_JOINS = {int(x) for x in (PROJECT.get("caption_joins") or [])}         # the word that begins at this many ms stays in the previous caption
CAPTION_TIMING = PROJECT.get("caption_timing") or {}                         # {"<first word ms>": {"lead": s, "tail": s}}: how early a caption appears / how long it stays
CAPTION_EDITS = PROJECT.get("caption_edits") or {}                  # {"<word start ms>": "replacement text" ("" hides the word)}
CAPTION_TRANSLATE = PROJECT.get("caption_translate") or {}          # {"<word start ms>": "English gloss"}: shown as a smaller line ABOVE that caption card, for a foreign-language moment
CJK_RE = re.compile(r"[一-鿿]")


def media_path(mid):
    if mid in (None, "", "main"):
        return SRC
    f = MEDIA[mid]["file"]
    return Path(f) if os.path.isabs(f) else PDIR / f


def media_has_audio(mid):
    return True if mid in (None, "", "main") else bool(MEDIA.get(mid, {}).get("has_audio", True))
HOOK = PROJECT["hook"]
OUTRO = PROJECT["outro"]
ANN = {}
for _k, _a in PROJECT["annotations"].items():
    _a = json.loads(json.dumps(_a))
    for _p in _a["pts"]:
        _p["color"] = hexc(_p["color"])
    if _a.get("stamp"):
        _a["stamp"]["color"] = hexc(_a["stamp"]["color"])
    ANN[_k] = _a

# ------------------------------------------------------------------ helpers


def run(cmd, cwd=None):
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(str(c) for c in cmd[:8])} ...\n{r.stderr[-1500:]}")
    return r


def ff(*args, cwd=None):
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd)


@lru_cache(None)
def font(size):
    return ImageFont.truetype(IMPACT, size)


def ease_out(t): t = min(max(t, 0.0), 1.0); return 1 - (1 - t) ** 3
def ease_in(t): t = min(max(t, 0.0), 1.0); return t ** 3
def ease_back(t):
    t = min(max(t, 0.0), 1.0); c1 = 1.70158; c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def near_start(t): return min(WORDS, key=lambda w: abs(w["start"] - t))
def near_end(t): return min(WORDS, key=lambda w: abs(w["end"] - t))


def fz_hold_frames(f):
    """How long a freeze frame HOLDS, in frames. This is inserted, extra time (Rule: a freeze pauses the
    video, it never eats into the footage after it) — never a source range that gets skipped over."""
    ann = ANN[f["ann"]]
    if f.get("dur") is not None:
        dur = float(f["dur"])
    elif f.get("end") is not None:                                      # legacy field from before freezes could hold independently:
        dur = float(f["end"]) - ann["t"]                                 # `end` used to be an absolute source time; read as a duration instead
    else:
        last_t0 = max((float(p.get("t0", 0)) for p in ann["pts"]), default=0.0)
        dur = last_t0 + 0.85                                             # long enough for every callout to finish its reveal (~0.62s) + a beat to read it
    return max(1, round(max(0.2, dur) * FPS))


def _fz_warp(a1, fzs):
    """A freeze frame inserts held time at its point without skipping any source footage. Build a function
    mapping a source-relative second (from a1) to the matching OUTPUT-relative second, i.e. shift anything
    at or after each freeze point later by that freeze's hold duration (cumulative for several freezes)."""
    marks = sorted((ANN[f["ann"]]["t"] - a1, fz_hold_frames(f) / FPS) for f in (fzs or []))
    if not marks:
        return lambda t: t
    return lambda t: t + sum(hold for pos, hold in marks if pos <= t)


def cut_range(a, b, pre=None, post=None):
    pre = float(SETTINGS.get("cut_pad_pre", 0.07)) if pre is None else pre          # lead-in before the first word
    post = float(SETTINGS.get("cut_pad_post", 0.12)) if post is None else post      # reaction time after the last word
    """Snap [a,b] to word boundaries, pad both edges without touching neighbours.
    Returns (start_seconds, n_frames). Working padding window is 30-200ms (Hard Rule 7)."""
    ws, we = near_start(a), near_end(b)
    a0, b0 = ws["start"], we["end"]
    prev = [w["end"] for w in WORDS if w["end"] <= a0 - 0.001]
    nxt = [w["start"] for w in WORDS if w["start"] >= b0 + 0.001]
    a1 = min(max(a0 - pre, (max(prev) + 0.01) if prev else 0.0), a0 - 0.005)
    b1 = max(min(b0 + post, (min(nxt) - 0.01) if nxt else b0 + post), b0 + 0.005)
    n = max(1, round((b1 - a1) * FPS))
    return a1, n


def fmt_ass(t):
    t = max(t, 0.0); cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


# ------------------------------------------------------------------ captions (ASS, karaoke)

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,Impact,92,&H0080C8F8,&H00FFFFFF,&H00602820,&H00000000,0,0,0,0,100,100,1,0,1,9,0,2,60,60,540,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def clean_word(text):
    w = re.sub(r"[,;:.]+$", "", text.strip())
    mode = SETTINGS.get("caption_case", "upper")
    return w.upper() if mode == "upper" else (w.title() if mode == "title" else w)


def clean_word_ml(text):
    """Like clean_word, but a manual line break the editor stored as a real newline character (a leading one
    means "start a new line right before this word"; one in the middle means the extra typed-in words after it
    start a new line) becomes the ASS line-break marker, case-normalised on each side of it."""
    ass_nl = chr(92) + "N"
    return ass_nl.join(clean_word(p) for p in text.split("\n"))


def ass_translation_prefix(ch):
    """If this caption card carries a `caption_translate` entry (a foreign-language moment someone gave an
    English gloss for), render that gloss as a smaller line ABOVE the main text. Never invents a translation —
    CAPTION_TRANSLATE is user-authored, not machine-guessed."""
    translation = next((CAPTION_TRANSLATE[k] for w in ch if (k := str(_wms(w))) in CAPTION_TRANSLATE), None)
    if not translation:
        return ""
    safe = str(translation).replace("{", "").replace("}", "").replace("\n", " ")
    return f"{{\\fscx72\\fscy72\\1c&HE6E6E6&\\3c&H202860&\\bord3\\fad(50,0)}}{safe}\\N{{\\r}}"


def ass_bgr(hexstr):
    r, g, b = hexc(hexstr); return f"&H00{b:02X}{g:02X}{r:02X}"


def _wms(w):
    return int(round(w["start"] * 1000))


def _ct(first_ms):
    t = CAPTION_TIMING.get(str(first_ms)) or {}
    return (t.get("lead"), t.get("tail"))


def apply_edits(ws):
    # a hidden word (edited to "") keeps its place in the list with empty text, instead of being removed —
    # removing it would also throw away its real timing, so a caption whose TRAILING word got hidden would
    # silently lose how long that speech actually lasted (this matches the editor, which does the same: a
    # hidden word stays in the caption's word list, just with nothing to show, so "last word" means the same
    # thing on both sides and a duration you set by eye in the editor renders as the same duration)
    out = []
    for w in ws:
        k = str(int(round(w["start"] * 1000)))
        w = dict(w, text=CAPTION_EDITS[k]) if k in CAPTION_EDITS else w
        out.append(w)
    return out



STOP_WORDS = {"the", "a", "is", "i", "to", "and", "so", "uh", "um", "it", "of", "in"}


def _tok(t):
    return re.sub(r"[^a-z0-9']", "", t.lower())


def _runs(ws, gap=1.0):
    """Phrases the coach repeats back to back ("get back, get back, get back"): list of (start index, phrase length, repeats up to 4).
    Looks a couple of words ahead so "take the ball, hold the ball x3" locks onto "hold the ball", not "the ball hold"."""
    toks = [_tok(w["text"]) for w in ws]; n = len(ws); out = []; i = 0

    def at(st):
        best = None
        for g in (3, 2, 1):
            seq = toks[st:st + g]
            if len(seq) < g or not all(seq) or (g == 1 and seq[0] in STOP_WORDS):
                continue
            reps, j = 1, st + g
            while j + g <= n and toks[j:j + g] == seq and ws[j]["start"] - ws[j - 1]["end"] < gap:
                reps += 1; j += g
            if reps >= 2 and (best is None or reps * g > best[0] * best[1]):
                best = (reps, g)
        return best
    while i < n:
        cands = [(st, at(st)) for st in range(i, min(n, i + 4))]
        cands = [(st, b) for st, b in cands if b]
        if not cands:
            i += 1; continue
        end0 = cands[0][0] + cands[0][1][0] * cands[0][1][1]             # only compare runs that overlap the earliest one
        cands = [c for c in cands if c[0] < end0]
        pun = lambda k: k < 0 or ws[k]["text"].strip()[-1:] in ".,?!"
        st, (reps, g) = max(cands, key=lambda c: (c[1][0] * c[1][1], pun(c[0] - 1), pun(c[0] + c[1][0] * c[1][1] - 1), toks[c[0]] not in STOP_WORDS, -c[0]))
        # prefer the run that begins right after punctuation and ends on punctuation ("hold the ball," x3), then one that starts on a real word
        reps = min(reps, 4); out.append((st, g, reps)); i = st + g * reps
    return out


def _loudness_db(ws, a1, n):
    """How much louder than his usual level each word is (dB), measured from the source audio."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a1:.3f}", "-t", f"{n / FPS:.3f}", "-i", str(SRC), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    db = []
    for w in ws:
        st = max(0, int((w["start"] - a1) * 16000)); en = max(int((w["end"] - a1) * 16000), st + 1280); seg = x[st:en]
        db.append(20 * np.log10(np.sqrt(float((seg ** 2).mean())) + 1e-5) if seg.size else -80.0)
    med = float(np.median(db)) if db else 0.0
    return [d - med for d in db]


def _hot(rank, size):
    if rank >= 2 or size >= 175: return "#ff4d3d"          # red-hot on the 3rd repeat or a very big word
    if rank == 1 or size >= 125: return "#ff9a3c"          # orange
    return SETTINGS.get("caption_highlight", "#f8c880")     # gold


def build_intense_ass(a1, n, path, fzs=None):
    """Kinetic captions: each word pops in as it is spoken. A phrase he repeats becomes a pyramid, one line per repeat, each bigger and
    hotter than the last (gold, orange, red). Words he shouts louder than his usual level are also bigger."""
    warp = _fz_warp(a1, fzs)
    b1 = a1 + n / FPS
    ws = [w for w in WORDS if w["start"] >= a1 - 0.01 and w["end"] <= b1 + 0.01]
    # a sentence's natural end is a property of what was actually SAID, not of what you later edited a word's
    # display text to — so this is captured from the words as spoken, before apply_edits can blank one of them out.
    # Without it, editing the trailing word of a sentence to "" (hiding a bad ASR tail, or collapsing a repeat into
    # "x2"/"x3") removes the only signal that told the chunker a new caption should start after it, and the next,
    # unrelated sentence silently merges into the same card.
    # an ASR trailing-off marker ("Right... right hand.") ends in "." too, but it isn't a real sentence end —
    # only count a word that actually has letters/digits in it, or a self-interruption/pause gets mistaken
    # for two swallowed sentences. Two different words can also share the exact same start ms (ASR sometimes
    # timestamps a rapid pair identically) — a plain word one of those ms belongs to would otherwise look up
    # the OTHER word's punctuation and be treated as a sentence end it isn't, so a colliding ms is dropped
    # entirely rather than guessed at.
    _start_counts = Counter(_wms(w) for w in ws)
    RAW_SENT_END = {_wms(w) for w in ws if _start_counts[_wms(w)] == 1 and (_t := w["text"].strip()) and _t[-1] in ".?!" and re.search(r"[A-Za-z0-9]", _t)}
    ws = apply_edits(ws)
    ws = [w for w in ws if clean_word(w["text"]) not in {"UH", "UM", "AH", "EH", "ERM"}]
    ws = [w for i, w in enumerate(ws) if not (i + 1 < len(ws) and _tok(w["text"]) in STOP_WORDS and _tok(w["text"]) == _tok(ws[i + 1]["text"]))]
    # a word typed as several words in one slot (cramming extra text into one original ASR word, then hiding the
    # rest) used to render as one oversized "word" here — its FULL crammed string sized as a single token threw off
    # both the line-wrap width math and the per-word pop-in, so it looked like a wall of text that just appeared
    # with no animation and the wrong size/position. build_ass already splits this back into its own words for its
    # highlight sweep; same fix here, splitting the slot's time budget evenly so each piece still pops in on its
    # own beat and sizes/wraps correctly.
    ws2 = []
    for w in ws:
        toks = str(w["text"]).split()
        if len(toks) <= 1:
            ws2.append(w); continue
        dur = max(0.04, w["end"] - w["start"]); share = dur / len(toks)
        for j, tok in enumerate(toks):
            nw = dict(w, text=tok, start=w["start"] + j * share, end=w["end"] if j == len(toks) - 1 else w["start"] + (j + 1) * share)
            if j > 0:
                nw["_split"] = True     # a synthesized piece of a crammed edit, not a real transcript slot — see the swallow-guard below
            ws2.append(nw)
    ws = ws2
    if not ws:
        Path(path).write_text(ASS_HEAD + "\n"); return 0
    runs = {i: (g, reps) for i, g, reps in _runs(ws)}
    loud = _loudness_db(ws, a1, n)
    base = int(SETTINGS.get("caption_size", 92))
    lb = lambda i: min(max(loud[i] / 10.0, 0.0), 0.4)                       # loudness bonus 0..40 %
    chunks, cur, i = [], [], 0
    _wc = (PROJECT.get("settings") or {}).get("caption_words")            # an explicit word count still ends the CARD there; otherwise a card runs until a real
    MAXW = int(_wc) if (_wc is not None and int(_wc) > 0) else 20         # pause/punctuation ends it, with this only as a generous backstop

    def flush():                                                          # wrap long text across up to 3 same-size lines instead of cutting it into more, shorter-lived cards
        if not cur:
            return
        ls, cw2, seg = [], 0.0, []
        for idx in cur:
            wd = len(clean_word(ws[idx]["text"])) * 0.5 * base * (100 + lb(idx) * 100) / 100 + 0.3 * base
            if seg and cw2 + wd > 930 and len(ls) < 2:
                ls.append(seg); seg, cw2 = [], 0.0
            seg.append(idx); cw2 += wd
        ls.append(seg)
        chunks.append(dict(lines=ls, rank=[0] * len(ls)))

    def _lines_needed(idxs):                                              # same greedy width math as flush(), just counting lines instead of building them —
        cw2, n = 0.0, 1                                                   # used to break BEFORE a caption would grow into a 3rd (hard-to-read) line
        for idx in idxs:
            wd = len(clean_word(ws[idx]["text"])) * 0.5 * base * (100 + lb(idx) * 100) / 100 + 0.3 * base
            if cw2 and cw2 + wd > 930:
                n += 1; cw2 = 0.0
            cw2 += wd
        return n
    while i < len(ws):
        if i in runs:
            if cur: flush(); cur = []
            g, reps = runs[i]
            chunks.append(dict(lines=[list(range(i + r * g, i + (r + 1) * g)) for r in range(reps)], rank=list(range(reps)), pyramid=True))
            i += g * reps; continue
        w = ws[i]; wk = _wms(w)
        # a long unbroken explanation (no punctuation, no real pause) chunked purely by pause/punctuation can grow
        # past what fits on 2 lines before it hits one — reading a 3-line kinetic caption is what this guards
        # against, breaking right before the word that would force a 3rd line rather than waiting for a pause
        # that might be a while coming.
        wraps3 = cur and _lines_needed(cur + [i]) > 2
        if cur and (wk in CAPTION_BREAKS or wraps3 or (wk not in CAPTION_JOINS and (w["start"] - ws[cur[-1]]["end"] > 0.6 or len(cur) >= MAXW))):
            flush(); cur = []
        cur.append(i)
        # the raw-sentence-end fallback only stands in for a word that was HIDDEN (edited to nothing) — one edited
        # to REPLACEMENT text is the user authoring new content on purpose, and its own (lack of) punctuation is
        # their real signal; falling back to what the ORIGINAL word happened to end with there split deliberate
        # multi-word reconstructions ("RenZhe that's a good pass", built by editing several ASR slots into one new
        # sentence) into two overlapping cards, which is worse than the bug this fallback was added to fix.
        punct_end = (w["text"].strip() and w["text"].strip()[-1] in ".?!") or (w["text"] == "" and wk in RAW_SENT_END)
        if punct_end and (_wms(ws[i + 1]) if i + 1 < len(ws) else None) not in CAPTION_JOINS:
            flush(); cur = []
        i += 1
    if cur:
        flush()
    # sanity check, not a hard failure: if a card contains a real sentence-ending word that ISN'T its last word,
    # AND at least one word after it is still the original, untouched transcript text, a later sentence almost
    # certainly got swallowed into this card by accident (the exact failure this file was rewritten to prevent —
    # see RAW_SENT_END above). Three things deliberately do NOT count as a swallow, and are excluded so this stays
    # quiet in the normal case: a repeat pyramid (each repeat naturally ends its own "sentence" by design), a
    # card where every later word was hand-edited too (you rebuilt one sentence out of several ASR slots on
    # purpose — a leftover raw period in the middle of that isn't a mistake), and a synthesized piece of a crammed
    # multi-word edit (`_split`) — it has no CAPTION_EDITS key of its own to match, but it's still edited text by
    # construction. Printed so it shows up in the render log instead of only being noticed after the video is posted.
    for ch in chunks:
        if ch.get("pyramid"):
            continue
        flat = [idx for l in ch["lines"] for idx in l]
        for pos, idx in enumerate(flat[:-1]):
            later = flat[pos + 1:]
            if _wms(ws[idx]) in RAW_SENT_END and any(str(_wms(ws[j])) not in CAPTION_EDITS and not ws[j].get("_split") for j in later):
                print(f"WARN cap: card starting {ws[flat[0]]['start']:.2f}s may have swallowed a later sentence "
                      f"(sentence-end at {ws[idx]['start']:.2f}s isn't the card's last word, and unedited words follow it) "
                      f"— double-check it", file=sys.stderr)
                break
    lines = []
    LEAD, TAIL, HOLD = _cap_timing()
    sts = []
    leads = []
    for ch in chunks:
        s0 = ws[ch["lines"][0][0]]["start"]; ov = _ct(_wms(ws[ch["lines"][0][0]])); ld = LEAD if ov[0] is None else float(ov[0]); leads.append(ld); st = max(a1, s0 - ld)
        sts.append(max(st, min(sts[-1] + HOLD, s0 - 0.02)) if sts and ov[0] is None else st)
    for ci, ch in enumerate(chunks):
        first = ch["lines"][0][0]; last = ch["lines"][-1][-1]; t0 = sts[ci]; t0o = warp(t0 - a1)             # words appear a beat before he says them
        nxt = warp(sts[ci + 1] - a1) if ci + 1 < len(chunks) else 1e9
        nwords = sum(len(l) for l in ch["lines"]); span = ws[last]["start"] - ws[first]["start"]
        burst = nwords >= 3 and len(ch["lines"]) == 1 and span / (nwords - 1) < 0.16       # "go and sit down" shouted in half a second: show it as one card
        # a repeat pyramid ("don't take, don't take, don't take") isn't read top-to-bottom like a 9-word sentence —
        # it's read once and recognised as a repeat, so its hold time is based on ONE repeat's word count, not all of them
        read_words = len(ch["lines"][0]) if ch.get("pyramid") else nwords
        tov = _ct(_wms(ws[first]))[1]
        # a repeat said in a rapid-fire burst ("get back, get back, get back" barked in under a second) has its
        # ASR word timestamps just as compressed. Delaying a tight repeat's pop-in to avoid a collision (an earlier
        # version of this did that) fixes the flicker but throws the pop out of sync with when it's ACTUALLY
        # spoken — swapping "looks janky" for "doesn't match the audio", not actually fixing it. Instead, each
        # word keeps its real, spoken pop-in moment, and only its OWN animation length adapts: full and dramatic
        # when there's room before the next word, a quick snap when the coach barely paused. Computed per WORD,
        # not per line, so two fast words inside the very same repeat ("Get" "back,") still keep pace too.
        pyramid_anim_ms = {}
        if ch.get("pyramid"):
            flat_idx = [i for l in ch["lines"] for i in l]
            natural = {i: max(0, int((ws[i]["start"] - leads[ci] - t0) * 1000)) for i in flat_idx}
            for pos, i in enumerate(flat_idx):
                nxt_ms = natural[flat_idx[pos + 1]] if pos + 1 < len(flat_idx) else natural[i] + 260
                pyramid_anim_ms[i] = max(40, min(200, nxt_ms - natural[i] - 15))
        # the last word's own pop-in plays out over ~200ms starting when IT is spoken, not when the chunk began —
        # a word whose recorded duration is tiny or zero (a bad ASR timestamp, or the final word of a run whose
        # real length got collapsed) would otherwise have its card end before that animation is even visible.
        # Used to demand a fixed 0.35s no matter what, then let `end` overshoot into the next caption's start to
        # make room for it — fine when rare, but a forced word-count chunk boundary (no real pause under it) can
        # land the last word right up against the next caption's own start, and this fired on nearly EVERY such
        # boundary, not just the odd one. Same fix already proven for repeat pyramids (`pyramid_anim_ms` below):
        # shrink the animation itself to fit the real gap instead of pushing the card's end past it.
        last_ms = max(0, int((ws[last]["start"] - leads[ci] - t0) * 1000))
        nxt_ms_rel = int(round((nxt - t0o) * 1000)) if nxt < 1e9 else last_ms + 260
        last_word_ad = max(40, min(200, nxt_ms_rel - last_ms - 15))
        last_pop_s = max(0.0, warp(ws[last]["start"] - a1) - t0o)
        anim_floor = t0o + last_pop_s + last_word_ad / 1000.0 + 0.05
        # a short caption (as few as one word — "Hopping.") wants at least caption_min on screen, same as any
        # other — this is the TARGET a caption's hold time reaches for.
        reading_floor = t0o + _reading_hold(read_words, HOLD) + (0.15 if burst else 0.0)
        # every caption — automatic OR an explicit caption_timing override — is now hard-clamped to end strictly
        # before the next one starts. This used to let a caption push a little past `nxt` (up to 0.15s) when
        # `anim_floor`/`reading_floor` couldn't otherwise be met, on the reasoning that a brief overlap reads
        # better than an invisible word or a too-short hold. In practice any non-zero overlap — even that small,
        # deliberately-capped amount — read as "captions still overlapping" to a real viewer, repeatedly, no
        # matter how the cap was tuned. So there is no overshoot allowance left at all: `anim_floor`/`reading_floor`
        # still pull a caption's target hold time UP as high as they can, they just can never push it PAST `nxt`
        # any more. A caption that can't fully reach its ideal hold time (or whose last word's pop-in animation
        # doesn't have room to fully play) settles for what fits instead of ever touching the next card — losing
        # a little polish on a rare tight caption is a smaller cost than any overlap, ever.
        auto_end = max(warp(ws[last]["end"] - a1) + TAIL + (0.15 if len(ch["lines"]) > 1 else 0.0), reading_floor, anim_floor)
        end = (min(auto_end, nxt - 0.02) if tov is None
               else min(max(warp(ws[last]["end"] - a1) + float(tov), t0o + 0.3), nxt - 0.02))
        rows = []
        for li, idxs in enumerate(ch["lines"]):
            r = ch["rank"][li]; pyramid = ch.get("pyramid", False)
            # a phrase he repeats stacks up like tossed-down stickers: each repeat pops in from smaller, overshoots
            # a bit harder, and leans its own way (alternating, a little more each time), on top of already being
            # bigger and hotter-coloured than the last (Rule: this only kicks in for actual repeats — a long line
            # that just wrapped onto more than one line of equal size is NOT a pyramid and stays unchanged)
            tilt = ((-1) ** r) * (3 + r * 2.5) if pyramid else 0
            pop_from, overshoot = (0.32, 1.24 + 0.035 * r) if pyramid else (0.6, 1.14)
            sizes = [min(215.0, (100 + 38 * r if pyramid else 100) + lb(i) * (30 if pyramid else 100)) for i in idxs]
            wdt = sum(len(clean_word(ws[i]["text"])) * 0.5 * base * sz / 100 for i, sz in zip(idxs, sizes)) + 0.3 * base * len(idxs)
            k = min(1.0, 930 / wdt) if wdt else 1.0                              # shrink a line that would not fit the screen
            parts = []
            for i, sz in zip(idxs, sizes):
                S = sz * k; ms = (idxs.index(i) * 70) if burst else max(0, int((ws[i]["start"] - leads[ci] - t0) * 1000)); col = ass_bgr(_hot(r, S))
                rot = f"\\frz{tilt:.1f}" if tilt else ""
                ad = pyramid_anim_ms.get(i, last_word_ad if i == last else 200)  # this word's own pop-in/settle length — shorter only if the next word (or next caption, for the last word) is due very soon
                ov_end = round(ad * 0.55)
                tags = (f"{{\\alpha&HFF&\\1c{col}\\bord{max(6.0, 9 * S / 100 * 0.9):.1f}{rot}\\fscx{S * pop_from:.0f}\\fscy{S * pop_from:.0f}"
                        f"\\t({ms},{ms + 1},\\alpha&H00&)\\t({ms},{ms + ov_end},\\fscx{S * overshoot:.0f}\\fscy{S * overshoot:.0f})\\t({ms + ov_end},{ms + ad},\\fscx{S:.0f}\\fscy{S:.0f})}}")
                parts.append(tags + clean_word_ml(ws[i]["text"]))
            rows.append(" ".join(parts))
        flat = [ws[idx] for l in ch["lines"] for idx in l]
        lines.append(f"Dialogue: 0,{fmt_ass(t0o)},{fmt_ass(end)},Cap,,0,0,0,,{ass_translation_prefix(flat)}{{\\fad(0,140)}}" + "\\N".join(rows))
    head = (ASS_HEAD.replace("Impact,92,", f"Impact,{base},")
            .replace(",60,60,540,1", f",60,60,{int(SETTINGS.get('caption_margin_v', 540))},1")
            .replace("&H00602820", ass_bgr(SETTINGS.get("caption_outline", "#202860"))))
    Path(path).write_text(head + "\n".join(lines) + "\n")
    return len(chunks)


def _reading_hold(n_words, floor):
    """A caption stays long enough to read: about 3.5 words a second, never below the minimum."""
    return max(floor, 0.45 + 0.28 * n_words)


def _cap_timing():
    """(seconds a caption appears before its first word, seconds it stays after its last word, minimum time on screen)."""
    return (float(SETTINGS.get("caption_lead", 0.22)), float(SETTINGS.get("caption_tail", 0.6)), float(SETTINGS.get("caption_min", 1.1)))


def build_ass(a1, n, path, shift=0.0, fzs=None):
    if SETTINGS.get("caption_intense"):
        return build_intense_ass(a1, n, path, fzs)
    """Word-level karaoke captions for the kept range, in segment-local time (Rule 5)."""
    warp = _fz_warp(a1, fzs)
    b1 = a1 + n / FPS
    ws = [w for w in WORDS if w["start"] >= a1 - 0.01 and w["end"] <= b1 + 0.01]
    ws = [w for w in ws if clean_word(w["text"]) not in {"UH", "UM", "AH", "EH", "ERM"}]
    ws = [w for i, w in enumerate(ws)                       # "is, is" / "the, the" stutters -> one word
          if not (i + 1 < len(ws) and len(clean_word(w["text"])) <= 3
                  and clean_word(w["text"]) == clean_word(ws[i + 1]["text"]))]
    # chunk boundaries come from the ORIGINAL spoken words/timing only, same as the editor — a caption you
    # edited or hid words in must still land in its own card with its own karaoke sweep, never silently
    # merge into a neighbour just because some of its words are now blank.
    chunks, cur = [], []
    for wi, w in enumerate(ws):
        k = _wms(w)
        if cur and (k in CAPTION_BREAKS or (k not in CAPTION_JOINS and w["start"] - cur[-1]["end"] > 0.6)):
            chunks.append(cur); cur = []
        cur.append(w)
        capn = int(SETTINGS.get("caption_words", 0))                    # words per caption; 0 (default) = break naturally (pause/punctuation), not a fixed count
        long_ = sum(len(x["text"]) for x in cur) > (5 * capn if capn > 0 else 46)   # a readable-line safety net — always on, even with no explicit word count
        hardcap = min(capn, 16) if capn > 0 else 10                     # backstop so a run of short words with no punctuation/pause still breaks at a sane length
        nk = _wms(ws[wi + 1]) if wi + 1 < len(ws) else None
        if ((w["text"].strip() and w["text"].strip()[-1] in ".?!") or len(cur) >= hardcap or long_) and nk not in CAPTION_JOINS:
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    # an ASR trailing-off marker ("Right... right hand.") ends in "." too, but it isn't a real sentence end —
    # only count a word that actually has letters/digits in it, or a self-interruption/pause gets mistaken
    # for two swallowed sentences. See build_intense_ass for why a colliding start ms is dropped entirely.
    _start_counts = Counter(_wms(w) for w in ws)
    RAW_SENT_END = {_wms(w) for w in ws if _start_counts[_wms(w)] == 1 and (_t := w["text"].strip()) and _t[-1] in ".?!" and re.search(r"[A-Za-z0-9]", _t)}
    chunks = [dws for dws in (apply_edits(ch) for ch in chunks) if any(w["text"] != "" for w in dws)]   # NOW swap in edited text; a chunk hidden entirely just doesn't render
    # regression guard, not a hard failure (see build_intense_ass for the full story — this file already chunks
    # on raw text so it shouldn't be able to trip this, but a future edit to the ordering above could reintroduce
    # it silently): warn if a card holds a real sentence end that isn't its last word AND an untouched word
    # follows it, which means a later sentence likely got swallowed in by accident.
    for ch in chunks:
        for pos, w in enumerate(ch[:-1]):
            later = ch[pos + 1:]
            if _wms(w) in RAW_SENT_END and any(str(_wms(x)) not in CAPTION_EDITS for x in later):
                print(f"WARN cap: card starting {ch[0]['start']:.2f}s may have swallowed a later sentence "
                      f"(sentence-end at {w['start']:.2f}s isn't the card's last word, and unedited words follow it) "
                      f"— double-check it", file=sys.stderr)
                break
    lines = []
    LEAD, TAIL, HOLD = _cap_timing()
    sts = []                                                           # each caption appears LEAD before its first word, but never cuts the previous one shorter than HOLD
    for ch in chunks:
        s0 = warp(ch[0]["start"] - a1); ov = _ct(_wms(ch[0])); st = max(0.0, s0 - (LEAD if ov[0] is None else float(ov[0])))
        sts.append(max(st, min(sts[-1] + HOLD, s0 - 0.02)) if sts and ov[0] is None else st)
    ANIM = min(500.0, max(20.0, float(SETTINGS.get("caption_anim", 90))))    # ms: how long each word's white -> highlight fade takes
    GOLD = ass_bgr(SETTINGS.get("caption_highlight", "#f8c880"))
    for i, ch in enumerate(chunks):                                    # readable: on screen before he says it, and it stays a moment after
        st = sts[i]; nxt = sts[i + 1] if i + 1 < len(chunks) else 1e9; tov = _ct(_wms(ch[0]))[1]
        # every caption is hard-clamped to end strictly before the next one starts, explicit override or not —
        # see build_intense_ass for the fuller reasoning (a caption's hold time settles for what fits rather
        # than ever touching the next card, no exceptions).
        en = (min(max(warp(ch[-1]["end"] - a1) + TAIL, st + _reading_hold(len(ch), HOLD)), nxt - 0.02)
              if tov is None else min(max(warp(ch[-1]["end"] - a1) + float(tov), st + 0.3), nxt - 0.02))
        lead_cs = max(0, int(round((warp(ch[0]["start"] - a1) - st) * 100)))
        # every word gets its OWN white->highlight fade, timed to when it is actually spoken. A word you edited into
        # several words (typed extra text into one slot) is split back into its own words here, sharing that one
        # slot's time budget, so the highlight animation still sweeps across each of them individually.
        pieces = []; t_ms = lead_cs * 10.0
        for j, w in enumerate(ch):
            dur = (ch[j + 1]["start"] - w["start"]) if j + 1 < len(ch) else (w["end"] - w["start"])
            dur_ms = max(40.0, dur * 1000)
            subs = []
            for li, seg in enumerate(str(w["text"]).split("\n")):
                for ti, tok in enumerate(seg.split()):
                    subs.append((tok, "\\N" if (li > 0 and ti == 0) else None))
            if not subs:
                subs = [("", None)]
            share = dur_ms / len(subs)
            for tok, forced_sep in subs:
                ms1, ms2 = t_ms, t_ms + min(ANIM, share); t_ms += share
                sep = forced_sep if forced_sep is not None else (" " if pieces else "")
                pieces.append(f"{sep}{{\\1c&HFFFFFF&\\t({ms1:.0f},{ms2:.0f},\\1c{GOLD})}}{clean_word(tok)}")
        text = ass_translation_prefix(ch) + "{\\fad(50,0)\\fscx112\\fscy112\\t(0,110,\\fscx100\\fscy100)}" + "".join(pieces)
        lines.append(f"Dialogue: 0,{fmt_ass(st + shift)},{fmt_ass(en + shift)},Cap,,0,0,0,,{text}")
    head = (ASS_HEAD.replace("Impact,92,", f"Impact,{int(SETTINGS.get('caption_size', 92))},")
            .replace(",60,60,540,1", f",60,60,{int(SETTINGS.get('caption_margin_v', 540))},1")
            .replace("&H0080C8F8", ass_bgr(SETTINGS.get("caption_highlight", "#f8c880")))
            .replace("&H00602820", ass_bgr(SETTINGS.get("caption_outline", "#202860"))))
    Path(path).write_text(head + "\n".join(lines) + "\n")
    return len(chunks)


# ------------------------------------------------------------------ AA drawing helpers


def aa_paste(frame, box, fn, alpha=1.0, ss=3):
    """Draw with supersampling into a small layer and alpha-composite onto `frame` (RGBA)."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0 or alpha <= 0:
        return
    layer = Image.new("RGBA", ((x1 - x0) * ss, (y1 - y0) * ss), (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer), ss, x0, y0)
    layer = layer.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    if alpha < 1:
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
    frame.alpha_composite(layer, (x0, y0))


def draw_ring(frame, c, r, color, prog, tnow, width=9):
    cx, cy = c; rx, ry = r
    pulse = 1 + 0.045 * math.sin(tnow * 7.0) if prog >= 1 else 1
    rx, ry = rx * pulse, ry * pulse
    pad = 60

    def fn(d, ss, ox, oy):
        box = [(cx - ox - rx) * ss, (cy - oy - ry) * ss, (cx - ox + rx) * ss, (cy - oy + ry) * ss]
        d.ellipse(box, fill=color + (36,))
        d.ellipse(box, outline=color + (60,), width=int(width * 2.6 * ss))
        d.arc(box, -90, -90 + 360 * min(prog, 1.0), fill=color + (255,), width=int(width * ss))
    aa_paste(frame, (cx - rx - pad, cy - ry - pad, cx + rx + pad, cy + ry + pad), fn, ss=2)


def draw_line(frame, p0, p1, color, prog, width=7):
    x0, y0 = p0; x1 = x0 + (p1[0] - x0) * prog; y1 = y0 + (p1[1] - y0) * prog
    pad = 24

    def fn(d, ss, ox, oy):
        d.line([((x0 - ox) * ss, (y0 - oy) * ss), ((x1 - ox) * ss, (y1 - oy) * ss)],
               fill=color + (255,), width=int(width * ss))
        rr = 9 * ss
        d.ellipse([(x0 - ox) * ss - rr, (y0 - oy) * ss - rr, (x0 - ox) * ss + rr, (y0 - oy) * ss + rr],
                  fill=color + (255,))
    aa_paste(frame, (min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad), fn, ss=2)


def _pill_body(d, x0, y0, ss, w, h, scale, color, num, text, size, padx, badge):
    d.rounded_rectangle([x0, y0, x0 + w * ss, y0 + h * ss], radius=h * ss / 2.2,
                        fill=DARK + (238,), outline=color + (255,), width=int(5 * ss * scale))
    if num:
        bx, by = x0 + (16 * scale) * ss, y0 + (h - badge) / 2 * ss
        d.ellipse([bx, by, bx + badge * ss, by + badge * ss], fill=color + (255,))
        nf = font(int(44 * scale) * ss)
        d.text((bx + badge * ss / 2, by + badge * ss / 2 + 2 * ss), str(num), font=nf, fill=DARK + (255,), anchor="mm")
    tf = font(int(size * scale) * ss)
    tx = x0 + (padx + (badge + 14 * scale if num else 0)) * ss
    d.text((tx, y0 + h * ss / 2 + 3 * ss), text, font=tf, fill=WHITE + (255,), anchor="lm")


def draw_pill(frame, center, text, color, scale, alpha, num=None, size=56, angle=0):
    f = font(int(size * scale))
    tw = f.getlength(text)
    th = int(size * scale)
    padx, pady = int(30 * scale), int(16 * scale)
    badge = int(64 * scale) if num else 0
    w = int(tw + padx * 2 + (badge + 14 * scale if num else 0))
    h = int(th + pady * 2 + 6)
    cx, cy = center
    if not angle:                                       # the common case: no rotation, exactly as before
        box = (cx - w / 2 - 12, cy - h / 2 - 12, cx + w / 2 + 12, cy + h / 2 + 12)

        def fn(d, ss, ox, oy):
            x0, y0 = (cx - w / 2 - ox) * ss, (cy - h / 2 - oy) * ss
            _pill_body(d, x0, y0, ss, w, h, scale, color, num, text, size, padx, badge)
        aa_paste(frame, box, fn, alpha=alpha, ss=2)
        return w, h
    S, pad = 2, 14                                       # rotated: draw into its own layer, spin it, then paste (like draw_stamp)
    lay = Image.new("RGBA", (int((w + pad * 2) * S), int((h + pad * 2) * S)), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    _pill_body(d, pad * S, pad * S, S, w, h, scale, color, num, text, size, padx, badge)
    lay = lay.rotate(angle, resample=Image.BICUBIC, expand=True)
    lay = lay.resize((lay.width // S, lay.height // S), Image.LANCZOS)
    if alpha < 1:
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * alpha)))
    x0, y0 = int(cx - lay.width / 2), int(cy - lay.height / 2)
    frame.alpha_composite(lay, (x0, y0))
    return w, h


def draw_stamp(frame, spec, tnow):
    """Big rotated HOP / DROP stamp with a drawn X or check mark (Impact has no such glyphs)."""
    t = tnow - spec["t0"]
    if t < 0:
        return
    sc = (0.75 + 0.25 * ease_back(t / 0.28)) * spec.get("scale", 1.0)
    color, text, sym = spec["color"], spec["text"], spec["sym"]
    S = 2
    f = ImageFont.truetype(IMPACT, int(140 * sc) * S)
    icon, padx = int(110 * sc) * S, 40 * S
    w = int(padx * 2 + icon + 30 * S + f.getlength(text)); h = int(200 * sc) * S
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    d.rounded_rectangle([6, 6, w - 6, h - 6], radius=34 * S, fill=DARK + (222,), outline=color + (255,), width=10 * S)
    cx, cy, r = padx + icon // 2, h // 2, int(icon * 0.4)
    if sym == "x":
        for a, b in [((-r, -r), (r, r)), ((-r, r), (r, -r))]:
            d.line([(cx + a[0], cy + a[1]), (cx + b[0], cy + b[1])], fill=color + (255,), width=22 * S)
    else:
        d.line([(cx - r, cy), (cx - r * 0.2, cy + r * 0.8), (cx + r, cy - r * 0.8)], fill=color + (255,), width=24 * S, joint="curve")
    d.text((padx + icon + 30 * S, h // 2 + 6 * S), text, font=f, fill=color + (255,), anchor="lm")
    lay = lay.rotate(spec.get("angle", 0), resample=Image.BICUBIC, expand=True)
    lay = lay.resize((lay.width // S, lay.height // S), Image.LANCZOS)
    a = min(1.0, t / 0.12)
    if a < 1:
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    cx, cy = spec["xy"]
    x0 = min(max(0, int(cx - lay.width / 2)), W - lay.width)
    y0 = min(max(0, int(cy - lay.height / 2)), H - lay.height)
    frame.alpha_composite(lay, (x0, y0))


# ------------------------------------------------------------------ freeze frames (annotated)

@lru_cache(None)
def _spot(key, still_path):
    # built at the STILL's own native resolution (== the 1080x1920 annotation reference, for every project with
    # freeze frames today — see ANNO_SCALE above), not the export W/H: this mask multiplies directly against the
    # native-resolution still, before that still is resized up/down to the actual export canvas.
    ann = ANN[key]
    with Image.open(still_path) as im:
        Wn, Hn = im.size
    yy, xx = np.mgrid[0:Hn, 0:Wn].astype(np.float32)
    m = np.zeros((Hn, Wn), np.float32)
    for p in ann["pts"]:
        cx, cy = p["c"]; rx, ry = p["r"]
        g = np.exp(-(((xx - cx) / (rx * 1.9)) ** 2 + ((yy - cy) / (ry * 1.9)) ** 2))
        m = np.maximum(m, g)
    return m


def freeze_frame(args):
    key, still_path, i, n, out_dir = args
    ann = ANN[key]; t = i / FPS; T = n / FPS
    base = np.asarray(Image.open(still_path).convert("RGB"), dtype=np.float32)
    Hn, Wn = base.shape[:2]                                    # the still's own native pixel size — the crop below
                                                                # is applied to THIS image, not the export canvas
    dim = 0.46 * ease_out((t - 0.04) / 0.30)
    m = _spot(key, still_path)
    shade = 1 - dim * (1 - m)
    img = Image.fromarray(np.clip(base * shade[..., None], 0, 255).astype(np.uint8))
    zoom = 1 + 0.05 * ease_out(t / max(T, 0.1))
    # fx/fy are the FRACTIONAL zoom-in center (0..1), read off the 1080x1920 annotation reference regardless of
    # either the still's native size or the export size; the crop box itself is then sized/positioned against the
    # still's OWN native dimensions, since that's the image actually being cropped.
    fx = float(np.mean([p["c"][0] for p in ann["pts"]])) / ANNO_REF_W
    fy = float(np.mean([p["c"][1] for p in ann["pts"]])) / ANNO_REF_H
    cw, ch = Wn / zoom, Hn / zoom
    x0 = (Wn - cw) * fx; y0 = (Hn - ch) * fy
    img = img.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))
    frame = img.convert("RGBA")                                # `frame` is now genuinely at the export (W,H) —
                                                                 # everything drawn below must be in THAT space

    # viewfinder corners + breakdown chip
    fa = ease_out((t - 0.05) / 0.2)

    def corners(d, ss, ox, oy):
        m_, L, wd = 46 * ss, 100 * ss, 9 * ss
        for (x, y, dx, dy) in [(m_, m_, 1, 1), (W * ss - m_, m_, -1, 1), (m_, H * ss - m_, 1, -1), (W * ss - m_, H * ss - m_, -1, -1)]:
            d.line([(x, y + dy * L), (x, y), (x + dx * L, y)], fill=GOLD + (255,), width=wd, joint="curve")
    aa_paste(frame, (0, 0, W, H), corners, alpha=fa * 0.9, ss=1)
    if t > 0.10:
        draw_pill(frame, (540 * ANNO_SCALE, 250 * ANNO_SCALE), "COACH BREAKDOWN", GOLD, 0.62 + 0.38 * ease_back((t - 0.1) / 0.25), min(1, (t - 0.1) / 0.15), size=46 * ANNO_SCALE)

    for k, p in enumerate(ann["pts"]):
        tp = t - p["t0"]
        if tp < 0:
            continue
        prog = ease_out(tp / 0.30)
        c = (p["c"][0] * ANNO_SCALE, p["c"][1] * ANNO_SCALE)
        r = (p["r"][0] * ANNO_SCALE, p["r"][1] * ANNO_SCALE)
        draw_ring(frame, c, r, p["color"], prog, tp, width=max(1, round(9 * ANNO_SCALE)))
        # connector from ring edge toward the label
        lx, ly = p["lab"][0] * ANNO_SCALE, p["lab"][1] * ANNO_SCALE; cx, cy = c
        dx, dy = lx - cx, ly - cy; dist = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dist, dy / dist
        rr = (r[0] * r[1]) / math.hypot(r[1] * ux, r[0] * uy)
        p0 = (cx + ux * (rr + 8), cy + uy * (rr + 8))
        p1 = (lx - ux * 26, ly - uy * 26)
        lp = ease_out((tp - 0.22) / 0.20)
        if lp > 0:
            draw_line(frame, p0, p1, p["color"], lp, width=max(1, round(7 * ANNO_SCALE)))
        sp = (tp - 0.36) / 0.26
        if sp > 0:
            draw_pill(frame, (lx, ly), p["text"], p["color"], 0.7 + 0.3 * ease_back(sp), min(1, sp * 2.2), num=k + 1, size=round(56 * ANNO_SCALE), angle=p.get("angle", 0))
    if ann.get("stamp"):
        st = dict(ann["stamp"], xy=(ann["stamp"]["xy"][0] * ANNO_SCALE, ann["stamp"]["xy"][1] * ANNO_SCALE),
                  scale=ann["stamp"].get("scale", 1.0) * ANNO_SCALE)
        draw_stamp(frame, st, t)
    fl = max(0.0, 1 - t / 0.13) * 0.85
    if fl > 0:
        frame = Image.blend(frame, Image.new("RGBA", (W, H), (255, 255, 255, 255)), fl)
    frame.convert("RGB").save(Path(out_dir) / f"f_{i:04d}.jpg", quality=94, subsampling=0)
    return i


def extract_still(t, out_png):
    ff("-ss", f"{t:.3f}", "-i", SRC, "-frames:v", "1", "-vf", f"format=yuv420p,{GRADE}", out_png)


def render_freeze(key, n, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    still = out_dir.parent / f"still_{key}_{ANN[key]['t']:.3f}.png"
    if not still.exists():
        extract_still(ANN[key]["t"], still)
    jobs = [(key, str(still), i, n, str(out_dir)) for i in range(n)]
    with Pool(max(2, (os.cpu_count() or 4) - 1)) as pool:
        pool.map(freeze_frame, jobs, chunksize=4)


# ------------------------------------------------------------------ overlay sequences (RGBA PNG)


def save_seq(frames, out_dir):
    """Save RGBA frames cropped to their union bbox. Returns (x, y, n)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    boxes = [f.getbbox() for f in frames if f.getbbox()]
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    x0, y0 = x0 & ~1, y0 & ~1
    for i, f in enumerate(frames):
        f.crop((x0, y0, x1, y1)).save(out_dir / f"o_{i:04d}.png", compress_level=1)
    return x0, y0, len(frames)


def render_banner(label, title, out_dir):
    n = int(2.9 * FPS); frames = []
    size = 84
    while font(size).getlength(title) > 700 and size > 40:
        size -= 2
    for i in range(n):
        t = i / FPS
        off = -1150 * (1 - ease_back(t / 0.42)) if t < 2.6 else -1150 * ease_in((t - 2.6) / 0.3)
        f = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        def fn(d, ss, ox, oy):
            x = (off - ox) * ss; y0 = (330 - oy) * ss
            d.polygon([(x + 70 * ss, y0), (x + 1030 * ss, y0), (x + 990 * ss, y0 + 150 * ss), (x + 30 * ss, y0 + 150 * ss)], fill=DARK + (238,))
            d.polygon([(x + 30 * ss, y0), (x + 250 * ss, y0), (x + 210 * ss, y0 + 150 * ss), (x - 10 * ss, y0 + 150 * ss)], fill=GOLD + (255,))
            d.rectangle([x + 60 * ss, y0 + 150 * ss, x + 990 * ss, y0 + 160 * ss], fill=GOLD + (255,))
            big = label[1]
            d.text((x + 120 * ss, y0 + 40 * ss), label[0], font=font(34 * ss), fill=DARK + (255,), anchor="mm")
            d.text((x + 120 * ss, y0 + 100 * ss), big, font=font((88 if len(big) < 3 else 44) * ss), fill=DARK + (255,), anchor="mm")
            d.text((x + 285 * ss, y0 + 78 * ss), title, font=font(size * ss), fill=WHITE + (255,), anchor="lm")
        aa_paste(f, (0, 300, W, 520), fn, ss=2)
        frames.append(f)
    return save_seq(frames, out_dir)


def render_chip(text, out_dir, t_in=0.15, t_out=2.6, total=3.0, xy=(540, 250), color=GOLD):
    n = int(total * FPS); frames = []
    for i in range(n):
        t = i / FPS; f = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if t_in <= t <= t_out:
            a = min(1, (t - t_in) / 0.15) * min(1, (t_out - t) / 0.2)
            draw_pill(f, xy, text, color, 0.7 + 0.3 * ease_back((t - t_in) / 0.25), a, size=50)
        frames.append(f)
    return save_seq(frames, out_dir)


def render_countdown(marks, total, out_dir):
    """marks: list of (local_time, text, color). Big numbers pop in and fade."""
    n = int(total * FPS); frames = []
    for i in range(n):
        t = i / FPS; f = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for k, (tm, text, col) in enumerate(marks):
            end = marks[k + 1][0] if k + 1 < len(marks) else tm + 1.3
            if tm <= t < end:
                tt = t - tm; sc = 1.5 - 0.5 * ease_out(tt / 0.22); a = min(1, tt / 0.06) * (1 - ease_in((tt - (end - tm - 0.12)) / 0.12) if (end - tm) > 0.2 else 1)
                big = text.isdigit()
                fs = int((430 if big else 170) * sc)
                lay = Image.new("RGBA", (W, 700), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
                d.text((W // 2, 350), text, font=font(fs), fill=col + (255,), anchor="mm", stroke_width=14, stroke_fill=DARK + (255,))
                lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
                f.alpha_composite(lay, (0, 560))
        frames.append(f)
    return save_seq(frames, out_dir)


# ------------------------------------------------------------------ cards (logo slam / outro)


@lru_cache(None)
def _logo(width):
    lg = Image.open(LOGO).convert("RGBA")
    bb = lg.getbbox(); lg = lg.crop(bb)
    return lg.resize((width, int(lg.height * width / lg.width)), Image.LANCZOS)


@lru_cache(None)
def _bg():
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H * 0.42) / (H * 0.55)) ** 2)
    k = np.clip(1 - d, 0, 1)[..., None]
    c0 = np.array(DARK, np.float32); c1 = np.array(BLUE, np.float32) * 0.85
    return (c0 * (1 - k) + c1 * k).astype(np.uint8)


def card_frame(i, n, kind):
    t = i / FPS; T = n / FPS
    img = Image.fromarray(_bg()).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    # diagonal gold slashes
    for k, (x, wd, al) in enumerate([(120, 26, 200), (190, 10, 140)]):
        s = ease_out((t - 0.05 - k * 0.05) / 0.35)
        d.polygon([(x + 900 * (1 - s), -50), (x + 900 * (1 - s) + wd, -50), (x + 900 * (1 - s) + wd - 380, H + 50), (x + 900 * (1 - s) - 380, H + 50)], fill=GOLD + (al,))
    width = 760 if kind == "hook" else 720
    sc = 0.55 + 0.45 * ease_back(t / 0.4)
    lg = _logo(int(width * sc)); ly = 380 if kind == "hook" else 330
    img.alpha_composite(lg, (int(W / 2 - lg.width / 2), int(ly + (width - lg.width) * 0.45)))
    # shine sweep
    sh = (t - 0.5) / 0.5
    if 0 < sh < 1:
        shine = Image.new("RGBA", (W, H), (0, 0, 0, 0)); sd = ImageDraw.Draw(shine)
        x = -300 + 1700 * sh
        sd.polygon([(x, 0), (x + 90, 0), (x - 300 + 90, H), (x - 300, H)], fill=(255, 255, 255, 70))
        img.alpha_composite(shine)
    txt1, txt2 = (list(HOOK["card"]) if kind == "hook" else list(OUTRO["lines"]))[:2]
    a1 = min(1, max(0, (t - 0.45) / 0.3))
    a2 = min(1, max(0, (t - 0.75) / 0.3))
    y1 = 1330 if kind == "hook" else 1270
    lay = Image.new("RGBA", (W, 400), (0, 0, 0, 0)); ld = ImageDraw.Draw(lay)
    s1 = 92 if kind == "hook" else 108
    ld.text((W // 2, 90), txt1, font=font(s1), fill=WHITE + (int(255 * a1),), anchor="mm")
    ld.text((W // 2, 200), txt2, font=font(52), fill=GOLD + (int(255 * a2),), anchor="mm")
    img.alpha_composite(lay, (0, y1 - 90 + int(20 * (1 - ease_out((t - 0.45) / 0.4)))))
    if kind == "outro" and t > T - 0.5:
        img = Image.blend(img, Image.new("RGBA", (W, H), DARK + (255,)), min(1, (t - (T - 0.5)) / 0.5))
    return img.convert("RGB")


def _card_job(a):
    i, n, kind, out = a
    card_frame(i, n, kind).save(Path(out) / f"f_{i:04d}.jpg", quality=94, subsampling=0)


def render_card(kind, n, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    with Pool(max(2, (os.cpu_count() or 4) - 1)) as pool:
        pool.map(_card_job, [(i, n, kind, str(out_dir)) for i in range(n)], chunksize=4)


TITLE_START_GAP = {"fly": 0.20, "zoom": 0.30, "slide": 0.16, "pop": 0.13}


def title_start(k):
    anim = HOOK.get("title_anim", "pop")
    base = 0.30 if anim == "fly" else (0.28 if anim in ("zoom", "slide") else 0.35)
    return base + TITLE_START_GAP.get(anim, 0.13) * k


def hook_title_seq(out_dir, total):
    n = int(total * FPS); frames = []
    lines = [(l[0], int(l[1]), hexc(l[2]), int(l[3])) for l in HOOK["lines"]]
    anim = HOOK.get("title_anim", "pop")
    for i in range(n):
        t = i / FPS; f = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for k, (txt, sz, col, yy) in enumerate(lines):
            tt = t - title_start(k)
            if tt < 0:
                continue
            dur = {"fly": 0.06, "zoom": 0.32, "slide": 0.16}.get(anim, 0.10)
            a = min(1, tt / dur) * (1 - ease_in((t - (total - 0.2)) / 0.2))
            oy = min(max(0, yy), H - 300)
            if anim == "fly":
                sc = 1.0
                lay = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
                d.text((W // 2, 150), txt, font=font(int(sz * sc)), fill=col + (255,), anchor="mm", stroke_width=10, stroke_fill=DARK + (255,))
                dirn = -1 if k % 2 == 0 else 1                              # titles fly in from alternating sides and settle with a small overshoot
                p = ease_back(tt / 0.42); x = int(dirn * W * 1.05 * (1 - p)); speed = 1 - ease_out(tt / 0.42)
                for gh, ga in ((0.09, 0.16), (0.045, 0.32), (0.0, 1.0)):   # a short motion trail while it is moving fast
                    if gh and speed < 0.08:
                        continue
                    slot = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); slot.paste(lay, (x + int(dirn * W * gh * speed * 3), 0))
                    slot.putalpha(slot.getchannel("A").point(lambda v, ga=ga: int(v * a * ga)))
                    f.alpha_composite(slot, (0, oy))
            elif anim == "zoom":
                zp = ease_out(min(1, tt / 0.32))
                zsc = 2.5 - 1.5 * zp                                       # slams in oversized, settles to 1x
                rot = (1 - zp) * (9 if k % 2 == 0 else -9)                 # and untilts as it lands
                lay = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
                d.text((W // 2, 150), txt, font=font(max(1, int(sz * zsc))), fill=col + (255,), anchor="mm", stroke_width=10, stroke_fill=DARK + (255,))
                if abs(rot) > 0.3:
                    lay = lay.rotate(rot, center=(W // 2, 150), resample=Image.BICUBIC)
                lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
                f.alpha_composite(lay, (0, oy))
            elif anim == "slide":
                sp = ease_out(min(1, tt / 0.34))
                dy = int((1 - sp) * 140)                                   # rises up into place from below
                lay = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
                d.text((W // 2, 150), txt, font=font(sz), fill=col + (255,), anchor="mm", stroke_width=10, stroke_fill=DARK + (255,))
                lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
                f.alpha_composite(lay, (0, oy + dy))
            else:                                                          # pop (default)
                sc = 0.85 + 0.15 * ease_back(tt / 0.3)
                lay = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
                d.text((W // 2, 150), txt, font=font(int(sz * sc)), fill=col + (255,), anchor="mm", stroke_width=10, stroke_fill=DARK + (255,))
                lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
                f.alpha_composite(lay, (0, oy))
        frames.append(f)
    return save_seq(frames, out_dir)


# ------------------------------------------------------------------ SFX + audio


def make_sfx():
    d = WORK / "sfx"; d.mkdir(parents=True, exist_ok=True)
    jobs = {
        "whoosh": ["-f", "lavfi", "-i", "anoisesrc=d=0.7:c=pink:r=48000:a=0.9", "-af",
                   "highpass=f=400,lowpass=f=7000,afade=t=in:d=0.35,afade=t=out:st=0.35:d=0.35,volume=0.9"],
        "boom": ["-f", "lavfi", "-i", "sine=f=58:d=0.7:r=48000", "-af", "afade=t=out:st=0.05:d=0.65,volume=1.4"],
        "shutter": ["-f", "lavfi", "-i", "anoisesrc=d=0.09:c=white:r=48000:a=0.9", "-af",
                    "highpass=f=1800,lowpass=f=9000,afade=t=out:d=0.09"],
        "tick": ["-f", "lavfi", "-i", "sine=f=1046:d=0.12:r=48000", "-af", "afade=t=out:d=0.12"],
        "ding": ["-f", "lavfi", "-i", "sine=f=1568:d=0.5:r=48000", "-af", "afade=t=out:d=0.5"],
    }
    for name, args in jobs.items():
        p = d / f"{name}.wav"
        if not p.exists():
            ff(*args, "-ac", "2", "-c:a", "pcm_s16le", p)
    return d


VOICE_PRESETS = {      # one-click voice EQ + gentle compression
    "clean": "equalizer=f=3000:t=q:w=1.2:g=2.5,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120:makeup=2",
    "bright": "equalizer=f=4500:t=q:w=1.0:g=3.5,equalizer=f=120:t=q:w=1:g=-2",
    "warm": "equalizer=f=200:t=q:w=1:g=2.5,equalizer=f=6000:t=q:w=1:g=-2",
}


def mix_audio(out, n_frames, items):
    total = n_frames / FPS
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]; fl = []
    for k, it in enumerate(items):
        if it["kind"] == "silence":
            cmd += ["-f", "lavfi", "-t", f"{it['d']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            chain = f"[{k}:a]aformat=channel_layouts=stereo"
        elif it["kind"] == "src":
            cmd += ["-ss", f"{it['a']:.3f}", "-t", f"{it['d']:.3f}", "-i", str(it.get("src", SRC))]
            d = it["d"]
            fi, fo = max(0.03, float(it.get("fi", 0.03))), max(0.03, float(it.get("fo", 0.03)))   # never below 30 ms: no pops
            hp = int(SETTINGS.get("audio_highpass", 90))
            dn = f"afftdn=nr={int(SETTINGS.get('audio_denoise_nr', 10))}:nf=-40," if SETTINGS.get("audio_denoise") else ""
            vp = VOICE_PRESETS.get(SETTINGS.get("audio_voice"), "")
            chain = (f"[{k}:a]{(f'highpass=f={hp},' if hp else '')}{dn}{(vp + ',') if vp else ''}aresample=48000,aformat=channel_layouts=stereo,"
                     f"afade=t=in:st=0:d={fi:.3f},afade=t=out:st={max(d - fo, 0):.3f}:d={fo:.3f}")
        else:
            cmd += ["-i", it["path"]]
            chain = f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo"
        ms = int(it.get("delay", 0) * 1000)
        chain += f",volume={it.get('vol', 1.0)},adelay={ms}|{ms}[a{k}]"
        fl.append(chain)
    mix = "".join(f"[a{i}]" for i in range(len(items))) + \
        f"amix=inputs={len(items)}:normalize=0:duration=longest,apad=whole_dur={total:.5f}[out]"
    cmd += ["-filter_complex", ";".join(fl) + ";" + mix, "-map", "[out]", "-t", f"{total:.5f}", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", out]
    run(cmd)


DUCK = {"low": (0.05, 3), "medium": (0.03, 6), "high": (0.02, 12)}     # (threshold, ratio) for sidechaincompress


def mix_tracks(base_a, out_wav, total):
    """Lay music / sound-effect tracks over the voice mix. Tracks marked `duck` dip under the voice."""
    tracks = [t for t in (AUDIO.get("tracks") or []) if not t.get("mute") and t.get("media") in MEDIA]
    if not tracks:
        return base_a
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base_a)]
    fl, duck, plain = [], [], []
    for i, t in enumerate(tracks, start=1):
        inn = float(t.get("in", 0)); start = float(t.get("start", 0))
        dur = float(t.get("dur") or 0) or max(0.1, total - start)
        fi, fo = max(float(t.get("fade_in", 0.05)), 0.01), max(float(t.get("fade_out", 0.05)), 0.01)
        if t.get("loop"):
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(media_path(t["media"]))]
        ms = int(start * 1000)
        fl.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start={inn:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:st=0:d={fi:.3f},afade=t=out:st={max(dur - fo, 0):.3f}:d={fo:.3f},volume={float(t.get('gain', 1.0))},adelay={ms}|{ms}[t{i}]")
        (duck if t.get("duck") else plain).append(f"[t{i}]")
    ins = []
    if duck:
        thr, ratio = DUCK.get(AUDIO.get("duck", "medium"), DUCK["medium"])
        fl.append("[0:a]asplit=2[vo1][vo2]")
        fl.append((("".join(duck) + f"amix=inputs={len(duck)}:normalize=0[dm]") if len(duck) > 1 else f"{duck[0]}anull[dm]"))
        fl.append(f"[dm][vo1]sidechaincompress=threshold={thr}:ratio={ratio}:attack=20:release=400:makeup=1[dk]")
        ins = ["[vo2]", "[dk]"] + plain
    else:
        ins = ["[0:a]"] + plain
    fl.append("".join(ins) + f"amix=inputs={len(ins)}:normalize=0:duration=first,apad=whole_dur={total:.5f}[out]")
    cmd += ["-filter_complex", ";".join(fl), "-map", "[out]", "-t", f"{total:.5f}", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out_wav)]
    run(cmd)
    return out_wav


# ------------------------------------------------------------------ video segment encoder


def crop_zoom(z, fx, fy):
    if abs(z - 1.0) < 1e-3:
        return ""
    return (f",crop=w=iw/{z}:h=ih/{z}:x=(iw-ow)*{fx}:y=(ih-oh)*{fy},scale={W}:{H}:flags=lanczos")


def _src_in(p):
    return ["-ss", f"{p['ss']:.3f}", "-t", f"{p['dur_src']:.4f}", "-i", str(p.get("src", SRC))]


def _src_chain(k, p, label):
    ch = f"[{k}:v]setpts=PTS-STARTPTS"
    if p.get("speed", 1.0) != 1.0:
        ch += f",setpts={1 / p['speed']:.4f}*PTS"
    ch += f",fps={FPS},tpad=stop=4:stop_mode=clone,trim=end_frame={p['n']},setpts=PTS-STARTPTS"
    if p.get("anim"):
        z0, z1, fx, fy = p["anim"]
        nn = max(p["n"] - 1, 1)
        ch += (f",scale={int(W * 1.5)}:{int(H * 1.5)}:flags=bicubic,"
               f"zoompan=z='{z0}+({z1}-{z0})*on/{nn}':x='(iw-iw/zoom)*{fx}':y='(ih-ih/zoom)*{fy}':d=1:s={W}x{H}:fps={FPS}")
    else:
        ch += crop_zoom(p.get("zoom", 1.0), p.get("fx", 0.5), p.get("fy", 0.5))
    ch += f",{GRADE}"
    if p.get("flash"):
        ch += ",fade=t=in:st=0:d=0.14:color=white"
    return ch + f",scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=yuv420p[{label}]"


def encode_segment(out_mp4, parts, overlays, ass, n_total, cwd, dip=None):
    """parts: src/xsrc/seq clips concatenated; overlays: dicts(dir,x,y,n,t0,enable); ass burned LAST.
    xsrc = cross-dissolve from the previous action's continuation into this clip; dip = (fade-in s, fade-out s) through dark."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    fl = []; labels = []; k = 0
    for p in parts:
        if p["kind"] == "src":
            cmd += _src_in(p); fl.append(_src_chain(k, p, f"p{k}")); labels.append(f"[p{k}]"); k += 1
        elif p["kind"] == "xsrc":
            pa = dict(p, ss=p["ssA"], src=p["srcA"], zoom=p["zoomA"], flash=False); pb = dict(p, ss=p["ssB"], src=p["srcB"], zoom=p["zoomB"], flash=False)
            cmd += _src_in(pa) + _src_in(pb)
            fl.append(_src_chain(k, pa, f"xa{k}")); fl.append(_src_chain(k + 1, pb, f"xb{k}"))
            fl.append(f"[xa{k}][xb{k}]xfade=transition=fade:duration={p['n'] / FPS - 0.001:.3f}:offset=0,trim=end_frame={p['n']},setpts=PTS-STARTPTS[p{k}]")
            labels.append(f"[p{k}]"); k += 2
        else:
            cmd += ["-framerate", str(FPS), "-i", str(p["pattern"])]
            fl.append(f"[{k}:v]setpts=PTS-STARTPTS,fps={FPS},tpad=stop=4:stop_mode=clone,trim=end_frame={p['n']},setpts=PTS-STARTPTS,"
                      f"scale={W}:{H}:in_range=pc:in_color_matrix=bt601:out_range=tv:out_color_matrix=bt709,setsar=1,format=yuv420p[p{k}]")
            labels.append(f"[p{k}]"); k += 1
    cur = "[base]"
    fl.append("".join(labels) + f"concat=n={len(parts)}:v=1:a=0{cur}")
    if dip and (dip[0] or dip[1]):
        f = []
        if dip[0]: f.append(f"fade=t=in:st=0:d={dip[0]:.3f}:color=0x141a40")
        if dip[1]: f.append(f"fade=t=out:st={max(n_total / FPS - dip[1], 0):.3f}:d={dip[1]:.3f}:color=0x141a40")
        fl.append(f"[base]{','.join(f)}[basef]"); cur = "[basef]"
    for o in overlays:
        ol = f"o{k}"
        if o.get("kind") == "video":                       # a real video clip stacked on top (multi-track / PiP), not a rendered PNG sequence
            cmd += ["-ss", f"{o['in']:.3f}", "-t", f"{o['n'] / FPS + 0.05:.4f}", "-i", str(o["path"])]
            fit = (f"scale={o['w']}:{o['h']}:force_original_aspect_ratio=increase,crop={o['w']}:{o['h']}" if o.get("fit", "cover") == "cover"
                   else f"scale={o['w']}:{o['h']}:force_original_aspect_ratio=decrease")
            op = float(o.get("opacity", 1.0))
            alpha = f",format=rgba,colorchannelmixer=aa={op:.3f}" if op < 0.999 else ",format=yuv420p"
            # establish a clean 0-based n-frame sequence FIRST, then shift it to its on-screen start (t0) as the
            # very last timestamp op — shifting before trim and then re-zeroing after trim (as an earlier version
            # of this did) silently throws the shift away, so every overlay would start at frame 0 regardless of t0.
            fl.append(f"[{k}:v]setpts=PTS-STARTPTS,fps={FPS},trim=end_frame={o['n']},setpts=PTS-STARTPTS+{o.get('t0', 0):.4f}/TB,{fit},setsar=1{alpha}[{ol}]")
        else:
            if o.get("still"):
                cmd += ["-loop", "1"]
            cmd += ["-framerate", str(FPS), "-i", str(o["pattern"])]
            fl.append(f"[{k}:v]format=rgba,setpts=PTS-STARTPTS+{o.get('t0', 0):.4f}/TB[{ol}]")
        nxt = f"[v{k}]"
        en = f":enable='{o['enable']}'" if o.get("enable") else ""
        fl.append(f"{cur}[{ol}]overlay=x={o['x']}:y={o['y']}:eof_action=pass:format=auto{en}{nxt}")
        cur = nxt; k += 1
    tail = ""
    if ass:
        tail += f",subtitles=filename={ass}:fontsdir={FONTS_DIR}"     # captions LAST (Hard Rule 1)
    if DRAFT:
        tail += ",scale=540:960"
    fl.append(f"{cur}null{tail},format=yuv420p[vout]")
    preset, crf = ("ultrafast", "28") if DRAFT else ("medium", "21")
    cmd += ["-filter_complex", ";".join(fl), "-map", "[vout]", "-an", "-frames:v", str(n_total),
            "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", out_mp4]
    run(cmd, cwd=cwd)


# ------------------------------------------------------------------ segment definitions

def seg_frames(s):
    k = s["kind"]
    if k == "talk": return cut_range(s["a"], s["b"])[1] + sum(fz_hold_frames(f) for f in (s.get("fz") or []))
    if k in ("reps", "clip"): return round(s["d"] * FPS)
    if k == "slow": return round(s.get("d", 0.8) / s.get("speed", 0.25) * FPS)
    if k == "hook": return sum(round(sh.get("dur", 1.0) / sh.get("speed", 1.0) * FPS) for sh in HOOK["shots"]) + int(HOOK.get("card_frames", round(1.6 * FPS)))
    return int(OUTRO.get("frames", round(5.0 * FPS)))


@lru_cache(maxsize=None)
def seg_global_start(sid):
    """Cumulative OUTPUT-timeline frame offset where segment `sid` begins — sum of every earlier segment's own
    frame count. Segments are rendered and cached independently (each `build_seg` call knows nothing about the
    others), but a `video_tracks` clip's `start`/`dur` are given in this GLOBAL timeline, so every segment needs
    to know where it personally lands in it to work out which clips overlap it and at what local offset."""
    t = 0
    for s in SEGS:
        if s["id"] == sid:
            return t
        t += seg_frames(s)
    return t


def video_track_overlays(s, g0):
    """Turn `project.video_tracks` clips into segment-local `encode_segment` overlay dicts for whichever ones
    overlap this segment's slice of the GLOBAL timeline. `g0` = this segment's own global start, in frames."""
    tracks = PROJECT.get("video_tracks") or []
    g0s = g0 / FPS; n = seg_frames(s); g1s = g0s + n / FPS
    out = []
    for tr in tracks:
        for c in (tr.get("clips") or []):
            c0, c1 = float(c["start"]), float(c["start"]) + float(c["dur"])
            ov0, ov1 = max(c0, g0s), min(c1, g1s)
            if ov1 - ov0 < 0.5 / FPS:                                  # no (or sub-frame) overlap with this segment
                continue
            local0, local1 = ov0 - g0s, ov1 - g0s
            n_ov = max(1, round((ov1 - ov0) * FPS))
            src_in = float(c.get("a", 0)) + (ov0 - c0)                 # source-in, adjusted if this segment only sees the clip's middle/tail
            x, y = round(float(c.get("x", 0.0)) * W), round(float(c.get("y", 0.0)) * H)
            w, h = max(2, round(float(c.get("w", 0.3)) * W)), max(2, round(float(c.get("h", 0.3)) * H))
            whole = local0 <= 0.5 / FPS and local1 >= n / FPS - 0.5 / FPS
            out.append(dict(kind="video", path=str(media_path(c["media"])), **{"in": src_in}, n=n_ov, x=x, y=y, w=w, h=h,
                             opacity=float(c.get("opacity", 1.0)), fit=c.get("fit", "cover"), t0=max(0.0, local0),
                             enable=None if whole else f"between(t,{max(0.0, local0):.3f},{min(local1, n / FPS):.3f})"))
    return out


def video_track_audio_items(s, g0):
    """The `mix_audio` `items` counterpart to `video_track_overlays` — a stacked clip's own sound is mixed in
    too (unless you muted it), using the exact same overlap math so it never drifts out of sync with its picture."""
    tracks = PROJECT.get("video_tracks") or []
    g0s = g0 / FPS; g1s = g0s + seg_frames(s) / FPS
    out = []
    for tr in tracks:
        for c in (tr.get("clips") or []):
            ca = c.get("audio") or {}
            if ca.get("mute") or not media_has_audio(c["media"]):
                continue
            c0, c1 = float(c["start"]), float(c["start"]) + float(c["dur"])
            ov0, ov1 = max(c0, g0s), min(c1, g1s)
            if ov1 - ov0 < 0.5 / FPS:
                continue
            src_in = float(c.get("a", 0)) + (ov0 - c0)
            out.append(dict(kind="src", a=src_in, d=ov1 - ov0, delay=ov0 - g0s, src=media_path(c["media"]),
                             vol=float(ca.get("gain", 1.0)), fi=0.03, fo=0.03))
    return out


def _canon(o):                          # 352.0 and 352 are the same edit (browsers drop the ".0")
    if isinstance(o, dict): return {k: _canon(v) for k, v in o.items()}
    if isinstance(o, list): return [_canon(v) for v in o]
    return int(o) if isinstance(o, float) and o.is_integer() else o


def _sha(o):
    return hashlib.sha1(json.dumps(_canon(o), sort_keys=True).encode()).hexdigest()


def _edits_for(s):
    if s["kind"] != "talk" or not (CAPTION_EDITS or CAPTION_BREAKS or CAPTION_JOINS or CAPTION_TIMING):
        return {}
    a1, n = cut_range(s["a"], s["b"]); b1 = a1 + n / FPS; inr = lambda k: a1 - 0.05 <= int(k) / 1000 <= b1 + 0.05
    out = {k: v for k, v in CAPTION_EDITS.items() if inr(k)}
    if CAPTION_BREAKS or CAPTION_JOINS or CAPTION_TIMING:
        out["__layout"] = dict(b=sorted(k for k in CAPTION_BREAKS if inr(k)), j=sorted(k for k in CAPTION_JOINS if inr(k)), t={k: v for k, v in CAPTION_TIMING.items() if inr(k)})
    return out


def transition_plan(s):
    """How a clip enters: ("xfade", frames, previous clip's end, previous zoom) | ("dip", fade in s, fade out s) | None."""
    mode = s.get("transition") or SETTINGS.get("transition", "none")
    if mode == "none" or s["kind"] not in ("talk", "reps", "clip"):
        return None
    td = float(s.get("transition_dur") or SETTINGS.get("transition_dur", 0.35))
    if mode == "dip":
        return ("dip", td / 2, td / 2)
    i = next(k for k, x in enumerate(SEGS) if x["id"] == s["id"]); prev = SEGS[i - 1] if i > 0 else None
    if prev and prev["kind"] in ("talk", "reps") and s["kind"] in ("talk", "reps"):
        pa, pn = cut_range(prev["a"], prev["b"]) if prev["kind"] == "talk" else (prev["a"], round(prev["d"] * FPS))
        # a freeze holding right up to the end of `prev` means the last thing actually shown there is a still
        # frame, not live footage — crossfading from the raw source at `pend` would show the wrong picture
        tail_freeze = any(ANN[f["ann"]]["t"] - pa >= pn / FPS - 0.25 for f in (prev.get("fz") or []))
        pend = pa + pn / FPS; nfr = max(3, round(td * FPS))
        if not tail_freeze and pend + nfr / FPS < float(META.get("duration") or 1e9) - 0.1:
            return ("xfade", nfr, pend, prev.get("zoom", 1.0))
    return ("dip", min(td / 2, 0.25), 0.0)                                    # first clip after a card, freeze or cutaway: fade up from dark


def seg_hash(s):
    """Video hash: everything that changes pixels. Audio-only edits (`audio`) do not touch it."""
    seg = {k: v for k, v in s.items() if k != "audio"}
    pl = {"seg": seg, "settings": {k: v for k, v in SETTINGS.items() if not k.startswith("audio_") and k != "loudness" and v != DEFAULT_SETTINGS.get(k)},
          "rev": BUILD_REV, "draft": DRAFT, "edits": _edits_for(s),
          "ann": {f["ann"]: PROJECT["annotations"].get(f["ann"]) for f in (s.get("fz") or [])},
          "hook": HOOK if s["kind"] == "hook" else None, "outro": OUTRO if s["kind"] == "outro" else None,
          "media": MEDIA.get(s.get("media"), {}).get("file") if s.get("media") else None}
    if (s.get("transition") or SETTINGS.get("transition", "none")) != "none":       # a clip that fades in depends on the clip before it
        i = next(k for k, x in enumerate(SEGS) if x["id"] == s["id"])
        pl["prev"] = {k: v for k, v in SEGS[i - 1].items() if k not in ("audio", "tip_title", "chip")} if i > 0 else None
    if PROJECT.get("video_tracks") and s["kind"] in ("talk", "reps", "clip"):
        # a video_tracks clip is placed in the GLOBAL timeline, not this segment's own — so this segment's
        # pixels can change either because a track/clip itself changed, OR because an earlier segment's own
        # duration shifted (changing where THIS segment starts and so which clips now land on it). Hashing the
        # whole video_tracks list plus this segment's own global start covers both without re-deriving overlap.
        pl["video_tracks"] = PROJECT["video_tracks"]; pl["g0"] = seg_global_start(s["id"])
    return _sha(pl)


def seg_audio_hash(s):
    pl = {"seg": s, "audio_settings": {k: v for k, v in SETTINGS.items() if (k.startswith("audio_") or k.startswith("cut_pad")) and v != DEFAULT_SETTINGS.get(k)}, "rev": BUILD_REV,
          "hook": HOOK if s["kind"] == "hook" else None, "media": MEDIA.get(s.get("media"), {}).get("file") if s.get("media") else None}
    if PROJECT.get("video_tracks") and s["kind"] in ("talk", "reps", "clip"):    # same reasoning as seg_hash: a track's own sound, or an earlier segment's duration shifting where this one starts, both change its mix
        pl["video_tracks"] = PROJECT["video_tracks"]; pl["g0"] = seg_global_start(s["id"])
    return _sha(pl)


def cache_state(s):
    d = WORK / s["id"]
    files = (d / "v.mp4").exists() and (d / "a.wav").exists()
    v = (not FORCE) and files and (d / "v.sha").exists() and (d / "v.sha").read_text() == seg_hash(s)
    a = (not FORCE) and files and (d / "a.sha").exists() and (d / "a.sha").read_text() == seg_audio_hash(s)
    return v, a


def is_cached(s):
    v, a = cache_state(s)
    return v and a


def assemble_hash():
    return _sha({"segs": [[seg_hash(s), seg_audio_hash(s)] for s in SEGS], "audio": AUDIO, "loud": SETTINGS.get("loudness"), "draft": DRAFT,
                 "media": {k: MEDIA[k].get("file") for k in MEDIA}})


def assemble_cached():
    out = OUT_DIR / ("preview.mp4" if DRAFT else "final.mp4")
    f = WORK / "assemble.sha"
    return (not FORCE) and out.exists() and f.exists() and f.read_text() == assemble_hash()


def seg_dir(sid):
    d = WORK / sid; d.mkdir(parents=True, exist_ok=True); return d


def watermark_overlay(T):
    p = WORK / "wm.png"
    if not p.exists():
        lg = Image.open(LOGO).convert("RGBA"); lg = lg.crop(lg.getbbox())
        h = 150; lg = lg.resize((int(lg.width * h / lg.height), h), Image.LANCZOS)
        lg.putalpha(lg.getchannel("A").point(lambda v: int(v * 0.88))); lg.save(p)
    return dict(pattern=p, x=34, y=138, t0=0, enable=None, still=True)


def build_seg(s, only_audio=False):
    sid = s["id"]; d = seg_dir(sid); kind = s["kind"]
    sfx = make_sfx()
    out_v, out_a = d / "v.mp4", d / "a.wav"
    wm_png = watermark_overlay(0)["pattern"]

    def wm(T=None, until=None):
        return dict(pattern=wm_png, x=34, y=138, t0=0, still=True, enable=f"lt(t,{until})" if until else None)

    if kind in ("talk", "reps", "clip"):
        src = media_path(s.get("media")) if kind != "talk" else SRC
        if kind in ("reps", "clip"):
            a1 = s["a"]; n0 = round(s["d"] * FPS)
        else:
            a1, n0 = cut_range(s["a"], s["b"])
        zoom = s.get("zoom", 1.0); au = s.get("audio") or {}
        fzs = s.get("fz") or []
        fz_hold = [fz_hold_frames(f) for f in fzs]
        n = n0 + sum(fz_hold)                                              # a freeze HOLDS extra time; it never skips the footage after it
        T = n / FPS; parts = []; ovs = []
        if not only_audio and PROJECT.get("video_tracks"):
            ovs += video_track_overlays(s, seg_global_start(sid))          # stacked B-roll/PiP clips (multi-track), UNDER banners/chip/watermark, ABOVE the base footage
        has_voice = not (kind == "clip" and not media_has_audio(s.get("media")))
        items = [] if has_voice else [dict(kind="silence", d=T)]
        if PROJECT.get("video_tracks"):
            items += video_track_audio_items(s, seg_global_start(sid))     # a stacked clip's own sound, unless muted — computed even in only_audio mode

        def voice(seg_a1, seg_n, out_delay_fr):                            # a piece of the real dialogue, placed at its OUTPUT-timeline delay
            if has_voice and seg_n > 0:
                items.append(dict(kind="src", a=seg_a1, d=seg_n / FPS, delay=out_delay_fr / FPS, src=src,
                                   vol=(0.0 if au.get("mute") else float(au.get("gain", 1.0))),
                                   fi=au.get("fade_in", 0.03), fo=au.get("fade_out", 0.03)))

        cur = 0; out_cur = 0                                                # cur = source frames consumed so far; out_cur = output frames written so far
        for f, nf in zip(fzs, fz_hold):
            ann = ANN[f["ann"]]; fs_n = round((ann["t"] - a1) * FPS)
            if fs_n > cur:
                seg_n = fs_n - cur
                parts.append(dict(kind="src", ss=a1 + cur / FPS, n=seg_n, dur_src=seg_n / FPS + 0.2, zoom=zoom, src=src))
                voice(a1 + cur / FPS, seg_n, out_cur)
                out_cur += seg_n; cur = fs_n
            if not only_audio:
                render_freeze(f["ann"], nf, d / f"fz_{f['ann']}")
            parts.append(dict(kind="seq", pattern=d / f"fz_{f['ann']}" / "f_%04d.jpg", n=nf))
            items.append(dict(kind="wav", path=sfx / "shutter.wav", delay=out_cur / FPS, vol=0.5))
            for p in ann["pts"]:                                           # a soft pop as each callout lands, instead of the coach's voice just carrying on underneath
                pop_t = min(float(p.get("t0", 0)) + 0.32, max(0.0, nf / FPS - 0.05))
                items.append(dict(kind="wav", path=sfx / "tick.wav", delay=out_cur / FPS + pop_t, vol=0.3))
            out_cur += nf                                                  # cur stays at fs_n on purpose: the source resumes at the freeze point, nothing skipped
        if cur < n0:
            seg_n = n0 - cur
            parts.append(dict(kind="src", ss=a1 + cur / FPS, n=seg_n, dur_src=seg_n / FPS + 0.2, zoom=zoom, src=src))
            voice(a1 + cur / FPS, seg_n, out_cur)
        tplan = transition_plan(s) if kind in ("talk", "reps", "clip") else None
        if tplan and tplan[0] == "xfade" and src == SRC and parts and parts[0]["kind"] == "src" and parts[0]["n"] > tplan[1] + 2 and abs(parts[0]["ss"] - a1) < 1e-6:
            p0 = parts[0]; nfr = tplan[1]
            parts[0:1] = [dict(kind="xsrc", n=nfr, dur_src=nfr / FPS + 0.2, ssA=tplan[2], srcA=SRC, zoomA=tplan[3], ssB=p0["ss"], srcB=SRC, zoomB=p0.get("zoom", 1.0)),
                          dict(p0, ss=p0["ss"] + nfr / FPS, n=p0["n"] - nfr, dur_src=(p0["n"] - nfr) / FPS + 0.2)]
            dip = None
        elif tplan and tplan[0] == "xfade":
            dip = (0.2, 0.0)
        else:
            dip = tplan[1:] if tplan else None
        if s.get("tip"):
            tp = s["tip"]
            lab = tuple(s["tip_label"]) if s.get("tip_label") else (("BONUS", "+") if tp == "B" else ("TIP", str(tp)))
            if not only_audio:
                x, y, nn = render_banner(lab, s.get("tip_title") or str(tp), d / "banner")
                ovs.append(dict(pattern=d / "banner" / "o_%04d.png", x=x, y=y, t0=0.0))
            items.append(dict(kind="wav", path=sfx / "whoosh.wav", delay=0.0, vol=0.22))
        if s.get("chip"):
            if not only_audio:
                x, y, nn = render_chip(s["chip"], d / "chip")
                ovs.append(dict(pattern=d / "chip" / "o_%04d.png", x=x, y=y, t0=0.0))
        if s.get("countdown"):
            marks = [(584.80 - a1, "5", GOLD), (585.90 - a1, "4", GOLD), (587.06 - a1, "3", GOLD),
                     (588.26 - a1, "2", GOLD), (589.48 - a1, "1", GOLD), (591.20 - a1, "THAT'S A WIN!", WHITE)]
            if not only_audio:
                x, y, nn = render_countdown(marks, T, d / "cd")
                ovs.append(dict(pattern=d / "cd" / "o_%04d.png", x=x, y=y, t0=0.0))
            for tm, txt, _ in marks[:5]:
                items.append(dict(kind="wav", path=sfx / "tick.wav", delay=tm, vol=0.28))
            items.append(dict(kind="wav", path=sfx / "ding.wav", delay=marks[5][0], vol=0.3))
        ovs.append(wm())
        ass = None
        if kind == "talk" and s.get("captions", True) and not only_audio:
            build_ass(a1, n0, d / "cap.ass", fzs=fzs); ass = "cap.ass"
        if not only_audio:
            encode_segment(out_v, parts, ovs, ass, n, d, dip=dip)
        mix_audio(out_a, n, items)
        return n

    if kind == "slow":
        speed = s.get("speed", 0.25); src_dur = s.get("d", 0.8); n = round(src_dur / speed * FPS)
        x, y, nn = render_chip(f"SLOW-MO  {speed:g}X", d / "chip", t_in=0.12, t_out=n / FPS - 0.2, total=n / FPS)
        parts = [dict(kind="src", ss=s["a"], n=n, dur_src=src_dur + 0.15, speed=speed, flash=True,
                      anim=(1.0, 1.12, s.get("fx", 0.5), s.get("fy", 0.5)))]
        ovs = [dict(pattern=d / "chip" / "o_%04d.png", x=x, y=y, t0=0.0), wm()]
        encode_segment(out_v, parts, ovs, None, n, d)
        mix_audio(out_a, n, [dict(kind="wav", path=sfx / "whoosh.wav", delay=0.0, vol=0.30),
                             dict(kind="wav", path=sfx / "boom.wav", delay=0.02, vol=0.35)])
        return n

    if kind == "hook":
        shots = HOOK["shots"]
        shot_ns = [round(sh.get("dur", 1.0) / sh.get("speed", 1.0) * FPS) for sh in shots]
        starts = [sum(shot_ns[:i]) for i in range(len(shots))]
        card_n = int(HOOK.get("card_frames", round(1.6 * FPS))); n = sum(shot_ns) + card_n; vid_end = sum(shot_ns) / FPS
        x, y, nn = hook_title_seq(d / "title", vid_end)
        parts = []
        for i, sh in enumerate(shots):
            part = dict(kind="src", ss=sh["a"], n=shot_ns[i], dur_src=sh.get("dur", 1.0) + 0.15, speed=sh.get("speed", 1.0), flash=(i > 0))
            if sh.get("zoom_anim", False):
                part["anim"] = (1.0, 1.10, sh.get("fx", 0.5), sh.get("fy", 0.5))
            else:
                part.update(zoom=sh.get("zoom", 1.0), fx=sh.get("fx", 0.5), fy=sh.get("fy", 0.5))
            parts.append(part)
        if card_n > 0:                      # card_frames = 0 gives a cold open with no logo card
            render_card("hook", card_n, d / "card")
            parts.append(dict(kind="seq", pattern=d / "card" / "f_%04d.jpg", n=card_n))
        ovs = [dict(pattern=d / "title" / "o_%04d.png", x=x, y=y, t0=0.0)] + ([wm(until=f"{vid_end:.3f}")] if card_n > 0 else [wm()])
        encode_segment(out_v, parts, ovs, None, n, d)
        items = []
        if WORDS:
            qa, qn = cut_range(HOOK["quote"]["a"], HOOK["quote"]["b"])
            items.append(dict(kind="src", a=qa, d=qn / FPS, delay=0.30, vol=1.0))
        for i in range(1, len(shots)):
            items.append(dict(kind="wav", path=sfx / "whoosh.wav", delay=starts[i] / FPS - 0.12, vol=0.22))
        if HOOK.get("title_anim", "pop") == "fly" and HOOK.get("title_sfx", True):      # a swoosh as each title line flies in, a soft thud when the last one lands
            for k in range(len(HOOK["lines"])):
                items.append(dict(kind="wav", path=sfx / "whoosh.wav", delay=max(0.0, title_start(k) - 0.08), vol=0.45))
            items.append(dict(kind="wav", path=sfx / "boom.wav", delay=title_start(len(HOOK["lines"]) - 1) + 0.34, vol=0.3))
        items.append(dict(kind="wav", path=sfx / "boom.wav", delay=vid_end, vol=0.55))
        items.append(dict(kind="wav", path=sfx / "whoosh.wav", delay=vid_end - 0.2, vol=0.3))
        mix_audio(out_a, n, items)
        return n

    if kind == "outro":
        n = int(OUTRO.get("frames", round(5.0 * FPS)))
        render_card("outro", n, d / "card")
        encode_segment(out_v, [dict(kind="seq", pattern=d / "card" / "f_%04d.jpg", n=n)], [], None, n, d,
                       dip=(0.2, 0.0) if SETTINGS.get("transition", "none") != "none" else None)
        mix_audio(out_a, n, [dict(kind="wav", path=sfx / "whoosh.wav", delay=0.0, vol=0.3),
                             dict(kind="wav", path=sfx / "boom.wav", delay=0.28, vol=0.45)])
        return n
    raise ValueError(kind)


# ------------------------------------------------------------------ assemble


def loudnorm_two_pass(in_wav, out_m4a):
    f = f"loudnorm=I={float(SETTINGS.get('loudness', -14))}:TP=-1.5:LRA=11"           # 1.5 dB of headroom so the AAC encode cannot push a peak over 0 dBFS
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(in_wav), "-af", f + ":print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    j = json.loads(r.stderr[r.stderr.rfind("{"): r.stderr.rfind("}") + 1])
    f2 = (f"{f}:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}"
          f":measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true")
    ff("-i", in_wav, "-af", f2, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out_m4a)
    return j


def assemble(seg_ids, out_name):
    vlist = WORK / "_v.txt"; alist = WORK / "_a.txt"; total = 0
    vlist.write_text("".join(f"file '{(WORK / sid / 'v.mp4').resolve()}'\n" for sid in seg_ids))
    alist.write_text("".join(f"file '{(WORK / sid / 'a.wav').resolve()}'\n" for sid in seg_ids))
    base_v, base_a = WORK / "base_v.mp4", WORK / "base_a.wav"
    ff("-f", "concat", "-safe", "0", "-i", vlist, "-c", "copy", base_v)
    ff("-f", "concat", "-safe", "0", "-i", alist, "-c", "copy", base_a)
    with wave.open(str(base_a)) as w:
        total_s = w.getnframes() / w.getframerate()
    mixed = mix_tracks(base_a, WORK / "mix.wav", total_s)
    norm = WORK / "norm.m4a"
    j = loudnorm_two_pass(mixed, norm)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / out_name
    ff("-i", base_v, "-i", norm, "-map", "0:v", "-map", "1:a", "-c", "copy", "-movflags", "+faststart", "-shortest", out)
    return out, j


def main():
    if not WORDS and any(sg["kind"] == "talk" for sg in SEGS) and not any(f in sys.argv for f in ("--durations", "--plan")):
        sys.exit("This project has talking segments but no transcript yet. Transcribe the video first.")
    WORK.mkdir(parents=True, exist_ok=True)
    only = None
    for i, a in enumerate(sys.argv):
        if a == "--only":
            only = sys.argv[i + 1].split(",")
    if "--durations" in sys.argv:
        out = []; t = 0.0
        for sg in SEGS:
            n = seg_frames(sg); row = dict(id=sg["id"], kind=sg["kind"], start=round(t, 3), dur=round(n / FPS, 3))
            if sg["kind"] == "talk":
                a1, n0 = cut_range(sg["a"], sg["b"]); row["a1"] = round(a1, 3); row["b1"] = round(a1 + n0 / FPS, 3)
            out.append(row); t += n / FPS
        print(json.dumps(out)); return
    if "--plan" in sys.argv:
        rows = []
        for sg in SEGS:
            v, a = cache_state(sg); rows.append(dict(id=sg["id"], cached=v and a, video_cached=v, audio_cached=a))
        vids = all(r["video_cached"] for r in rows)
        rows.append(dict(id="__mix__", cached=assemble_cached(), video_cached=vids, audio_cached=True))
        print(json.dumps(rows)); return
    if "--fz-one" in sys.argv:
        i = sys.argv.index("--fz-one"); key, fi, fn, outp = sys.argv[i + 1:i + 5]
        tmp = Path(outp).parent / f"_fz_{os.getpid()}"; tmp.mkdir(parents=True, exist_ok=True)
        still = tmp / "still.png"; extract_still(ANN[key]["t"], still)
        freeze_frame((key, str(still), int(fi), int(fn), str(tmp)))
        shutil.move(str(tmp / f"f_{int(fi):04d}.jpg"), outp); shutil.rmtree(tmp); return
    if "--list" in sys.argv:
        tot = 0
        for s in SEGS:
            if s["kind"] == "talk":
                a1, n = cut_range(s["a"], s["b"]); ws = [w["text"] for w in WORDS if w["start"] >= a1 - 0.01 and w["end"] <= a1 + n / FPS + 0.01]
                print(f"{s['id']:5s} {a1:8.2f}-{a1 + n / FPS:8.2f} ({n / FPS:5.2f}s)  {' '.join(ws[:5])} ... {' '.join(ws[-4:])}")
                tot += n / FPS
            elif s["kind"] == "slow":
                print(f"{s['id']:5s} slow-mo {s['a']:.2f}  3.2s"); tot += 3.2
            elif s["kind"] == "reps":
                print(f"{s['id']:5s} reps {s['a']:.2f} {s['d']:.1f}s"); tot += s["d"]
            else:
                print(f"{s['id']:5s} {s['kind']}"); tot += 5.4 if s["kind"] == "hook" else 5.0
        print(f"estimated total ~{tot:.1f}s"); return
    if "--fz-preview" in sys.argv:
        prev = WORK / "fzprev"; prev.mkdir(parents=True, exist_ok=True); sheets = []
        for key in ANN:
            still = prev / f"still_{key}.png"
            if not still.exists():
                extract_still(ANN[key]["t"], still)
            freeze_frame((key, str(still), 42, 60, str(prev)))
            sheets.append(Image.open(prev / "f_0042.jpg").resize((540, 960)))
            shutil.copy(prev / "f_0042.jpg", prev / f"prev_{key}.jpg")
        cols = 4; rows = (len(sheets) + cols - 1) // cols
        sh = Image.new("RGB", (540 * cols, 960 * rows))
        for i, im in enumerate(sheets):
            sh.paste(im, ((i % cols) * 540, (i // cols) * 960))
        sh.save(prev / "sheet.jpg", quality=88); print("saved", prev / "sheet.jpg"); return
    ids = []
    for s in SEGS:
        ids.append(s["id"])
        if only and s["id"] not in only:
            continue
        v, a = cache_state(s); d = WORK / s["id"]
        if v and a:
            print(f"cached {s['id']}", flush=True); continue
        print(f"start {s['id']}", flush=True)
        if v and s["kind"] in ("talk", "reps", "clip"):           # audio-only change: skip the slow video render
            n = build_seg(s, only_audio=True)
        else:
            n = build_seg(s)
        (d / "v.sha").write_text(seg_hash(s)); (d / "a.sha").write_text(seg_audio_hash(s))
        print(f"built {s['id']:6s} {n / FPS:6.2f}s", flush=True)
    if not only or "--assemble" in sys.argv:
        out_name = "preview.mp4" if DRAFT else "final.mp4"
        if assemble_cached():
            print("start mix", flush=True); print("wrote", OUT_DIR / out_name, "| unchanged", flush=True)
        else:
            print("start mix", flush=True)
            out, j = assemble(ids, out_name)
            (WORK / "assemble.sha").write_text(assemble_hash())
            print("wrote", out, "| measured loudness", j["input_i"], "LUFS", flush=True)


if __name__ == "__main__":
    main()
