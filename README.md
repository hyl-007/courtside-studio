# Courtside Studio

A local, CapCut-style vertical-video editor you run on your own Mac and drive by talking to Claude (or by hand, in the browser). Built for cutting fast, highlight/interview-style reels — timeline editing, word-level captions (including a "kinetic" pop-in style for repeated phrases), freeze frames with callout annotations, beat-synced cuts, multi-track picture-in-picture, and an ffmpeg render pipeline underneath.

This started as a tool for a youth basketball academy's social reels, then grew into a general-purpose vertical-video editor. Some internal names (`zba_cli.py`, `ZBA_*` environment variables, the "Zenith tracker" integration card) are leftovers from that — they're just naming, not tied to any specific business, and safe to rename or delete if you don't need them.

## What's here

- `app/build.py` — the render engine (Python + ffmpeg). Reads a project's edit decisions and produces the final MP4.
- `app/server.py` — a stdlib-only HTTP server: the browser editor's API, plus an optional Claude-assistant endpoint that edits your project by running one whitelisted CLI command at a time.
- `app/index.html` — the whole browser editor, one file, no build step.
- `app/zba_cli.py` — the single tool Claude (or you, from a terminal) uses to inspect and edit a project.
- `app/qa.py`, `app/plan_check.py`, `app/frame_at.py` — a quality-check scorer, a dry-run planner for cut lists, and a frame-extraction helper.
- `.claude/skills/hyl-studio-video-editing/` — a Claude Code skill documenting the render pipeline's real gotchas (caption-timing architecture, verification habits, common failure modes) so an agent working on this codebase doesn't have to rediscover them. Copy this folder into your own `~/.claude/skills/` to use it in any Claude Code session.

## Requirements

- macOS (developed and tested there; the ffmpeg/PIL pipeline should port to Linux with minor path changes)
- Python 3 with `numpy`, `Pillow` (`pip install numpy pillow`)
- `ffmpeg`/`ffprobe` on your `PATH` (`brew install ffmpeg`)
- A word-level transcript per source video (this repo doesn't include a transcription step — bring your own, e.g. via ElevenLabs Scribe or Whisper, in the `{"words": [{"type":"word","start":..,"end":..,"text":..}, ...]}` shape `build.py` expects)

## Getting started

```bash
cd app
python3 server.py
# open http://localhost:8765
```

A project lives in `library/<slug>/`:
```
library/<slug>/
  meta.json          # name, source video path, duration
  project.json        # every edit decision — segments, captions, freeze frames, settings
  source/              # your raw footage
  transcripts/          # word-level transcript JSON, same filename stem as the source video
  work/ / work_draft/   # render cache (safe to delete)
  out/final.mp4          # the render
```

Replace `brand/logo.png` with your own watermark (any PNG works) — the render pipeline expects a file at that path.

## Using the Claude assistant

The browser editor has a "✦ Claude" panel that runs a separate `claude -p` process, restricted to one command at a time via `app/zba_cli.py`, editing the same `project.json` you see live in the timeline. It needs the `claude` CLI installed and on your `PATH`.

## Notes

- Every render is verified before it's called done: check `ffprobe`'s reported duration/size, and `ffmpeg -v error -i out.mp4 -f null -` must exit clean. A render can report success and still be silently corrupt if two renders write to the same `work/` folder at once — never start a second render while one is running.
- `BUILD_REV` near the top of `build.py` must be bumped whenever you change rendering *logic* (not just project data), or the segment-cache will keep serving stale output.
- See the skill file for the deeper caption-timing gotchas — that's the part of this codebase most likely to bite you.
