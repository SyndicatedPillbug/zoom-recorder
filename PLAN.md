# Hardening plan (post-incident)

Context: on 2026-09-16, a well-intentioned but reckless move (running a third-party
audio "enhancer" with its output directory pointed at the *same* folder as the
source file) silently zeroed out the only copy of a 32-minute recording. No
backups existed. The raw 5-minute capture segments had already been deleted by
the recorder's own cleanup step, hours earlier, per its normal (successful)
operation. Net result: an interview's audio is permanently gone.

None of that was the recorder's fault directly, but the recorder made total
loss *possible* by not keeping a second copy of anything, ever. This plan
closes that gap first, then addresses the other asks (device-change survival,
self-checking, easy activation).

Decisions already made (per Odin, 2026-09-16):
- **Activation**: one-keystroke manual start/stop. No background daemon that
  watches for calls and auto-arms itself — a persistent process with mic
  access and silent file writes is exactly the shape of thing Falcon (or any
  EDR) is tuned to flag, and it's a consent risk if it ever records a call it
  shouldn't. Simpler is safer here.
- **Versioning**: immutable originals + a checksum manifest. Not git-annex,
  not restic/Borg — just: write once, chmod to read-only, log a hash. Lowest
  complexity that still makes today's failure mode structurally impossible.

## Where the current code already stands

The in-progress `zoom_record.py` rewrite (not yet committed) already covers a
surprising amount of this well:

- Segment-based recording (crash only costs the current segment).
- Automatic mic selection, ranked by quality, preferring whichever device is
  actually delivering signal.
- Continuous health probing of the **active** mic every `--chunk-seconds`,
  with automatic failover to another device after `--fail-threshold`
  consecutive silent checks — this already handles "the input changed mid-call."
- Periodic probing of **inactive** candidates too, so failover has fresh data
  to decide from.
- System/loopback input is also monitored and re-selected if it drops.
- `capture.log` records every probe, every switch, every decision.
- `--self-test` verifies the loopback path end-to-end before you ever rely on it.

So "survive changing inputs/outputs mid-call" and "self-check that audio is
actually being captured" are largely **already solved** by the rewrite. What's
missing is what happens *after* recording stops, and a couple of structural
gaps that made today's incident possible.

## Gap 1 (critical): segments get deleted, originals aren't protected

`main()`'s `finally` block does:
```python
shutil.rmtree(workdir, ignore_errors=True)
```
right after merging — every single run, success or not. And nothing ever
stops another program (or a future version of this script, or a typo) from
opening `recording.wav` for writing again.

**Fix:**
1. Never delete segments. After a successful, *verified* merge (see Gap 2),
   move — don't delete — the segment directory into
   `~/ZoomRecordings/<date>/.segments/`. Disk is cheap; a lost interview isn't.
   Segments are also useful because merge is a lossless `-c copy` concat —
   they're a bit-exact fallback if the merge itself is ever bad.
2. Immediately after a verified merge, `os.chmod(merged, 0o444)`. Read-only,
   before any transcription or downstream tool ever touches it.
3. Every tool downstream (transcription, enhancement, whatever comes next)
   must be pointed at a **copy** in a `derived/` subfolder, never the original
   path. Cheap guard: a small wrapper that refuses to run if the requested
   output path resolves inside the dated recording folder itself outside
   `derived/`.

## Gap 2: nothing checks the recording is actually good before trusting it

Today's `transcript.txt` silently degrading into `[Pause]` for 90% of a file
was itself a warning sign that sat unnoticed for weeks. The recorder should
catch that itself, immediately.

**Fix — add a `verify_recording()` pass right after merge, before archiving
segments or chmod'ing:**
- Duration sanity check: merged duration ≈ sum of segment durations (catches
  a broken concat).
- Full-file `volumedetect` + a coarse silence scan (`silencedetect` at a
  couple of thresholds): compute `% of file with detectable signal`.
- Write a one-line summary to `capture.log` and print it on exit, e.g.:
  `Coverage: 68% of recording has signal above -60dB. 3 stretches (>60s) below threshold — see capture.log.`
- If coverage is suspiciously low (say <50%), this is exactly the kind of
  thing that should be **loud**, not a thing you discover during transcription
  three weeks later — flash a terminal bell / macOS notification.

## Gap 3: mic and system audio are pre-mixed — should be separate tracks

The current pipeline does `amix=inputs=2` immediately, producing one blended
mono-ish signal. That decision is exactly what made today's recovery effort so
hard: once two voices are summed together, you can't boost one speaker without
boosting the noise floor under the *other* one identically — there's no way to
separately gain, EQ, or denoise "my mic" vs "their voice through the system
loopback" after the fact.

**Fix:** write mic and system to two channels of a stereo file instead of
mixing to mono (`-filter_complex "[0:a]pan=stereo|c0=c0|c1=0[m];[1:a]pan=stereo|c0=0|c1=c0[s];[m][s]amix=inputs=2:duration=longest[a]"`
or simpler: just don't mix at all — write **two mono files per segment**
(`seg_00000_mic.wav`, `seg_00000_sys.wav`) and merge each track independently.
Two mono files is simpler to reason about and means any future
enhancement/leveling pass can be applied per-source instead of on an
already-blended signal. Downstream transcription can still mix them for a
single transcript pass, but the untouched dual tracks stay archived.

## Gap 4: no versioning / manifest

**Fix — dead simple, per the "immutable + manifest" decision:**
- On successful verify: append one line to `~/ZoomRecordings/manifest.jsonl`:
  `{"date": "...", "path": "...", "sha256": "...", "duration_s": ..., "coverage_pct": ..., "segments": N}`
- That's it. No git, no LFS, no backup daemon. A flat, append-only, human-
  readable audit log that answers "does this file still match what was
  originally recorded" with one `sha256sum` check, forever.
- Optional, cheap extra: since the manifest itself is tiny text, *that* can
  live in an actual git repo (`~/ZoomRecordings/.git`, manifest-only, `.gitignore`
  the actual audio) — free history of "what was recorded when," basically zero
  cost since it's never more than a few KB.

## Gap 5: one-keystroke activation

No background watcher (per the decision above). Two reasonable options,
roughly equal effort:

- **A global hotkey via Hammerspoon** (or `skhd`) bound to a small shell
  function: press once to start (spawns the recorder script detached with a
  pidfile), press again to stop (sends SIGINT to the pidfile, which the
  existing `trap`/`signal.signal` handlers already turn into a clean merge).
  No persistent process exists except while actively recording.
- **A tiny menu-bar toggle** using `rumps` (a ~50-line Python menu-bar app):
  shows a mic icon, click to start/stop, turns red while recording so it's
  never ambiguous whether it's on — and is a normal, visible menu-bar app the
  whole time, not a hidden background service, which matters for the
  Falcon/policy question.

Either is fine; the menu-bar option is slightly friendlier (visible state,
no memorizing a key combo) and doubles as the "is this actually recording
right now" indicator you'd otherwise have to check a log for.

## Suggested order of work

1. **Now, before anything else touches this script again:** Gap 1 (stop
   deleting segments, chmod originals read-only) and Gap 2 (verify + coverage
   report). This is the direct fix for what happened today and is a small,
   contained change.
2. Gap 4 (manifest) — trivial once Gap 2's verify step exists, since it's
   computing most of the needed data already.
3. Gap 3 (separate mic/system tracks) — slightly bigger change to the ffmpeg
   command construction, worth doing before the next important recording.
4. Gap 5 (hotkey or menu-bar launcher) — independent of the others, do
   whenever it's convenient.

Items 1–2 are the ones I'd actually implement first; everything else is
valuable but not "prevents a repeat of today" critical.

---

# Phase 2: Live transcript + AI answer HUD

Goal: a small two-column window during recording — live transcript on the left,
generative bullet answers/talking points on the right, grounded in both the
conversation and a background database of `.md` files. Opt-in via `--live`.

## Design decisions

- **Isolation above all.** The HUD is a separate ffmpeg tap plus separate
  threads. It never touches the capture path, the originals, or verification.
  If it fails to start (or crashes), recording proceeds exactly as before.
  Implemented under `hud/`; `zoom_record.py` only calls `LiveSession.start()` /
  `.stop()`.
- **Local web UI, not a native window.** The recorder's main thread is already
  owned by the monitor loop / rumps. A `127.0.0.1` HTTP + SSE page avoids any
  GUI-thread contention and gives the two-column layout for free.
- **Pluggable providers.** Groq is the default for both STT and answers
  (fastest + cheapest, usable free tier). OpenRouter, OpenAI and Ollama are
  wired in through the same OpenAI-compatible client; `local` STT uses
  whisper.cpp. The HUD header always shows what is leaving the machine.
- **Budget-aware answers.** Detected questions use the stronger model; rolling
  talking points use the cheap, high-quota model and are dropped first when the
  daily token budget runs low. This is what makes an aggressive (~35 s) cadence
  viable on a free tier.
- **KB is local.** Embeddings (`sentence-transformers`) are computed on-device
  and cached; only the handful of retrieved snippets join the prompt.
- **Partial diarization via channels.** Mic and system/loopback are separate
  captures, so each is transcribed independently and labelled (`--self-name` /
  `--remote-name`, default "You" / "Others"). Exact for two parties with no
  diarization model; multiple remote speakers share the loopback label.
  `--no-speaker-labels` restores the single mixed stream.

## Consent / privacy note

This is the first feature that can send data off the machine. With a remote STT
backend the **audio** leaves; with answers enabled the **transcript text** (and
relevant snippets from the user's own `.md` files) is sent to the answer
provider. Both are opt-in, both are surfaced live in the HUD, and
`--live-no-answers` (or a local STT + Ollama combination) keeps everything
on-device. The no-background-daemon decision from Phase 1 still holds: the HUD
exists only while a recording is in progress.

## Order of work

1. `hud/` scaffolding: config, OpenAI-compatible client, event state, budget.
2. HTTP/SSE server + two-column page.
3. Isolated live tap + chunked STT.
4. Local markdown knowledge base + embeddings.
5. Answer engine (question detection, rolling points, provider fallback).
6. Recorder/menu-bar integration + docs.
7. Tests, plus a check that `--live` off is byte-identical to before.

All of the above is implemented; see `hud/` and `tests/test_hud.py`.

Menu bar: the HUD gets its own distinct options (`Start with Live HUD`,
`Open Live HUD…`) rather than reusing the plain recording toggle, with a
three-state icon (🎙 idle / 🔴 REC recording / 🧠 HUD recording + live window).
The state logic is kept in `hud/menu_state.py` so it is testable without
importing `rumps`.
