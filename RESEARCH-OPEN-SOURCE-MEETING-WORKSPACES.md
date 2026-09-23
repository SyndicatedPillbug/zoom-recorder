# Open-Source Meeting Workspace Research

Research pass: 2026-09-23  
Purpose: identify reusable patterns for a persistent meeting library, playback-linked transcript
editing, speaker correction, local diarization, and post-call reprocessing.

## Executive conclusion

There is no single project to adopt wholesale. The strongest path is to keep this application's
app-agnostic capture and integrity layer, then borrow proven workspace patterns:

- Meetily for a local meeting library, inline transcript editing, pause/resume, and local model
  integration;
- Vibe for a polished local transcription/review/export surface;
- OpenWhispr for meeting-mode audio separation, voice fingerprinting, notes/search, and the idea
  of a persistent desktop workspace;
- Transcribe Offline for a native local editor with real-time diarization;
- Kuali for searchable meeting history, participant-owned tasks, and honest attribution when the
  meeting platform exposes participant identity;
- Millet for post-call speaker labeling, voice-profile enrollment, and regeneration of derived
  outputs;
- Transcript Desk, Rescript, OpenTranscriber, Audino, and Potato for waveform-linked editing and
  annotation interaction patterns.

The project should treat these as reference implementations, not automatic dependencies. License,
model terms, macOS permissions, memory use, and capture assumptions all require separate review.

## Project findings

### Screenpipe

Screenpipe is important historical research. The current repository moved from MIT to the
Screenpipe Commercial License in June 2026, but the project explicitly states that versions
previously released under MIT remain available under MIT. That means the license change is not a
retroactive withdrawal of the rights granted for an earlier MIT release. The practical boundary
is the exact commit or release: code present in a verified MIT-era snapshot can be studied,
modified, and reused under MIT, while later additions and the current tree need their current
license reviewed. See the [license announcement](https://screenpipe.com/blog/screenpipe-license-update)
and [current license](https://github.com/dp466/screenpipe/blob/main/LICENSE.md), especially its
statement that earlier MIT versions remain MIT-licensed.

Useful ideas from its earlier and current direction:

- a persistent local memory/search layer across screen, audio, and transcript events;
- a local API and event-oriented data model;
- always-available access after capture rather than a disappearing recording window.

Decision: do not make the current Screenpipe repository a dependency. We will separately identify
and pin a last MIT-era release/commit if we reuse code, retain its MIT notice and copyright
attribution, audit its dependency/model licenses, and keep any later Screenpipe code out unless
its separate terms permit the intended use. Historical MIT-era code is a viable research and
reuse source; it is not a reason to copy the present source tree wholesale.

### Meetily

The current Meetily repository identifies itself as MIT and local-first. It advertises real-time
Whisper/Parakeet transcription, speaker diarization, local summaries, import/enhance workflows,
embedded local storage, and a standalone Tauri/Rust architecture. Its releases specifically call
out inline transcript editing, meeting history, pause/resume, system-tray control, and post-call
retranscription. Sources: [repository](https://github.com/zackriya-meetily/meetily),
[privacy policy](https://github.com/Zackriya-Solutions/meetily/blob/main/PRIVACY_POLICY.md), and
[release notes](https://github.com/Zackriya-Solutions/meetily/releases).

Useful patterns:

- session history as a first-class product surface;
- local database instead of a folder of disconnected outputs;
- editing and reprocessing as normal workflows;
- local GPU acceleration as an explicit capability.

Caution: the repository has an archived legacy backend. It should not be treated as the current
supported architecture without checking the current source tree.

### Vibe

Vibe is MIT-licensed and focuses on local audio/video transcription. Its current README lists
real-time preview, system and microphone capture, speaker diarization, stable timestamps,
waveform-oriented processing, local model selection, and exports including SRT, VTT, TXT, HTML,
PDF, JSON, and DOCX. Source: [Vibe repository](https://github.com/thewh1teagle/vibe).

Useful patterns:

- model/settings management exposed in the UI;
- explicit export formats;
- a review surface that treats transcription as an editable artifact rather than a terminal log.

Caution: Vibe is primarily a transcription application, not a capture-integrity system or
app-agnostic meeting library. It is a UX reference, not a replacement.

### OpenWhispr

OpenWhispr is MIT-licensed and advertises local or cloud transcription, meeting transcription,
live diarization, voice fingerprinting, audio/video import, notes, folders, semantic search, and
AI actions. Its repository describes a React/Electron/better-sqlite3/whisper.cpp/sherpa-onnx stack.
Sources: [README](https://github.com/OpenWhispr/openwhispr) and [meeting pipeline notes](https://github.com/OpenWhispr/openwhispr/blob/main/CLAUDE.md).

The meeting pipeline is especially relevant: its documented design includes separate microphone
and system streams, echo-leak detection, mic gating, duplicate suppression, and retraction of
racing transcript finals. Those are useful patterns for our current speaker-stream problem.

Caution: a public issue shows that system-audio configuration can still fail on some Linux audio
setups. The project is evidence that separate streams and explicit echo handling matter, not proof
that another app will solve our macOS route problem automatically.

### Transcribe Offline

Transcribe Offline is MIT-licensed and describes itself as a local Rust/egui application for
transcription, speaker diarization, and transcript review/editing. Its current documentation
also advertises real-time live transcription and live speaker diarization backed by a native
ggml/llama.cpp-style engine with Metal/Vulkan/CUDA support. Source:
[repository](https://github.com/openresearchtools/transcribeoffline).

Useful patterns:

- a native editor can keep local processing responsive;
- streaming transcript and diarization need continuous session state rather than post-hoc flat
  text replacement;
- playback/review should be designed alongside live transcription.

### Kuali

Kuali is MIT for its desktop workspace and Apache 2.0 for its browser extension. It presents a
searchable meeting library, participant-owned tasks, summaries, questions, and citations. It
keeps participant identity before decoding when Discord or supported Meet integrations expose it,
and explicitly labels mixed audio honestly rather than inventing attribution. Sources:
[repository](https://github.com/igarrux/kuali) and its platform support notes.

Useful patterns:

- use platform identity when available, but preserve `unknown` when it is not;
- cite answers back to meetings and source ranges;
- make the library and task model durable, not ephemeral HUD output.

Caution: Zoom and Teams support are currently described as experimental/partial, so its capture
architecture is not a direct fit for our app-agnostic requirement.

### Millet

Millet is a useful reference for manual correction improving future recognition. Its documentation
describes post-call speaker labeling with audio clips, explicit names, automatic profile matching,
and stored voice profiles whose embeddings improve through later labeled sessions. Source:
[voiceprint and labeling documentation](https://github.com/pretyflaco/millet).

Useful patterns:

- label speakers after the call while listening to representative clips;
- preserve a reusable voice-profile store separately from raw audio;
- regenerate derived transcript/summary/PDF outputs after confirmed labels;
- support a non-interactive JSON label map for automation.

### Muesli

Muesli is a small macOS local meeting recorder that advertises ScreenCaptureKit system-audio
capture, microphone capture, local faster-whisper, optional pyannote diarization, and menu-bar
operation. Source: [repository](https://github.com/scottscotthendo/muesli).

Its interesting contribution is architectural: ScreenCaptureKit can provide a different system
audio path than Multi-Output/BlackHole on supported macOS versions. It also documents the cost of
diarization dependencies and Hugging Face model access. It should be evaluated as a capture-path
experiment, not adopted without permission, memory, and loss-detection tests.

### Transcript Desk, Rescript, OpenTranscriber, Audino, and Potato

These projects cover the review/editing side especially well:

- [Transcript Desk](https://github.com/srothgan/transcriptdesk): waveform player beside a
  professional editor, timestamp jumping, revisions, speaker shortcuts, local saving;
- [Rescript](https://github.com/wassgha/rescript): word-level transcript editing where deleting
  words edits the associated media, with captions and NLE/DAW exports;
- [OpenTranscriber](https://github.com/abalvet/OpenTranscriber): synchronized multi-speaker
  waveforms, manual speaker tracks, segment loop playback, and annotation exports;
- [Audino](https://github.com/midas-research/audino): browser-based audio annotation for VAD,
  diarization, speaker identification, ASR, and custom labels;
- [Potato](https://github.com/davidjurgens/potato): waveform annotation, speaker tiers, zoom/
  scroll, playback, and multiple annotation formats.

These are the strongest references for our requested “click the text, hear the audio, correct the
speaker, save the revision” workflow.

## Recommended architecture for this project

1. Keep the existing recorder and new capture-integrity subsystem as the immutable source layer.
2. Add a persistent local session index and workspace API over those folders.
3. Store structured transcript revisions with stable segment IDs, timestamps, provenance, and
   speaker IDs.
4. Make audio playback and waveform navigation first-class, with source-track health visible.
5. Make speaker labeling an editable assignment layer, not a destructive text replacement.
6. Re-run diarization/transcription in cancellable background jobs against selected ranges.
7. Mark summaries, answers, embeddings, and writebacks stale when their source revision changes.
8. Reuse visual and interaction ideas from the projects above only after license and dependency
   review.

## Research follow-ups before dependency adoption

- Clone candidate repositories into a temporary research area and inspect licenses, release
  activity, model terms, and macOS permissions.
- For Screenpipe specifically, record the exact last MIT-era tag/commit, preserve the original
  LICENSE text and notices, and produce a file-level provenance list before importing anything.
- Measure memory and latency on this 24 GB Mac with a one-hour two-channel fixture.
- Compare ScreenCaptureKit, BlackHole/Multi-Output, and tap paths with deliberate route failures.
- Test whether the candidate editor supports immutable originals and revisioned speaker edits.
- Prefer small, well-isolated components (waveform rendering, media timeline, annotation schema)
  over importing a full meeting product.
