# Audio Capture Integrity Plan

Status: active implementation plan  
Created: 2026-09-23  
Incident: 2026-09-23 session `10-59-57_a7c3f367`

Current implementation slice: integrity reporting, disk-based segment reconciliation, atomic
merge publication, end-of-file silence detection, resumable `--recover SESSION_DIR`, and
in-call source-health transitions are implemented and covered by the regression suite. The
remaining phases below are still required; this slice is not yet the complete real-device
reliability gate.

The deterministic fault suite now covers a live-but-silent loopback, system-source open failure,
source-state transition deduplication, disk-only recovery, failed atomic merge publication, and
silence that extends through end-of-file. The full suite currently passes 303 tests.

## Implemented in the current slice

- The current physical output now wins over a stale remembered Multi-Output pairing.
- Every session records the managed output, paired physical output, loopback identity, backend,
  and whether a live signal has actually verified the application route.
- “Opened” and “signal confirmed” remain separate states.
- Silent system audio is marked as a critical runtime condition and the warning tells the user to
  select the managed Multi-Output device in the call application.
- The live HUD displays the call source, paired output, backend, and verification indicator.
- Failed WAV merges use a valid temporary `.wav` suffix and the latest affected session was
  recovered from raw segments.
- The optional Core Audio tap now uses a unique private aggregate identity per attempt and its
  self-test reports startup failures as a controlled fallback condition instead of crashing the
  recorder with a traceback.

## Purpose

The recorder must never silently present a meeting as successfully recorded when an expected
audio source was absent, silent, truncated, or lost during shutdown. The goal is not to promise
that macOS, a meeting application, or a hardware route can never fail. The goal is stronger and
achievable: every failure must be detected quickly, surfaced prominently, preserved for recovery,
and represented honestly in the session artifacts.

The recorder must distinguish these states:

1. **Captured:** valid PCM was written for this source.
2. **Silent:** the source opened, but its samples contained no meaningful signal.
3. **Lost:** the source was healthy and then stopped producing valid signal or segments.
4. **Truncated:** the source has less usable duration than the recording interval.
5. **Recovered:** a final WAV was rebuilt from raw segments after a crash or finalization failure.
6. **Unavailable:** the source could not be opened or permission/routing prevented capture.
7. **Unknown:** the recorder lacks enough evidence to claim any of the above.

“A WAV file exists” is never sufficient evidence of successful capture.

## The incident this plan addresses

The 2026-09-23 meeting exposed two independent defects:

### A. Capture-path failure

The latest affected session did select `BlackHole 2ch`, but the managed Multi-Output device was
paired with the MacBook Air speakers while the active physical output was External Headphones.
The call application therefore bypassed the managed route; BlackHole received a normal-duration
stream of digital silence (`mean_volume = max_volume = -91 dB`). An older incident also selected
`ZoomAudioDevice`, which is not a general system-output loopback. Both cases demonstrate why a
device name and an opened stream are not proof that the other-party path is working.

### B. Finalization-path failure

The live HUD renamed the numeric session folder after deriving a readable title. `Recorder`
continued holding paths under the old folder name. The raw segments survived, but finalization
looked in the old location, concluded that no usable audio existed, and failed to emit the
expected files.

The second defect is now patched defensively with path rebinding. This plan removes the lifecycle
coupling entirely and adds recovery if a future rename or crash violates the expected ordering.

### Latest real-device validation — 2026-09-23

- The live doctor sees the active microphone, BlackHole loopback, and managed Multi-Output route.
- The recorder now chooses the currently active physical output before reusing a remembered
  pairing, so switching to headphones cannot silently retain a speakers-only pairing.
- The menu-bar context deliberately does not start Core Audio tap capture because macOS has not
  granted that launch context system-audio capture permission; it uses the loopback backend and
  records that decision in the integrity report.
- The optional tap self-test now has unique private aggregate identities and converts startup
  failures into a controlled fallback result. The tap is not promoted to primary until a native
  permission-bearing helper passes the real-device matrix in Phase 5.

## Failure-mode map

### 1. Before recording starts

- Microphone permission is missing, revoked, or granted to a different Python/launch context.
- System Audio Recording permission is missing for the tap path.
- BlackHole is missing, disconnected, renamed, or not an input device.
- The Multi-Output Device does not contain both BlackHole and the real output.
- The default output is a Bluetooth device, display, dock, or application-specific route that is
  not represented by the selected loopback.
- `ZoomAudioDevice` is selected because it looks like a loopback but does not carry general
  system output.
- The selected device opens successfully but carries digital silence.
- The user believes the system track is active while the recorder silently falls back to
  microphone-only mode.
- The application reports an input name but does not record a positive signal sample.
- Disk space, directory permissions, or a stale lock prevent segment creation.
- The selected model or resource configuration consumes enough memory to destabilize the call.

### 2. During startup

- ffmpeg starts but never writes its first segment.
- ffmpeg writes a header or zero-length segment and then wedges.
- The active microphone is probed by a second reader and the real recorder is blocked.
- The system route changes between device selection and the first segment.
- The startup check samples a quiet interval and mistakes “no speech” for “no capture.”
- The startup check verifies that a device opens but not that meaningful samples arrive.
- A route-repair attempt switches to a device that is technically valid but semantically wrong.
- The HUD says recording is active before capture has been proven.

### 3. During recording

- The default output changes when the user switches speakers, headphones, Bluetooth, or a dock.
- The call application changes its own output route.
- BlackHole remains selected but receives no output.
- A tap stops producing PCM while its process remains alive.
- The microphone disappears or becomes unavailable.
- ffmpeg exits, wedges, or continues running without advancing a segment.
- Segment rotation loses the final partial segment or creates a gap/duplicate.
- The disk fills or a write becomes permission-denied.
- Resource pressure, swap, thermal throttling, or another application destabilizes capture.
- STT, diarization, retrieval, provider calls, HUD rendering, or writeback blocks capture.
- A warning is emitted only to a log, behind a hidden HUD, or after the meeting ends.

### 4. During stop

- The menu-bar state does not change when the web/HUD stop control is pressed.
- A stop request races with route restoration, HUD shutdown, or process termination.
- ffmpeg is terminated before its final segment is flushed.
- The HUD derives identity and renames the folder before audio finalization.
- The recorder retains stale absolute paths after a folder move.
- A timeout kills the recorder before the finalizer runs.
- The user closes the HUD or menu-bar process while the capture process is still active.

### 5. During merge and verification

- A missing segment is treated as an empty but valid source.
- A concat operation succeeds syntactically while dropping or duplicating time.
- Duration is checked but continuity and signal coverage are not.
- A long silent tail is hidden by a healthy beginning.
- A normal-duration silent system track is treated as successful.
- Only the merged file is checked and the raw segment evidence is discarded.
- A failed merge leaves no usable artifact and no recoverable work directory.
- A verification warning is emitted without encoding its result in session metadata.

### 6. During archival and downstream work

- Raw segments are deleted before the merged files and manifest are verified.
- A partially written WAV is made visible under its final name.
- A downstream enhancer/transcriber overwrites an original.
- The manifest is written before the file is complete or with the wrong session identity.
- A readable folder rename causes manifests, writeback, or transcript paths to disagree.
- A later cleanup job removes the only recoverable copy.
- A partial session is presented in the recordings browser as complete.

### 7. After a crash, sleep, reboot, or forced termination

- `.work` exists but no command can resume finalization.
- The recorder cannot distinguish an in-progress session from an abandoned one.
- A second finalizer duplicates or overwrites an existing artifact.
- A stale lock prevents recovery.
- The raw segments are present but their original source, order, or timing is unknown.
- The next recording starts while the previous ffmpeg or tap process remains alive.

### 8. User-facing and operational failures

- The user does not know which track is healthy: microphone, system, tap, or none.
- A warning is phrased as advice instead of an explicit recording-integrity failure.
- The warning does not say what was lost and when it began.
- The user cannot acknowledge, retry, or switch to a known-good mode.
- The end-of-call summary does not force review of degraded tracks.
- Documentation says “recorded” when the artifact is only a silent container.

## Reliability architecture to implement

### 1. Make capture the owner of the session lifecycle

The recorder, not the HUD, owns the canonical session directory and finalization state. The HUD
may write derived files, but it must not rename or finalize the audio directory while capture is
active.

Required ordering:

```text
capture start
  -> source handshake
  -> verified recording
  -> stop request
  -> stop capture and flush final segments
  -> discover/reconcile raw segments from disk
  -> merge each source atomically
  -> verify each source
  -> write manifest/session integrity record
  -> archive raw segments
  -> derive title and rename session folder
  -> update all path-bearing metadata
```

The existing path-rebinding fix remains as a defensive backstop, but correct ordering is the
primary fix.

### 2. Add a per-source integrity state machine

Each expected source gets an independent state and evidence record. The minimum fields are:

- source kind: `mic`, `system_loopback`, or `system_tap`;
- selected device identity and route snapshot;
- permission/check result;
- first positive signal timestamp;
- last positive signal timestamp;
- last segment written and byte count;
- per-window RMS/peak/speech coverage;
- expected duration and actual usable duration;
- route changes and recovery attempts;
- final state and reason;
- artifact paths, sizes, durations, and checksums.

The session state must be one of `healthy`, `degraded`, `partial`, `recovered`, or
`capture_failed`. It must never infer health from process liveness alone.

### 3. Add a preflight that proves the actual end-to-end path

Before a real meeting is allowed to proceed in system-audio mode:

- verify permissions and route topology;
- reject or explicitly label `ZoomAudioDevice` when it is not carrying the configured general
  output path;
- verify BlackHole/tap is an input and the selected output is the expected output;
- run a short positive signal test where possible;
- wait for real capture segments;
- confirm the selected source has non-silent samples;
- show the user the exact microphone and other-party source;
- require an explicit mic-only acknowledgement if system audio cannot be proven.

The preflight must not open a second reader against the active microphone.

### 4. Monitor the signal, not just the process

Runtime health must combine:

- process liveness;
- segment progress and byte growth;
- route/default-device changes;
- per-source audio levels;
- time since last positive signal;
- time since last segment;
- disk space and write errors;
- memory/swap pressure.

A system source that has been silent while the microphone is active must trigger a visible,
high-priority warning within 15 seconds. The warning must identify the affected source and offer
the next action. One bounded route-recovery attempt is allowed; after that, the state remains
degraded until positive signal is proven.

### 5. Make finalization crash-safe and resumable

Introduce an idempotent finalizer that can run from either the normal stop path or a later
recovery command. It must:

- scan the session directory on disk rather than trusting only in-memory paths;
- reconcile every raw segment by source, session number, sequence number, duration, and checksum;
- merge to a temporary filename and atomically rename only after verification;
- preserve `.work`/`.segments` until every expected artifact is verified;
- never overwrite an existing verified original;
- write a `recording_integrity.json` report even when capture is partial;
- support repeated execution without duplication or corruption.

If only microphone audio exists, the result must explicitly say `system_audio: unavailable` or
`system_audio: silent`; it must not say “recording complete.”

### 6. Verify continuity and meaningful signal

Verification must be per-source and per-time-window, not only whole-file duration. It should
report:

- segment count and sequence gaps;
- expected versus actual duration;
- first/last usable sample;
- silent windows and the longest silent run;
- signal coverage percentage;
- mean/max level;
- whether the source was healthy at the start, lost mid-call, or never proved healthy;
- whether the output came from normal merge or recovery.

An output with a valid WAV header but no meaningful samples is a **failed source**, not a
successful recording.

## Implementation phases and exit gates

### Phase 0 — Incident contract and redacted diagnostics

Deliverables:

- `recording_integrity.json` schema;
- per-source state machine;
- explicit session states in the recordings browser/HUD;
- redacted route, device, timing, and health diagnostics;
- documentation of the 2026-09-23 incident.

Exit gates:

- Every session, including a failed one, has an integrity report.
- No report calls a source healthy without positive signal evidence.
- A silent-but-long WAV is classified as `silent`, not `complete`.

### Phase 1 — Correct lifecycle ownership and resumable finalization

Deliverables:

- recorder-owned rename/finalization ordering;
- disk-based segment reconciliation;
- atomic merge outputs;
- resumable/idempotent finalizer;
- stale-path and folder-rename regression tests.

Exit gates:

- 1,000 simulated folder-renames/finalizations preserve every segment.
- 100 forced-stop/restart runs produce either verified originals or a recoverable raw-segment
  directory.
- No failure path deletes raw evidence.

### Phase 2 — End-to-end preflight and explicit degraded mode

Deliverables:

- route-aware loopback self-test;
- clear rejection/labeling of semantically wrong devices;
- explicit mic-only mode and acknowledgement;
- startup warning before the user relies on an unverified other-party track.

Exit gates:

- A deliberately silent loopback is detected before or immediately after recording begins.
- A valid loopback passes without requiring a second microphone reader.
- 50/50 simulated permission, missing-device, wrong-device, and silent-device tests produce the
  correct state and user action.

### Phase 3 — Runtime loss detection and recovery

Deliverables:

- per-source heartbeat and signal windows;
- route-change handling with positive revalidation;
- disk-space/write-failure handling;
- memory/swap warning integration;
- prominent HUD/menu-bar alerts with timestamps.

Exit gates:

- System silence is surfaced within 15 seconds.
- Microphone loss is surfaced within 10 seconds.
- A route change cannot silently downgrade the session.
- 60-minute synthetic runs produce zero unreported source-loss intervals.

### Phase 4 — Verification, archival, and recovery UX

Deliverables:

- end-of-call integrity summary;
- one-click “recover this session” action;
- recordings list badges for healthy/degraded/partial/recovered;
- append-only manifest entries for every source and state;
- raw evidence retention policy.

Exit gates:

- Users can tell in one glance whether the other-party track was captured.
- A failed merge can be rerun without code or manual path repair.
- A partial session is never shown as a complete recording.

### Phase 5 — Real-device and real-call validation

Test matrix:

- MacBook speakers + Multi-Output + BlackHole;
- headphones;
- Bluetooth headset;
- external display/dock output;
- output switching mid-call;
- Zoom, Meet, Teams, Jitsi, browser audio, and locally played audio;
- local Turbo under normal and memory-pressure conditions;
- microphone-only mode;
- tap mode where permission is available;
- menu-bar stop, HUD stop, Ctrl+C, process termination, sleep/wake, and forced quit.

Exit gates:

- 20 real-device sessions across the matrix with no unreported source loss.
- Every intentionally broken route produces an in-call warning and an honest final report.
- The complete capture log and integrity report explain every degraded run without inspecting raw
  transcript content.

## Regression test inventory

The implementation is not complete until these tests exist at the point where the result is
consumed:

- source selection rejects semantically wrong loopback devices;
- startup requires positive signal evidence;
- active microphone is never opened by a competing health probe;
- system silence transitions to `degraded` and emits the user-visible alert;
- route changes require revalidation before returning to `healthy`;
- segment progress detects a live-but-stalled ffmpeg process;
- final partial segments are flushed and included;
- folder rename cannot strand segment paths;
- disk reconciliation finds segments without in-memory session state;
- merge output is atomic and idempotent;
- duration, continuity, and signal coverage are checked per source;
- silent WAVs are not classified as valid recordings;
- raw segments survive merge and verification failures;
- crash recovery creates a truthful partial artifact;
- repeated recovery does not duplicate or overwrite originals;
- menu-bar and HUD stop controls converge on the same state;
- recordings UI displays degraded/partial/recovered state;
- manifests and checksums point to the final session identity.

## Definition of done

This work is complete only when all of the following are true:

1. A missing or silent other-party path is detected during the meeting, not discovered later.
2. A route/device change cannot silently change what “system audio” means.
3. A running process with no advancing segments is treated as failed.
4. A folder rename, HUD shutdown, crash, or forced stop cannot discard discoverable audio.
5. Every session ends with verified artifacts or an explicit recoverable failure state.
6. A valid-duration silent WAV is visibly marked silent.
7. Raw segments remain available until verification and manifest creation succeed.
8. Recovery is a supported, repeatable operation rather than a manual forensic procedure.
9. The UI makes microphone and other-party capture health unambiguous.
10. The full deterministic suite, fault-injection suite, and real-device matrix meet the gates
    above.

## Important limitation

No software can reconstruct audio that macOS or the meeting application never delivered to any
capture path. The guarantee we can and must provide is that this condition is detected quickly,
reported prominently, never mislabeled as success, and never compounded by losing audio that had
already been written.

## Real-device test findings — 2026-09-23

The two-tab Jitsi test reached the same community-hosted room as distinct participants, with
browser microphone permissions granted and both tabs transmitting through BlackHole. The local
Core Audio tap delivered the injected speech; the saved system track contained the markers from
both participants. This validates the browser → managed route → system capture path.

The test also exposed two important failures:

1. The saved pairing still named `MacBook Air Speakers` while `External Headphones` were connected.
   Because the managed Multi-Output device remained the default, the stale pairing sent playback to
   the built-in speakers. The pairing was rebuilt around `External Headphones`, and selection now
   prefers a newly available headset over a lower-priority stale output when the aggregate is the
   current default. An explicit output choice remains authoritative.
2. Tap shutdown could race the feeder thread after a bounded join, clearing its shared file
   descriptor before the final write. The feeder now retains a private descriptor reference and
   treats the closed-pipe race as normal teardown; a regression test covers the no-fd path.

The run still reported low system-track coverage because the room was quiet for most of the
controlled interval; that is expected for this short marker test and is not being treated as a
healthy full-meeting capture. The saved artifacts remain available under
`/Users/OHalvorson/ZoomRecordings/2026-09-23/16-52-07_84dbce5e/` for inspection.

The follow-up run also showed that the tap’s former 10-second startup warning was too eager: the
final system WAV contained both participant markers even though the live monitor warned before
the tap had settled. The monitor now keeps the source in `awaiting_signal` for 30 seconds, then
requires 10 additional seconds of confirmed silence before alerting. This preserves fast
post-startup failure detection without treating normal Core Audio attachment as a route failure.

The route lifecycle was then verified on the live Mac. With the managed Multi-Output device
deliberately left as the default, a short tap-mode recording restored `External Headphones` before
capture, completed normally, cleared the routing-session marker, and left the doctor reporting
physical-output routing with volume control available. The lifecycle now covers both tap and
loopback modes and repairs a stale tool-owned aggregate on the next launch after an interrupted
shutdown.
