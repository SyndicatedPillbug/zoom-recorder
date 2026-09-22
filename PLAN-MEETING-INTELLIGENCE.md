# Meeting intelligence implementation plan

This plan covers the post-v2.2 reliability, latency, accuracy, retrieval, and
identity work requested for the local Zoom recorder. Distributable packaging,
signing, and notarization are intentionally excluded from this plan.

## Goals

- Keep audio capture independent from every slower downstream operation.
- Make interim speech visibly fast while keeping permanent evidence final and
  revision-safe.
- Answer only questions supported by the conversation or retrieved notes.
- Make a large Obsidian vault searchable without putting the whole vault into
  prompts.
- Preserve useful context as structured meeting memory, not only raw text.
- Give recordings, transcript files, and session folders meaningful identities
  derived from reliable metadata and transcript evidence.
- Make latency and accuracy measurable through deterministic replay fixtures.

## Delivery phases

### Phase A — identity and lifecycle safety (implemented)

Add a session identity record with a human-readable slug/title, start time,
participants when known, sources, model/provider configuration, and evidence
used to derive the title. Use it for session folder names and transcript
writeback filenames while preserving a collision-safe fallback and the existing
numeric/session artifacts. Reorder shutdown so capture and STT drain before
summary generation and final persistence.

### Phase B — transcript revisions and STT scheduling (implemented baseline)

Transcript events now carry source segment IDs, revisions and finality; a shared
inference gate prioritizes final chunks and drops stale interim work. The
 diagnostics now expose percentile latency, live lag, queue depth, and drop
 counters.

### Phase B.1 — adaptive local windows (in progress)

The local Turbo path now starts at the configured chunk target, retunes between
configurable minimum and maximum bounds, and retains a small overlap between
target-sized windows. Actual queue pressure is required before a window grows;
inference time by itself never adds capture-to-text latency. Phrase pauses may
still flush a shorter window, and final work remains ahead of provisional work.
Provisional admission now yields whenever final audio is queued or being
decoded; when the provisional queue is full, the newest draft replaces the
stale queued draft instead of being dropped. This keeps the authoritative lane
responsive while preserving the most current local draft for the HUD.

Real-time AMI replays now produce zero final queue drops and zero provisional
queue drops. On a dense 15-second speech sample, the original three-second
floor measured about 3.8 seconds p50 final latency; lowering the floor to 2.5
seconds measured about 2.4 seconds p50 and 2.5 seconds p95, with three final
inferences and no provisional drops. A speech-plus-pause sample measured about
2.2 seconds p50/p95 and flushed cleanly. The longer 45-second replay using the
new default produced zero final drops, zero provisional drops, nine final
inferences, and a 3.5-second settled target; final latency was 3.94 seconds
p50 and 4.79 seconds p95. This is bounded and materially better than an
unbounded backlog, but the p95 tail remains above the aspirational gate, so
this phase stays in progress. Overlap and phrase-flush tuning remain the next
likely levers before changing the answer scheduler.

### Phase C — question finalization and answer evidence (implemented baseline)

Interim question marks now wait for an explicit transcript boundary before an
answer is queued. Answer claims request evidence spans and are locally checked
against transcript/reference text; unsupported explicit claims are dropped and
their status is saved on the answer event.

### Phase D — retrieval and structured meeting memory (implemented baseline)

The KB now maintains a persistent SQLite FTS5 lexical side index where the
cache path is writable, retains frontmatter metadata, and uses lexical
candidates to bound large-vault semantic scans. A deterministic background
memory lane records decisions, commitments and numeric facts with exact
evidence. Tag/link scope filters remain a follow-up enhancement.

### Phase E — talking-point isolation and observability (implemented baseline)

Question answers and talking points now have separate workers and queues. A
shared provider gate gives questions priority while stale talking-point work is
dropped. The session exports non-secret diagnostics; percentile aggregation is
still a useful next tuning pass.

### Phase F — replay and regression harness (implemented baseline)

The dependency-free JSONL event format, replay command, event export, memory
replay, identity tests and stale-interim/question-boundary tests are in place.
Captured PCM benchmarking and a full shutdown-order integration fixture remain
future validation work.

## Verification

- Run the full non-GUI regression suite after each phase.
- Run `git diff --check`, Python import/syntax checks, and shell syntax checks.
- Run the doctor with loopback networking enabled.
- Benchmark at least one real local turbo replay and one synthetic stress case.
- Do not claim end-to-end live behavior until a controlled meeting or replay
  validates capture, transcript, answer, writeback, and shutdown together.

## Explicitly deferred

Distributable app packaging, signing, notarization, installer bundles, and
cross-machine release automation remain deferred until the local workflow is
polished and stable.
