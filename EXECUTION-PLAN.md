# Execution Plan

Status: active  
Created: 2026-09-22  
Reference: [ROADMAP.md](ROADMAP.md)  
Starting release: `v2.7`

This is the step-by-step implementation plan for the roadmap. Work proceeds from measurement and
reliability toward optimization and UX. A phase is not complete because code exists; it is complete
only when its exit gate is met and recorded.

## Non-negotiable quality gates

Every code phase must pass all of these before the next phase begins:

1. `python3 -m unittest discover -s tests` is green.
2. `git diff --check` is clean.
3. The affected behavior has a regression test at its point of consumption.
4. The change is exercised once with a deterministic fixture or replay.
5. The change does not make provisional text authoritative.
6. The change does not allow retrieval, diarization, talking points, or provider work to block
   capture or final transcription.
7. New metrics include their clock definition, sample count, and null/unknown behavior.

## Measurement contract

All benchmark output must identify:

- fixture and reference version;
- model and quantization;
- hardware/backend;
- window, interval, overlap, VAD, and queue settings;
- audio duration and speech duration when known;
- final WER, substitutions, deletions, and insertions;
- interim stable-prefix WER where meaningful;
- first stable publication from speech-start and from completed-window clocks;
- finalization latency p50/p95;
- inference latency p50/p95;
- queue depth, drops, stale revisions, and writeback failures;
- process/memory/thermal observations when available.

Unknown measurements must be represented as `null` or `unknown`, never as zero.

## Phase 0 — Baseline package and benchmark manifest

### Step 0.1 — Capture the reference state — complete

- Keep `v2.7` as the reference tag.
- Record the current full-suite count and exact command in the benchmark notes.
- Record the known AMI slice results: Turbo 20.0% WER, `base.en` 21.3% WER.
- Keep the 4-second interim default as the control condition.

Gate: a new checkout can identify the reference version and reproduce the control command. **Met.**

### Step 0.2 — Add a versioned fixture manifest — complete

Create a small JSON manifest format with fixture path, reference path, duration, speech bounds,
optional speaker/channel metadata, and expected use. Start with the existing AMI slice and add
available silence/noise and synthetic fixtures. Do not pretend a fixture has speaker labels or
timestamps when it does not.

Gate: the manifest loader rejects missing paths and malformed references with actionable errors;
it loads every checked-in fixture without network access. **Met.**

### Step 0.3 — Add benchmark result persistence — complete

Make the benchmark write one JSON result per run and a compact summary suitable for comparing
models/settings. Preserve the complete configuration used to generate it.

Gate: two runs with different windows/models cannot overwrite one another and can be compared by
fixture, model, and settings. **Met.** `hud.benchmark_suite` now provides manifest-driven
comparison output with explicit completed/skipped counts. Corpus coverage remains an open accuracy
gate until more speech fixtures are provisioned.

### Step 0.4 — Define initial numeric gates — complete

Use these as provisional engineering gates until the expanded corpus supplies better thresholds:

- final Turbo WER: no more than 2 percentage points worse than the v2.7 control on the same fixture;
- no final queue drops in a 60-second paced replay;
- no provisional queue drops caused by unbounded backlog;
- first stable publication: report p50/p95, with no accuracy gate until at least three speech
  fixtures exist;
- benchmark process completes without an unhandled exception.

Gate: every future benchmark report labels these as provisional thresholds. **Met.**

## Phase 1 — End-to-end lifecycle harness

### Step 1.1 — Build an audio-file session runner — complete

Feed a known PCM fixture through the same capture/STT/session interfaces used by the live path.
Exercise interim events, final events, answer gating, writeback, identity finalization, and stop.
Use a fake answer provider in this harness so network timing is not confused with lifecycle
correctness.

Gate: one command produces a run directory containing event trace, transcript, writeback, identity,
and summary artifacts. **Met by `hud.lifecycle`.**

### Step 1.2 — Assert finality and ordering — complete

Assert that provisional events are visible but absent from authoritative transcript/writeback,
that every final boundary is preserved, and that shutdown drains capture/STT before summary and
identity persistence.

Gate: tests fail if a provisional suffix appears in canonical transcript or if summary starts
before final STT drain. **Met by lifecycle and shutdown-order tests.**

### Step 1.3 — Exercise failures

Inject provider timeout, local STT error, writeback I/O error, permission error, forced stop, and
restart. Confirm the recorder keeps the durable artifacts it can safely keep and reports the exact
degraded component.

Gate: 100 repeated normal runs plus 20 injected-failure runs complete without an orphan process,
deadlock, corrupt JSON, or unrecoverable next start.

Current status: **100/100 normal deterministic lifecycle runs passed, 20/20 injected provider,
STT, writeback, and permission runs preserved durable artifacts, and the process-level forced-stop
plus restart check passed with two distinct persisted sessions.** Phase 1.3 is complete.

The real-audio replay runner is now available as `hud.e2e_audio`; it uses the production
audio-file source and local STT path, while the deterministic harness remains the fast control.
The provisioned 30-second AMI replay completed with Turbo, zero dropped chunks, and a successful
additional transcript writeback. The runner also creates its output directory before session
startup, so identity and diagnostic persistence cannot race a missing path.

### Step 1.4 — Add lifecycle observability — complete

Add run ID, stage timestamps, stage duration, queue counters, and shutdown reason to the redacted
diagnostic export.

Gate: every lifecycle failure can be located in one stage without reading raw secrets or audio.
**Met for shutdown stages and total duration; injected-failure stage labeling remains open.**

## Phase 2 — Transcription optimization

### Step 2.1 — Correct the clocks — complete

Separate audio-time, capture-time, inference-time, stable-publication-time, and finalization-time.
Measure first stable word relative to the first matching reference word where timestamps permit.

Gate: latency metrics explain exactly what “latency” means and do not use submission time as a
proxy for speech start. **Met for live transcript events; speech-start alignment still requires
the expanded timestamped corpus.**

### Step 2.2 — Tune local agreement

Test local-agreement thresholds, overlap, phrase flush, and 3/4-second windows against the
manifest. Keep Turbo final and the 4-second interim setting as the control.

Gate: a candidate wins only if it improves stable publication latency by at least 15% while staying
within the final-WER and first-word gates and producing no queue growth.

Current status: the 3-second candidate was rejected on the AMI control (wrong first stable word,
34 committed words, 62.67% stable-only WER) versus the 4-second control (correct first word, 44
committed words, 49.33% stable-only WER). The 4-second default remains in force.

### Step 2.2a — Add evidence-gated final publication — complete

Final local windows now carry VAD speech-activity coverage into the hallucination gate. Borderline
short hypotheses are held for one neighboring final window; matching text is then published, while
unsupported one-off hypotheses are discarded. Rejection reasons and confidence samples are being
added to the redacted live diagnostics and rolling benchmark output. The adversarial fixture
manifest now covers silence, noise, hum, harmonic music, and sparse clicks; the no-pace local
benchmark completed 5/5 available non-speech fixtures with zero committed words and zero skipped
requests, while the AMI control remained at 49.33% stable-only WER with 33 confidence samples.

Exit gate: the full suite remains green, provisional text remains non-authoritative, and the hold
is bounded to one final window. **Met in v2.8.23.**

### Step 2.3 — Compare model candidates

Benchmark `base.en`, `small.en` if installed, and Turbo under identical settings. Investigate
Core ML/ANE or MLX/faster-whisper only after the control path is measured and only as a separately
reversible backend.

Gate: no backend becomes default without a written speed/accuracy/memory comparison and a green
replay.

## Phase 3 — Answer latency and evidence

### Step 3.1 — Add one trace per authoritative boundary — complete

Measure capture-to-final, retrieval, prompt assembly, prompt tokens, provider queue, TTFT, usable
answer, and talking-point completion.

Gate: a slow answer can be attributed to a specific stage with p50/p95 values. **Met for answer
queue/start/assembly/provider-complete/final states; `hud.trace_report` now aggregates those
boundaries across long replays, while UI presentation remains open.**

### Step 3.2 — Protect direct answers

Keep direct answer work ahead of talking points, summaries, and memory. Reconstruct the active
question only from the newest authoritative boundary, retaining the prior question until the new
one is complete enough.

Gate: talking-point work never increases direct-answer queue wait in a controlled stress test.

### Step 3.3 — Bound and cache context

Deduplicate evidence, cap transcript/context tokens, and cache stable prompt prefixes. Show source
and evidence status in the answer event.

Gate: prompt size and assembly time remain below configured caps in a long meeting replay.

Current status: prompt character budgets, retrieval metrics, provider timing, and a provider-free
long-replay aggregation report are available. Stable prompt-prefix caching and a measured long-
replay cap remain open.

## Phase 4 — Obsidian retrieval

### Step 4.1 — Harden permissions and indexing

Make TCC/permission failures actionable. Index incrementally by file identity and modification
time, preserving headings, tags, links, and provenance.

Gate: a large-vault index can resume after interruption and never blocks capture.

### Step 4.2 — Add hybrid ranking and budgets

Use lexical candidates first, then optional semantic reranking with recency/tag/link/current-topic
signals. Cap files, chunks, characters, and prompt tokens.

Gate: every retrieved chunk carries file/heading provenance and retrieval stays within its latency
budget; full-vault prompt insertion is impossible.

Current status: the merged static/live retrieval path now enforces `kb.max_chars` (6,000 by
default), preserves Obsidian frontmatter and wikilinks, applies a bounded metadata reranking
signal, records retrieval timing/candidate/selection counts in answer traces, supports the
optional `kb.scope_tags` static-vault filter, and uses a conservative file-stat fast path before
falling back to content fingerprints. The deterministic `hud.kb_benchmark` is available;
resumable mid-build checkpoints are covered; target-hardware latency measurements and permission
recovery remain open.

## Phase 5 — Participant attribution

### Step 5.1 — Make manual identity edits first-class

Persist source label, display name, confidence, origin, timestamp, and correction history. Manual
labels must override inferred labels without rewriting confirmed transcript text.

Gate: edits survive reload, replay, writeback, and session finalization.

### Step 5.2 — Add background reconciliation

Run diarization/reconciliation after stable boundaries, isolated from capture/STT. Cache opt-in voice
profiles and allow deletion.

Gate: disabling diarization or failing its worker changes no capture, final text, answer, or
writeback behavior.

Current status: post-call diarization is isolated and failure-safe, with optional reusable voice
profiles. Derived output now includes non-authoritative quality diagnostics for unknown/generic/
manual/profile attribution rates and processing real-time factor. Live diarization remains off the
critical path.

### Step 5.3 — Evaluate post-call quality

Use labeled two-speaker fixtures to measure speaker error rate, unknown rate, correction rate, and
added latency. Do not enable heavy live diarization until those numbers justify it.

## Phase 6 — UX and operational hardening

### Step 6.1 — Build the control center

Expose start/stop, audio sources, model/backend health, transcript folder, additional writeback
folder, Obsidian folder, provider mode, diarization toggle, and performance preset without flags.

Gate: a clean install/configuration can start a fixture run without terminal intervention.

### Step 6.2 — Make state legible

Distinguish provisional, stable, final, inferred, manually confirmed, saved, and failed states.
Provide one-click redacted diagnostics.

Gate: a user can identify what is trustworthy and what needs action from the UI alone.

Current status: the Control Center now provides a one-click redacted diagnostics export. It
includes actionable checks and safe configuration/status metadata while excluding API keys,
transcript text, and audio. Integrated permission recovery remains open.

### Step 6.3 — Test adverse environments

Cover sleep/wake, device changes, network loss, disk full, permission changes, provider rate
limits, and repeated start/stop.

Gate: each adverse case has either automatic recovery or a specific user action and never silently
loses authoritative transcript data.

## Phase 7 — Privacy and release readiness

- Verify local-only mode end-to-end.
- Store secrets securely and redact diagnostics.
- Document retention/deletion for recordings, transcripts, indexes, embeddings, memory, and voice
  profiles.
- Add consent/recording state and cloud-use indicators.

Gate: a privacy-sensitive session can run without cloud services, and every persisted data class
has a documented deletion path.

Packaging, signing, notarization, installers, and cross-machine update automation remain deferred.

## Repeated plan review checklist

Before each phase starts, ask:

1. Is this phase dependent on a measurement that does not yet exist?
2. Does the proposed metric have a meaningful clock and a known sample count?
3. Could this change let provisional text become evidence or durable output?
4. Could a slow or failed optional worker block capture, STT, or shutdown?
5. Is the proposed threshold based on enough fixtures, or is it explicitly provisional?
6. Is there a regression test at the behavior's point of consumption?
7. Can the change be reversed without deleting user data or changing existing settings?
8. Does this belong in the current phase, or is it scope creep toward packaging?

Before a phase is marked complete, ask the same questions again and attach the actual benchmark,
test, and failure-injection results to the phase notes.

## Immediate execution order

1. [x] Add the fixture manifest and result persistence.
2. [x] Run and record the control benchmark in `BENCHMARK-BASELINE.md`.
3. [x] Build the controlled lifecycle harness.
4. [x] Run failure injection and repeated start/stop tests.
5. [x] Correct latency clocks and add the unified trace.
6. Only then tune windows, local agreement, models, retrieval, attribution, and UX.
