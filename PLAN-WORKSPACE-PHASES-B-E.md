# Meeting Workspace — Remaining Implementation Plan

Status: planned after Phase A implementation  
Created: 2026-09-23  
Parent: [PLAN-PERSISTENT-MEETING-WORKSPACE.md](PLAN-PERSISTENT-MEETING-WORKSPACE.md)

This plan covers the remaining larger pieces after the persistent workspace foundation. The live
capture path remains protected: every phase consumes immutable session artifacts or isolated
background jobs and must never make recording, final transcription, or emergency alerts wait.

## Non-negotiable invariants

1. Raw WAV files, raw segments, capture logs, integrity reports, and original transcript events
   are immutable evidence.
2. User edits are revisioned interpretation data under `derived/`.
3. Every summary, answer, embedding, writeback, or cross-meeting result records its source
   transcript revision and model/configuration.
4. A missing or silent source is visible in every relevant UI and export.
5. Every long-running job is cancellable, resumable, bounded in memory, and isolated from capture.
6. No source file or derived artifact is served outside the configured recordings root.

## Phase B — Waveform-linked playback

### Goal

Make the workspace a reliable audio-review tool for long recordings and partial captures.

### Work sequence

1. Add a lightweight waveform-peak generator that reads WAV headers/chunks incrementally and writes
   `derived/waveform_<track>.json` with source checksum, duration, bucket size, and min/max/RMS data.
2. Add a timeline component that renders peaks without loading PCM into the browser.
3. Link each transcript row to its `start_s`/`end_s`; clicking a row seeks every active player.
4. Add mixed/microphone/system track selection, solo/mute, playback speed, loop range, and
   keyboard shortcuts.
5. Draw integrity gaps, silent intervals, unavailable tracks, and unaligned transcript rows on
   the same timeline.
6. Keep the existing HTTP range endpoint as the only browser audio transport; do not introduce a
   full-file endpoint.

### Measurable gates

- A 60-minute fixture renders in under 1 second after cached peaks exist.
- Browser memory does not increase by more than 100 MB when opening a one-hour session.
- Seeking from a transcript row lands within 100 ms of the row start on 20/20 fixtures.
- All three tracks remain independently playable where present.
- A silent system track is labeled silent, not merely shown as an empty waveform.

## Phase C — Full transcript and speaker editor

### Goal

Turn correction into a trustworthy, fast editing workflow for real meetings.

### Work sequence

1. Move from the current row editor to a revision-aware segment model with explicit diff states:
   unchanged, user text edit, user speaker edit, split, merge, retranscribed, and unresolved.
2. Add keyboard navigation, focus-safe inline editing, undo/redo across reloads, and a visible
   unsaved/edited state.
3. Add bulk speaker assignment over a selected range.
4. Add speaker create, rename, merge, split, and unknown operations while retaining prior IDs in
   provenance history.
5. Add explicit voice-profile enrollment only after a user confirms a label. Store embeddings and
   match metadata, never source audio, in the existing voice-profile store.
6. Add corrected Markdown, TXT, JSON, SRT, and VTT exports. Every export includes the revision
   used and a degraded-source notice when applicable.
7. Make transcript writeback consume the selected workspace revision rather than the live file.

### Measurable gates

- Labeling 500 rows takes no more than 30 seconds of continuous keyboard/mouse work in a fixture.
- Reloading the app preserves text, speaker IDs, split/merge structure, undo history, and revision.
- A corrected label survives export, writeback, and a later reprocessing job in 20/20 fixtures.
- No export or editor operation changes a raw file; checksum comparison proves this.
- Every edited row displays whether its text and speaker came from live capture, diarization, a
  profile, or the user.

## Phase D — Reprocessing and evidence review

### Goal

Allow the user to improve a meeting after the fact without destroying the first record.

### Work sequence

1. Add a local job queue with job IDs, progress, cancellation, retry, and restart recovery.
2. Support selected-range retranscription with Turbo as the baseline and configured fallback only
   when local processing fails.
3. Support selected-range diarization/alignment against mic, system, mixed, or imported media.
4. Compare live, retranscribed, diarized, and user-confirmed revisions side by side.
5. Mark answers, summaries, embeddings, writebacks, and memory artifacts stale whenever their
   source revision changes.
6. Add evidence links from every answer claim to transcript segment IDs and audio time ranges.
7. Preserve user-confirmed text and speaker assignments unless the user explicitly chooses
   replacement.

### Measurable gates

- A one-hour reprocessing job is cancellable within 2 seconds and resumes after restart.
- Capture continues at its existing latency while reprocessing runs in the background.
- User-confirmed rows survive 20/20 retranscription and diarization fixtures.
- No answer can be displayed as current when its recorded source revision is stale.
- Every generated claim has at least one clickable supporting segment or is marked unverified.

## Phase E — Library search, Obsidian context, and export

### Goal

Make the workspace useful across a large personal meeting archive without putting the whole vault
or meeting history into every prompt.

### Work sequence

1. Add a local SQLite FTS index for session metadata, transcript revisions, participant labels,
   timestamps, tags, and integrity state.
2. Index incrementally from file checksums and revision IDs; interrupted indexing resumes safely.
3. Search by phrase, participant, title, date, folder, health state, and review status.
4. Add bounded hybrid retrieval across meeting transcripts and the configured Obsidian vault,
   preserving note paths and meeting timestamps as citations.
5. Add context budgets and source controls so live answers receive only the best bounded evidence.
6. Add batch export and safe archive controls. Derived deletion must never delete original audio.
7. Research and optionally import only a pinned, verified MIT-era Screenpipe snapshot for
   architecture or compatible components. Preserve its license/copyright notices and maintain a
   file-level provenance record; never depend on the current commercial-license tree.

### Measurable gates

- 100,000 transcript segments remain searchable with p95 local search latency under 250 ms.
- A cross-meeting answer cites meeting/session and timestamp for every supporting result.
- Unchanged Obsidian files avoid re-embedding and indexing resumes after interruption.
- Context assembly remains bounded by configured token and source-count budgets.
- Deleting or archiving derived data leaves original WAVs, raw segments, and capture reports
  unchanged in 20/20 destructive-safety fixtures.

## Delivery order and decision gates

1. Finish real-device Phase A acceptance using healthy, silent, recovered, and failed sessions.
2. Ship Phase B waveform/playback only after range transport and memory gates pass.
3. Ship Phase C editing/export/writeback only after revision and raw-immutability gates pass.
4. Ship Phase D jobs only after a stale-artifact matrix and cancellation/restart fixtures exist.
5. Ship Phase E search/context only after citation and bounded-context evaluations pass.

At each phase boundary: run the complete Python suite, rebuild/launch the Control Center against
the real recordings folder, perform the fixture matrix, update the roadmap and research notes, and
record measured latency, memory, and accuracy results before starting the next phase.
