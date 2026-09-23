# App-agnostic participant attribution plan

This plan adds optional multi-speaker attribution to the generic video-call
recorder. It does not depend on Zoom, Teams, Meet, Discord, or any provider
participant API. The current channel-based `You` / `Remote` labels remain the
fast, reliable live baseline.

## Implementation status (2026-09-22)

- Phase 1 is complete: stable source IDs, local speaker-label overrides,
  inline HUD editing, reconnect-safe snapshot state, and `session.json`
  persistence are shipped.
- Phase 2 now uses the local NeMo-Speech Sortformer adapter by default. It
  invokes the native runtime only after capture and live processing finish,
  writes derived attribution artifacts, and reports missing local setup without
  requesting a Hugging Face token. WhisperX remains an explicit compatibility
  backend only.
- The reusable voice-profile store is now implemented. It enrolls only from
  explicit user labels, stores aggregate embeddings with owner-only permissions,
  and reports thresholded matches as hints. Speaker diarization and persistent
  identity matching are separate providers; the latter is the next local
  runtime task and must not reintroduce a gated model dependency.
- Phases 3 and 4 remain intentionally opt-in design work; no live diarization
  model is allowed onto the capture or answer critical path yet.

## Objectives

- Preserve live transcript and answer latency when diarization is unavailable,
  slow, uncertain, or misconfigured.
- Support any call application whose local microphone and remote/system audio
  can be captured by the existing recorder.
- Let the user rename `Remote 1`, `Remote 2`, etc. directly in the live HUD.
- Keep raw audio and raw transcript events immutable; speaker corrections are
  revisions layered over them.
- Use speaker attribution to improve evidence provenance without treating a
  speaker label as proof that the words themselves are correct.
- Degrade automatically to the current two-channel labels.

## Design invariants

1. Audio capture, final STT, and question answers never wait for diarization.
2. The recorder always retains the original channel/source identity.
3. Uncertain diarization produces a generic label rather than a confident name.
4. User-entered names are local session metadata and never require a provider
   call.
5. Live writeback remains append-only; corrected labels appear in derived
   output or later revision records rather than rewriting an already-written
   line in place.
6. No call-platform roster or authentication is required.
7. Cross-session voice matching is opt-in local biometric metadata; it is
   never inferred from a name alone and can be disabled or deleted.

## Proposed data model

Every committed transcript segment gains optional attribution fields:

```json
{
  "segment_id": "remote:42",
  "speaker_id": "remote:1",
  "speaker_label": "Remote 1",
  "speaker_confidence": 0.91,
  "speaker_source": "channel|diarization|user",
  "speaker_revision": 2
}
```

`speaker_id` is stable within a recording. `speaker_label` is mutable display
metadata. `speaker_source` explains why the label exists. A correction emits a
`transcript_speaker_revision` event referencing the original `segment_id`;
the raw text and audio are untouched.

Initial identities:

- microphone: `local`, displayed as the configured local name;
- loopback/system: `remote`, displayed as `Remote` until diarization splits it;
- mixed/file input: `unknown` until a diarization pass provides evidence.

## User interaction

The HUD will display a compact editable speaker chip beside each transcript
speaker label. Clicking or double-clicking a label opens an inline text field.
Enter commits; Escape cancels; blank input restores the generic label.

The first rename changes the current speaker identity for the session and
updates future segments. It does not trigger an LLM call, block SSE updates, or
rewrite audio. A small local mapping is persisted in session metadata:

```json
{"speaker_id": "remote:1", "label": "Sarah", "source": "user"}
```

If the user renames `Remote 1` before background diarization finishes, that
mapping remains attached when later segments are reconciled. Conflicting or
low-confidence automatic labels never overwrite a user mapping.

## Processing lanes

### Lane 1: live capture and STT

Unchanged priority path:

1. Capture mic and system audio independently where possible.
2. Run the existing turbo/local or remote STT pipeline.
3. Publish `You` / `Remote` channel labels immediately.
4. Generate answers using stable labels only.

### Lane 2: optional live attribution

This is initially limited to speaker-turn detection and only runs on the
remote channel. It consumes bounded rolling audio windows or committed segment
timestamps. It may emit tentative `Remote 1` / `Remote 2` labels, but it cannot
hold the STT or answer workers. Stale windows are discarded.

### Lane 3: background reconciliation

After enough audio accumulates, a lower-priority diarization worker produces
more stable speaker turns. It emits correction events and updates derived
transcript views, meeting memory provenance, and `session.json` participant
metadata. The raw transcript remains recoverable.

### Lane 4: optional post-call pass

If live diarization is disabled or unavailable, run a final remote-track pass
after recording. This is the preferred first diarization implementation because
it can use longer context and does not affect live performance.

## Recommended rollout

### Phase 1 — editable identities, no diarization

- Add stable speaker IDs and label mappings to state/session metadata.
- Add inline HUD rename controls.
- Add tests for rename, persistence, reconnect, and user-overrides-automatic.
- Keep existing channel labels exactly as the fallback behavior.

### Phase 2 — post-call remote diarization

- Add an optional worker that processes only the remote/system track.
- Produce generic speaker IDs, confidence, timestamps, and revision events.
- Update derived transcript and participant metadata after verification.
- Benchmark memory, wall time, and CPU impact on the 24 GB M4 Air.

### Phase 3 — bounded live attribution

- Add opt-in rolling diarization with a strict latency budget.
- Never run it when STT lag exceeds the configured ceiling.
- Coalesce windows and drop stale work.
- Keep tentative labels out of answer triggering until stable.

### Phase 4 — name assistance

- Preserve inline manual rename as the primary mapping mechanism.
- Optionally support a short “who is speaking?” confirmation prompt.
- Consider voice enrollment only as an explicit opt-in; never infer a real
  person’s name silently.

### Reusable voice-profile contract

- A profile is created or updated only when a user-labeled diarization speaker
  supplies an acoustic embedding.
- A future match must clear both the similarity threshold and a separation
  margin from the next-best profile; otherwise the generic speaker label wins.
- The profile store keeps normalized aggregate vectors, sample counts, session
  references, and timestamps. It never stores raw audio or transcript text.
- Profile matches are marked `speaker_source: "voice_profile"`; manual labels
  remain `speaker_source: "user"` and always take precedence.

## Performance and safety gates

The feature is accepted only if controlled tests show:

- no increase in audio capture dropouts;
- no increase in final STT queue wait when diarization is enabled;
- no measurable delay added to question detection or provider TTFT;
- diarization work is bounded and cancellable;
- a provider/model failure returns cleanly to channel labels;
- the raw transcript and audio remain intact after any correction failure;
- user-renamed speakers are never silently replaced;
- mixed, one-sided, silent, and overlapping speech cases remain valid.

Diagnostics will separately record diarization queue wait, inference time,
windows dropped, attribution confidence, revisions, and fallback count. These
metrics will be written to `derived/live_diagnostics.json` without secrets.

## Verification fixtures

Add replay fixtures for:

- two-party mic/remote audio;
- three remote speakers on one loopback channel;
- overlapping speech;
- one speaker changing devices;
- a manual rename before and after automatic attribution;
- a low-confidence result that must remain `Remote`;
- diarization disabled, unavailable, and cancelled mid-session.

## Explicit non-goals

- No Zoom-specific participant API.
- No requirement for a meeting-platform SDK.
- No replacement of the current channel attribution.
- No speaker-name claim based solely on acoustic similarity.
- No distributable packaging, signing, notarization, or installer work.
