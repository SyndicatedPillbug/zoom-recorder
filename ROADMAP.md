# Meeting Intelligence Roadmap

Status: active roadmap
Updated: 2026-09-23
Reference baseline: `v2.7`
Latest shipped tag: `v2.8.23`

This project is an app-agnostic meeting recorder and live conversation assistant. It must work
with Zoom, Meet, Teams, Jitsi, browser calls, phone audio, and locally played recordings without
depending on a provider-specific meeting API.

The roadmap deliberately postpones distributable packaging, signing, notarization, and
cross-machine installation. The priority is to make the current Mac implementation accurate,
fast, reliable, explainable, and easy to tune before packaging it.

## Current assessment

The most important foundations are now in place:

- Local Turbo is the final-transcript baseline on Apple Silicon.
- `base.en` is the interim model, with a conservative 4-second rolling window by default.
- Revision-safe evidence boundaries prevent provisional text from contaminating saved transcripts,
  retrieval, memory extraction, or answers.
- Stable-partial publication, writeback, Obsidian indexing, structured memory, identity editing,
  non-blocking local diarization, Groq fallback behavior, replay, and latency instrumentation are
  implemented and covered by deterministic lifecycle/replay tests.
- The current release line has a 297-test green regression suite, 100/100 normal lifecycle runs,
  20/20 injected-failure runs, forced-stop/restart coverage, and a manifest-driven benchmark
  package.
- Obsidian retrieval is hybrid and bounded: frontmatter/wikilinks are preserved, metadata can
  rerank results, optional tag scope is exposed in Settings, unchanged vaults use a file-stat fast
  path, interrupted indexing resumes from completed file caches, and the HUD shows retrieval time.
- The aligned AMI 50–80 second slice currently measures Turbo at 20.0% WER and `base.en` at
  21.3% WER. The rolling benchmark shows that a 2-second interim window produced a wrong first
  stable word while 4 seconds produced the correct first word with roughly 0.13 seconds from
  window completion to publication in the tested fixture.

The largest remaining risks are not simply model speed:

1. The benchmark corpus is too small to support aggressive tuning.
2. The first controlled capture-to-shutdown replay now passes with real audio, but the corpus still
   needs multiple durations, accents, noise conditions, and two-channel fixtures.
3. Long-replay answer aggregation is now measurable with `hud.trace_report`; active-question
   reconstruction quality and stable prompt-prefix caching still need optimization.
4. Participant quality still needs labeled two-speaker fixtures and speaker-error measurements;
   local attribution is enabled by default but remains off the critical transcription path.
5. Permission recovery, redacted one-click diagnostics, adverse-environment recovery, and local
   privacy/deletion verification need a final operational pass.

## Operating principles

- Final Turbo text is authoritative. Interim text is useful for display and anticipation only.
- No provisional text may trigger durable writeback, memory, retrieval, or an answer that is
  presented as evidence-grounded.
- The capture and final-transcription path must work without network access.
- Diarization and voice matching must never be allowed to stall capture or final transcription.
- Every optimization needs a before/after measurement on the same fixtures.
- A local, bounded context slice is better than placing the entire vault in every prompt.
- Manual corrections are authoritative and should improve future suggestions without silently
  rewriting already-corrected history.
- Recording consent, cloud use, secrets, and raw audio retention must be visible and controllable.

## Phase 0 — Freeze the baseline and make it reproducible

Goal: establish a trustworthy comparison point before more tuning.

Work:

- Preserve `v2.7` as the current reference point.
- Create a benchmark manifest covering clean speech, silence, music/noise, accents, fast speech,
  overlap, short questions, long answers, and two-channel audio where available.
- Store references with timestamps and speaker labels where known.
- Record model name, quantization, hardware path, window size, overlap, queue depth, and settings
  alongside every result.
- Keep one small fast regression fixture and one longer realistic fixture.

Exit criteria:

- One command produces machine-readable and human-readable benchmark output.
- Every later transcription change can report WER, stable-prefix latency, finalization latency,
  dropped/revised events, and CPU/memory use against the same manifest.
- The current 4-second interim default and Turbo final path remain reproducible.

## Phase 1 — End-to-end lifecycle reliability

Goal: prove the whole application behaves correctly when real audio flows through it.

Work:

- Build a controlled audio-file run that exercises capture, interim STT, Turbo finalization,
  evidence gating, dynamic question updates, answer generation, transcript writeback, shutdown,
  and restart.
- Verify that a crash, provider timeout, permissions failure, or interrupted shutdown leaves no
  corrupt canonical transcript and no orphan process/lock.
- Test repeated start/stop cycles and long runs with backpressure.
- Add an explicit run manifest and event trace so failures can be replayed.

Exit criteria:

- 100 repeated fixture runs complete without orphaned servers, unreleased audio devices, or
  corrupted writeback.
- Final transcript output contains no provisional suffixes.
- A forced interruption still produces a clearly marked recoverable partial artifact.
- Restarting after failure is safe without manual cleanup.

This phase precedes aggressive latency work because faster code that loses final events or leaves
the recorder wedged is a regression.

## Phase 2 — Transcription latency and accuracy

Goal: approach real-time words while protecting final accuracy.

Work, in order:

1. Improve measurement of stable-prefix latency: measure from captured speech and completed window,
   not only from submission time.
2. Tune local-agreement logic so a word is published as soon as it is stable across revisions,
   while withholding uncertain suffixes.
3. Test 3-second interim windows only on the expanded corpus. Keep 4 seconds as the default until
   the accuracy and first-word gates pass.
4. Tune overlap and pause/phrase flush behavior before changing models.
5. Compare `base.en`, `small.en`, and Turbo under identical conditions. Keep Turbo as the final
   baseline even if another model wins a narrow interim benchmark.
6. Measure warmup, steady-state inference, queue wait, memory, and thermal behavior separately.
7. Investigate Core ML/ANE or MLX/faster-whisper only as isolated experiments after the existing
   whisper.cpp path has a complete baseline.

Exit criteria:

- Interim publication has a defined p50/p95 target on speech-start and window-completion clocks.
- No new fixture shows unacceptable first-word or stable-prefix regressions.
- Final Turbo WER does not regress beyond the agreed tolerance.
- No queue growth or dropped audio occurs during a sustained realistic run.
- The application remains responsive while transcription is active.

Do not make Turbo provisional, lower the final window floor blindly, or replace whisper.cpp before
these measurements exist.

## Phase 3 — Answer pipeline and dynamic question reconstruction

Goal: make answers continuously better without letting talking points delay direct answers.

Work:

- Add one trace ID per committed evidence boundary.
- Measure separately:
  - capture-to-final-text latency;
  - context/retrieval time;
  - prompt assembly time and token count;
  - provider queue time;
  - time to first token;
  - time to usable answer;
  - talking-point generation time.
- Reconstruct the active question from the latest authoritative transcript window, retaining the
  previous question until the new one is sufficiently complete.
- Keep direct question answering on the critical path and run talking points, summaries, and
  memory extraction asynchronously.
- Add prompt compaction, bounded evidence windows, duplicate removal, and cacheable stable prefix
  context.
- Make provider fallback explicit: local evidence collection continues if Groq is unavailable;
  retry/backoff must never block capture.

Exit criteria:

- Talking-point work never delays a direct answer.
- Prompt size and assembly time are visible for every answer.
- Answers identify whether they are based on final transcript evidence, retrieved notes, or both.
- A provider failure degrades gracefully without losing transcript state.

## Phase 4 — Obsidian and long-term context

Goal: use the vault as a high-value evidence source without making every prompt slow or noisy.

Work:

- Make permission/TCC failures diagnosable and recoverable from the UI.
- Maintain an incremental local index with file identity, modified time, headings, tags, links,
  and chunk boundaries.
- Use hybrid retrieval: lexical/FTS5 first, semantic ranking where available, then recency,
  tags, links, and the current meeting topic as rerank signals.
- Preserve provenance for every result: file, heading, chunk, modification time, and why it was
  selected.
- Add retrieval budgets: maximum files, chunks, characters, and prompt tokens.
- Add meeting-scoped filters and an explicit way to exclude a vault folder.
- Cache embeddings/index state and re-index only changed files.
- Never send the entire vault or unbounded retrieved text to a provider.

Exit criteria:

- A large vault can be indexed incrementally without blocking capture or UI interaction.
- Retrieval returns a small, cited context set within a defined latency budget.
- Permission failures explain the exact fix instead of surfacing raw filesystem errors.
- Offline recording and final transcription still work with retrieval disabled.

Current status: bounded hybrid retrieval, optional tag scoping, provenance-preserving snippets,
file-stat cache validation, resumable per-file embedding checkpoints, and meeting-scoped source
snapshots are shipped. The main app can select next-meeting folders and reports `ready`,
`needs_index`, `no_markdown`, or `unavailable` before a call. The synthetic 6,000-chunk benchmark
records 116.7 ms p50 / 210.3 ms p95 unscoped and 30.2 ms p50 / 48.0 ms p95 with `enterprise` tag
scope on the benchmark host. Target-machine measurements, richer local semantic models, and
permission recovery remain open.

## Phase 5 — Participant attribution and diarization

Goal: improve speaker-aware transcripts for any call source while keeping the hot path safe.

Architecture:

- Lane 1: capture and transcription, always independent.
- Lane 2: optional live attribution using channel/source hints and cached voice profiles.
- Lane 3: background reconciliation after stable transcript boundaries.
- Lane 4: optional post-call diarization for the highest-quality final transcript.

Work:

- Keep generic source labels such as `local`, `remote`, `channel 1`, or `speaker 0`; do not bake
  Zoom assumptions into the data model.
- Make participant labels editable directly in the transcript UI.
- Treat manual labels as authoritative and persist correction history.
- Store voice-match features only with explicit opt-in, clear retention controls, and a way to
  delete them.
- Use prior labeled matches to improve future suggestions, but never silently relabel confirmed
  text.
- Reconcile diarization segments in the background and show confidence/unknown states.
- Compare post-call diarization quality and latency before considering live neural diarization.

Exit criteria:

- Attribution failure cannot delay or damage capture, final text, answers, or writeback.
- Manual participant edits survive reload and improve later suggestions.
- Diarization can be disabled per run.
- The transcript clearly distinguishes inferred, manually confirmed, and unknown speakers.

Current status: the attribution worker is post-call and failure-safe; local NeMo-Speech Sortformer
is now the default backend with Metal/Vulkan/ROCm/CPU device selection; manual labels persist
through session artifacts, replay, and writeback; reusable voice profiles are owner-only. Derived
diarization output includes unknown/generic/manual/profile rates and processing real-time factor.
The local persistent voice-embedding provider, labeled speaker-error fixtures, and Linux AMD
acceptance run remain open.

Do not put NeMo, WhisperX, or other heavy neural diarization in the live hot path until it has
passed the same lifecycle and latency gates as transcription.

## Phase 6 — UX and operator control

Goal: make the reliable path the easy path.

Work:

- Replace flag-driven setup with a compact control center: start/stop, audio source, transcript
  folder, optional Obsidian folder, model state, provider state, and permissions.
- Keep diarization on by default as a non-blocking feature, with a visible toggle and fallback.
- Clearly style provisional, stable, final, manually corrected, and provider-generated content.
- Add live health indicators for microphone/system audio, local model, writeback, retrieval, and
  provider availability.
- Add one-click diagnostics that can export a redacted run manifest and event trace.
- Give transcript files identifying names based on safe, confidence-gated meeting metadata, while
  retaining stable IDs for machine identity.
- Keep the additional transcript writeback destination separate from the existing overall save
  location, as already specified.

Exit criteria:

- A new user can configure and start a run without terminal flags.
- The UI communicates what is live, provisional, final, inferred, and saved.
- The common permission and model failures have actionable recovery paths.
- Performance settings are understandable presets, not unexplained tuning knobs.

Current status: Settings exposes audio, models, providers, writeback, Obsidian folders,
diarization, and retrieval budgets/scope. The HUD exposes transcript/answer state, editable
speaker labels, and technical latency details. Setup now exports one-click redacted diagnostics;
the Mac HUD now opens in a selectable native AppKit/WebKit Window or Glass HUD surface by default
with browser fallback and explicit best-effort capture-protection status; integrated permission
recovery remains open.

## Phase 6A — Persistent Meeting Workspace

Current status: Phase A implemented and green-tested; real-device acceptance remains, with the
remaining Phases B–E execution plan in [PLAN-WORKSPACE-PHASES-B-E.md](PLAN-WORKSPACE-PHASES-B-E.md).

Goal: turn the post-call experience into a durable control, display, access, playback, and editing
space without putting it on the live capture path.

Work:

- Keep a persistent session library after recording stops; the HUD ends, but the workspace remains
  available from the menu bar.
- Show integrity-aware session badges and make missing/silent system audio impossible to mistake
  for a complete recording.
- Add waveform-linked playback for mixed, microphone, and system tracks, with solo/mute, speed,
  looping, and click-to-seek transcript rows.
- Add a revisioned transcript editor with inline text edits, undo/redo, segment split/merge, and
  direct speaker-label editing.
- Allow creation, rename, merge, split, and `unknown` speaker assignments without rewriting raw
  audio or original transcript events.
- Re-run transcription, diarization, alignment, or summaries against selected time ranges as
  cancellable background jobs.
- Mark derived answers, summaries, embeddings, and writebacks stale when their source revision
  changes.
- Add library search by text, participant, title, date, health, folder, and review state.
- Export corrected Markdown, TXT, SRT/VTT, JSON, and transcript writeback from the selected
  revision.
- Keep raw audio immutable and preserve a complete edit/provenance history.

Reference plan: [PLAN-PERSISTENT-MEETING-WORKSPACE.md](PLAN-PERSISTENT-MEETING-WORKSPACE.md). The
open-source landscape and licensing findings are recorded in
[RESEARCH-OPEN-SOURCE-MEETING-WORKSPACES.md](RESEARCH-OPEN-SOURCE-MEETING-WORKSPACES.md).

Exit criteria:

- 100/100 stopped sessions remain openable after the live HUD closes.
- Speaker edits persist across reload, export, writeback, and reprocessing.
- Clicking a transcript row seeks to its audio within 100 ms on fixtures.
- No editor or derived job can modify an immutable original.
- A one-hour session remains usable without loading all PCM into UI memory.
- Missing/silent source health remains visible on every session page.

## Phase 7 — Privacy, security, and operational hardening

Goal: make the application safe to use in real meetings.

Work:

- Make recording/consent state explicit and visible.
- Keep API keys in the system keychain or equivalent secure store; never log them.
- Redact secrets and sensitive transcript content from diagnostics by default.
- Add clear controls for cloud transcription, cloud answers, raw audio retention, and voice-profile
  retention.
- Define data ownership and deletion behavior for transcript, index, embeddings, memory, and voice
  data.
- Test macOS permission changes, sleep/wake, device changes, network loss, and disk-full behavior.

Exit criteria:

- A privacy-sensitive run can remain fully local.
- Diagnostics are safe to share after redaction.
- Every persisted data class has a documented location, retention rule, and deletion path.

## Phase 8 — Packaging (intentionally deferred)

This is explicitly out of scope for the current roadmap pass:

- signed/notarized installers;
- bundled model distribution;
- automatic cross-machine setup;
- update channels and migration tooling;
- production support packaging.

Revisit only after the current Mac workflow is polished, benchmarked, and stable across real calls.

## Deferred product idea — private presenter view / hardware-separated HUD

Keep this separate from the current software-only overlay work. Some meeting applications may
force hardware-level full-monitor capture and ignore per-window capture-exclusion mechanisms. In
that case, a truly private HUD may require physical or compositor-level separation:

- a second physical display for the presenter HUD;
- a clean virtual presenter display shared with the meeting application while the private HUD stays
  on the physical display;
- a transparent optical or "clear glass" HUD overlay that is visible to the presenter but is not
  part of the captured framebuffer; or
- a future hardware/compositor integration using a display overlay plane, if its capture behavior
  can be demonstrated reliably on a supported device.

This is a product exploration, not an implementation commitment for the current release line.
The eventual investigation should compare portability, eye focus, setup friction, capture
reliability, accessibility, cost, and whether the approach works with Zoom, Teams, Meet, and
browser-based calls that capture the physical display directly.

## Decision gates

Before moving to the next major phase, use these gates:

| Area | Gate |
| --- | --- |
| Accuracy | Final Turbo does not regress on the reference corpus; interim gains are measured on more than one clip. |
| Latency | p50 and p95 are reported from meaningful clocks, including stable-word and usable-answer latency. |
| Reliability | No lost final events, corrupt writeback, orphaned process, or unrecoverable restart in the stress run. |
| Context | Retrieval is bounded, cited, incremental, and useful without sending the whole vault. |
| Attribution | Manual labels are authoritative; background diarization never blocks the hot path. |
| UX | Start, diagnose, stop, and recover are possible without terminal flags. |
| Privacy | Local-only mode and deletion controls are verified, not merely documented. |

## Recommended immediate sequence

1. Provision a broader timestamped speech and two-speaker corpus, then rerun interim/model gates
   without changing the Turbo final baseline.
2. [partially complete] Run the controlled end-to-end audio-file lifecycle harness through capture,
   local STT, writeback, shutdown, and restart; add an answer-enabled provider-free lane next.
3. [partially complete] Aggregate answer traces over long replays; measure active-question
   reconstruction, talking-point isolation, and provider fallback behavior next.
4. [partially complete] Add one-click redacted diagnostics; complete permission recovery in the
   control center.
5. Add labeled diarization fixtures and measure speaker error, unknown rate, corrections, and
   added post-call latency.
6. Run adverse-environment tests and local privacy/deletion verification.
7. Reassess packaging only after the above work is stable.

## Non-goals for this cycle

- Provider-specific meeting bots or API joins.
- Whole-vault prompt injection or unbounded context.
- Cloud-only transcription.
- Live heavy diarization that can stall capture.
- Sacrificing final accuracy for an attractive but unmeasured interim latency number.
- Distributable installation and release engineering.

## Definition of done for the current product stage

The current Mac build is ready for real personal use when it can run a complete meeting or replay
without manual cleanup, publish fast but clearly provisional text, finalize accurate Turbo text,
answer from bounded and cited evidence, write the transcript to both configured destinations,
retain manual participant corrections, recover from provider/network/permission failures, and
produce a redacted trace that explains any latency or quality problem.
