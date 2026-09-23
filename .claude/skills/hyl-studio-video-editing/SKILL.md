---
name: hyl-studio-video-editing
description: Use when editing, debugging, or rendering videos in this Studio — a local CapCut-style editor — including caption timing/animation issues, freeze frames, render pipeline errors, export quality settings, or planning cuts for a highlight/interview reel.
---

# Studio video editing

## Overview

This is a local, custom-built CapCut-style vertical-video editor. `app/build.py` is the ffmpeg render engine, `app/server.py` the stdlib HTTP API + browser editor backend, `app/index.html` the single-page editor UI. Each project lives in `library/<slug>/` with `project.json` (edit decisions), `meta.json`, `source/`, `transcripts/`, `work/` (render cache), `out/final.mp4`.

This is a living reference — extend it whenever a new session finds a new gotcha, not just when told to.

## Before touching anything

1. `ps aux | grep build.py` — never start a render while one is already running (they share the same `work/` dir and will corrupt each other's output).
2. Read the relevant slice of `project.json` directly (`python3 -c "import json; ..."`) rather than guessing at its shape.
3. Never re-render an already-delivered project unasked, and never revert a value the user set live in the editor.

## Render pipeline essentials

- **`BUILD_REV`** (top of `build.py`) must be bumped whenever rendering *logic* changes (not just project data) — otherwise the segment-hash cache silently serves stale output. The tell: log says `cached session` instead of `built session`.
- **Verification is mandatory before calling anything done**: `ffprobe -show_entries format=duration,size` (confirm expected length/size) + `ffmpeg -v error -i out.mp4 -f null -` (must print nothing and exit 0 — confirms no decode errors). A render can "succeed" (exit 0, file written) and still be silently corrupt if two renders wrote to `work/` concurrently.
- **W/H/FPS are per-project settings** (`settings.export_w/export_h/export_fps`, default 1080/1920/30 — byte-identical to legacy behavior when unset), not hardcoded. Only 4 known-good combos are valid: 1080×1920 @30/60, 1440×2560 @60/120. Prefer **60fps over 120fps** — 120fps H.264 is not reliably hardware-decoded by common players/phones, which manifests as apparent slow-motion + choppiness (looks like a timing bug, isn't one — verify with `nb_frames / r_frame_rate` against the real segment duration before assuming a bug). A project with an *explicit* `hook.card_frames` / `outro.frames` value must be rescaled by `new_fps/old_fps` when its export fps changes, or that segment's real duration silently changes.
- **Motion blur on fast action (a thrown ball, a sprint) is a capture-time property** (shutter speed/exposure), not something resolution, frame rate, or re-encoding can fix. Check the *original* source file before assuming the render pipeline is at fault.
- Debugging technique used throughout this project: `ZBA_PDIR=<project dir> python3 -c "import build; ..."` to introspect the pipeline's internal state directly (word timing, chunk boundaries, hash values) rather than only re-rendering and eyeballing the result.

## Caption chunking & timing (the deepest, most bug-prone part)

- `apply_edits()` keeps a hidden (edited-to-`""`) word **in place** with empty text rather than removing it — removing it would lose its real timing, and the client's own `capGroups()` already works this way. Client and server must agree on what a caption's "last word" is, or a duration you set by eye in the editor renders differently.
- Chunk boundaries (where one caption card ends and the next begins) must come from the **original spoken words**, never from post-edit text — otherwise editing/hiding a word silently merges its caption into a neighbor. `build_ass` already builds chunks before applying edits; `build_intense_ass` needed an explicit `RAW_SENT_END` set (words that *originally* ended a sentence) captured before edits are applied.
- That fallback must only fire for a **hidden** word (`text == ""`), never a word edited to *replacement* text — a word edited to new content is the user authoring on purpose, and its own punctuation (or lack of it) is the real signal. Using the raw fallback unconditionally silently re-splits deliberate multi-word reconstructions (e.g. editing several original ASR word slots into one new sentence) back into fragments.
- A ms-keyed lookup (`_wms(w)` = rounded start-ms) is ambiguous when two different words share an identical ASR timestamp (a real, not-rare occurrence on rapid speech) — drop the colliding key entirely rather than guessing which word it means (`Counter`-based collision check).
- **Repeat pyramids** (`_runs()` detects "get back, get back, get back" style repeats → escalating pop/tilt/color per repeat): a crammed multi-word edit (typing several words into one slot) changes that slot's token and can silently break `_runs()`'s pattern-continuation match, dropping a genuine 3rd+ repeat out of the pyramid (renders as flat, unanimated text instead). If a repeat "isn't animated, it's just the word", check whether an edit broke the token match — the fix is often just removing the edit, not new code.
- **Timing a rapid repeat's pop-in**: don't delay a later repeat's pop to avoid visual collision — that desyncs it from when it's actually spoken (worse than the collision). Instead keep every pop at its real spoken moment and *compress that word's own animation duration* to fit whatever gap is really there before the next word is due (see `pyramid_anim_ms` in `build_intense_ass`).
- **`anim_floor`** (guarantees a caption's last word gets enough on-screen time for its pop-in to be visible, even with a bad/zero-duration ASR timestamp) must (a) only apply to the *automatic* timing case, never override an explicit `caption_timing` entry someone set on purpose — even a short one, and (b) be capped at a small max overlap with the *next* caption (≈0.15s), not allowed to push out arbitrarily — two full captions visibly overlapping reads worse than one word rendering a little dim.
- After any chunking-logic change, run a **regression sweep across every project** (`build_ass`/`build_intense_ass` on every talk segment, per project) — not just the one you're fixing. This codebase keeps a same-style `WARN cap:` guard baked into both functions that flags a card that may have swallowed a later sentence; treat any new warning as a real signal to investigate, and confirm existing warnings clear after a fix, not just that no new ones appear.

## Short-form vertical editing conventions (researched, cross-check before assuming)

- **Hook**: open on the most interesting frame/line, not a logo or slow build — viewers decide to keep watching in 1-2s.
- **Pacing (sports highlights)**: 3-7s per play, simple straight cuts over flashy transitions (a spin/zoom transition draws attention to the edit, not the play), alternate fast and slow plays so it doesn't fatigue, cut on the music's beat (this Studio's own beat-snap timeline feature exists for exactly this).
- **Captions**: short bursts easier to read than full sentences; keep clear of the bottom ~15-20% (TikTok's action bar / Reels' UI) and the very top (username overlay) — center-to-upper-third is safest.
- **Audio levels**: dialogue around -10 to -6dB, music ducked under it around -20 to -16dB.
- **Spec**: 1080×1920 is the standard almost everywhere; 30 or 60fps, not higher (see render pipeline notes above on why 120fps backfires).
- Sources: [Short-Form Video Pacing Guide](https://shortzly.com/blog/short-form-video-pacing-editing-guide), [How To Edit Sports Videos](https://insideeditors.com/how-to-edit-sports-videos/), [Format Clips for TikTok/Reels/Shorts](https://captions.ai/help/guides/creators/format-for-platforms), [How to Edit Short-Form Video](https://flowshorts.app/blog/how-to-edit-short-form-video).

## Common mistakes (this project's own history)

| Symptom | Real cause |
|---|---|
| "Caption ends too fast" | Client/server disagreed on the last word after an edit, or an explicit `caption_timing` override is being silently clamped |
| "Repeat caption not smooth" | Fixed-delay stagger fighting real speech rhythm — compress the animation instead of delaying the pop |
| Two captions visibly overlap | `anim_floor` pushing past the next caption's start with no cap |
| A caption merged into the next, unrelated sentence | Chunking ran on edited text instead of raw, or the raw-punctuation fallback fired on a replacement-text edit |
| "Video looks slow motion" / still choppy | Almost certainly 120fps H.264 hardware-decode limits, not a render timing bug — verify frame math first |
| Ball/fast motion still blurry after every fix | Check the *original* source footage — motion blur is baked in at capture time |
| Render looks corrupted/wrong mid-edit | Two renders (yours + your own browser-triggered one) wrote to `work/` at the same time |
