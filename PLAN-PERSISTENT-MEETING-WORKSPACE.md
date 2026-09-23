# Persistent Meeting Workspace Plan

Status: Phase A implemented 2026-09-23; Phases B–E planned in
[PLAN-WORKSPACE-PHASES-B-E.md](PLAN-WORKSPACE-PHASES-B-E.md)  
Created: 2026-09-23  
Relationship: successor to the transient Live HUD and companion to
[PLAN-AUDIO-CAPTURE-INTEGRITY.md](PLAN-AUDIO-CAPTURE-INTEGRITY.md)

## Product shift

The current HUD is optimized for the active call. That is necessary, but it is not enough. After
the recording stops, the application should remain useful as a durable control, display, access,
playback, and editing workspace.

The product should have two coordinated surfaces:

1. **Live HUD:** minimal, fast, disposable meeting overlay for transcript, questions, answers,
   health, and controls.
2. **Meeting Workspace:** persistent library/editor opened from the menu bar and available after
   recording ends. It owns review, correction, playback, recovery, export, and reprocessing.

Stopping a recording must end capture, not make the meeting disappear.

## Core experience

When a recording finishes, the workspace remains available and opens the completed session with:

- an integrity banner showing microphone, system/other-party, tap, transcript, and writeback
  status;
- a session title, date, duration, participants, source devices, and recovery state;
- audio playback with waveform/timeline navigation;
- transcript rows synchronized to the playhead;
- speaker labels editable directly on each row and in bulk;
- search, filtering, and jump-to-result playback;
- transcript, summary, questions, answers, action items, and evidence views;
- reprocess controls for selected ranges, alternate models, diarization, and alignment;
- export and writeback controls that preserve the raw originals;
- revision history and undo for user edits.

The library must also make degraded sessions useful. A session with a silent system track should
remain reviewable, but the UI must say so prominently and never imply that the missing audio can
be recovered locally.

## Canonical data model

The workspace needs a structured session model rather than treating Markdown or a flat transcript
text file as the source of truth.

### Immutable source layer

- original microphone WAV;
- original system/other-party WAV or tap track;
- raw crash-safe segments;
- capture log;
- integrity report and checksums;
- original live event log;
- original transcript revisions from the recorder.

These files are append-only or read-only after verification. Editing never modifies them.

### Editable interpretation layer

Each transcript segment should have a stable ID and revision history:

```text
segment_id
start_s
end_s
speaker_id
speaker_label
text
text_source: live | retranscribed | user
speaker_source: channel | diarization | profile | user | unknown
confidence
audio_source
revision
```

Speaker labels are assignments to stable speaker IDs, not destructive string replacements. A user
can rename Sarah once and every segment assigned to Sarah updates without rewriting unrelated
people or historical evidence.

### Derived layer

Summaries, answers, action items, embeddings, exports, and Obsidian writebacks are derived from a
selected transcript revision. Each derived artifact records the source revision and model/config
used to produce it. Re-editing a transcript should mark downstream artifacts stale rather than
silently pretending they still reflect the current text.

## Editing features

### Speaker correction

- Click a transcript row's speaker label to choose or create a participant.
- Rename a speaker globally for this session.
- Apply a label to a contiguous selection of rows.
- Split one diarization speaker into two when the automatic clustering was wrong.
- Merge two speaker IDs when they are the same person.
- Mark a segment `unknown` instead of forcing a guess.
- Optionally update the reusable voice-profile store only after explicit confirmation.
- Show whether each label came from channel identity, diarization, a voice profile, or the user.

### Text correction

- Inline edit with undo/redo.
- Split and merge transcript segments.
- Re-transcribe a selected time range with Turbo, another local model, or a configured fallback.
- Compare the current text with the live/original revision.
- Preserve user edits when reprocessing unless the user explicitly chooses replacement.
- Jump from a changed word to the exact audio region that supports or contradicts it.

### Audio review

- Play the mixed track or solo microphone/system tracks.
- Mute/solo tracks and compare them without destroying either source.
- Click any transcript row to seek to its timestamp.
- Loop a selected range for speaker labeling or difficult words.
- Variable playback speed and keyboard shortcuts.
- Show silent or missing intervals directly on the timeline.
- Show when a row has no corresponding usable audio, as in the recovered 2026-09-23 meeting.

## Persistence and navigation

### Library

- Menu-bar item: `Open Meeting Workspace`.
- Sessions grouped by date with readable title plus immutable ID.
- Health badges: `Healthy`, `Partial`, `Degraded`, `Recovered`, `Capture failed`.
- Search by title, participant, transcript text, tags, folder, and date.
- Filters for sessions needing review, missing system audio, unknown speakers, failed writeback,
  or stale derived artifacts.
- Session folder and artifact locations visible without exposing secrets.

### Session page

Use a three-region layout:

1. media/timeline and source health;
2. transcript editor and speaker labels;
3. summary, questions, answers, evidence, and derived-artifact status.

On small screens these become tabs or a split-pane layout. The Live HUD remains independent so
opening the workspace cannot steal focus or add latency during a call.

### Persistence rules

- The workspace server/app may remain available after capture stops.
- Closing the HUD must not close the workspace or stop processing.
- The last open session and scroll/playhead state are restored.
- Every edit is durable before the UI reports success.
- A failed write leaves the previous revision intact and offers retry.

## Implementation phases

### Phase A — Session library and integrity-aware detail page

- Index existing session folders without moving or rewriting artifacts.
- Read `session.json`, `recording_integrity.json`, manifests, transcript, and derived files.
- Add recording-health badges and a persistent recordings list.
- Open a session after recording ends.

Exit gates:

- A stopped session remains visible and openable.
- A partial/silent system track is unmistakable.
- Library indexing never blocks capture or changes raw files.

### Phase A completion record — 2026-09-23

The first phase is implemented. The Control Center now indexes existing sessions, shows
integrity-aware badges, opens a durable workspace from the menu bar, automatically hands the user
to that workspace after recording finalization, and exposes path-safe session/media APIs. The
workspace stores edits under `derived/` only, with atomic writes, optimistic revision checks,
revision snapshots, local undo, speaker assignment, split/merge, and derived Markdown export.
Original audio, raw segments, capture logs, integrity reports, and the original transcript remain
outside the editable interpretation layer. Endpoint, range-playback, path-safety, persistence,
conflict, undo, and export behavior are covered by the green regression suite.

The remaining real-device acceptance work is deliberately tracked with the capture-integrity
matrix: the workspace must be tested against healthy, partial, recovered, and capture-failed
sessions on the actual Mac before this phase is called production-proven.

### Phase B — Waveform-linked playback

- Add a browser-native audio transport or a small local media service.
- Generate/cache lightweight waveform peaks; never load hour-long PCM fully into the UI.
- Align transcript rows to playback and support per-source solo/mute.
- Add keyboard navigation and loop selection.

Exit gates:

- Clicking a transcript row seeks to the correct source time within 100 ms on test fixtures.
- Playback remains responsive for a one-hour session.
- Missing/silent tracks are shown as missing/silent rather than as empty UI space.

### Phase C — Transcript and speaker editor

- Introduce stable segment IDs and revisioned editable transcript JSON.
- Inline text editing, speaker dropdowns, create/rename/merge/split, undo/redo.
- Preserve provenance and audio timestamps on every edit.
- Export corrected Markdown, TXT, SRT/VTT, JSON, and transcript writeback.

Exit gates:

- A user can label an entire meeting's speakers without editing raw files.
- Reloading the session preserves every edit and undo boundary.
- Exports and Obsidian writeback reflect the selected revision exactly.

### Phase D — Reprocessing and evidence review

- Re-transcribe selected ranges without replacing the original.
- Re-run diarization against the selected audio source.
- Compare live, retranscribed, and user-confirmed versions.
- Mark derived summaries/answers stale after transcript changes.
- Add “why this answer exists” evidence links to transcript time ranges.

Exit gates:

- Reprocessing is cancellable, resumable, and isolated from the original recording.
- User-confirmed labels/text survive every reprocessing pass.
- AI output cannot silently use a stale transcript revision.

### Phase E — Library search, context, and export

- Search all transcript revisions and session metadata.
- Link sessions to Obsidian notes and source folders.
- Add bounded cross-meeting retrieval with citations back to sessions and time ranges.
- Add batch export and safe archive/delete controls.

Exit gates:

- A user can find a meeting by participant, phrase, date, or topic.
- Cross-meeting answers cite the source meeting and timestamp.
- Deleting a derived artifact cannot delete an original recording.

## Measurable product gates

- Recording completion leaves the workspace available in 100/100 lifecycle replays.
- 100% of session pages show integrity status before transcript content is treated as complete.
- Speaker-label edits persist across reload, export, writeback, and reprocessing fixtures.
- No raw audio file is modified by editing, diarization, enhancement, or export tests.
- One-hour playback and transcript scrolling remain responsive without loading all PCM into memory.
- A session can be recovered and opened after a forced stop without terminal intervention.
- A stale derived artifact is always identified after its source transcript revision changes.

## Architecture decision

Do not replace the existing capture recorder with a large third-party meeting application. Reuse
ideas and narrowly scoped components where licensing and integration fit, but keep our durable
source-of-truth, capture-integrity, app-agnostic audio layer. The workspace should consume the
session artifacts through a stable local API and never be on the capture critical path.
