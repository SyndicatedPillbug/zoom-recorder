# Local Runtime Portability Plan

Status: active, implementation started 2026-09-23

## Product decision

Speaker diarization is a core feature and remains enabled by default. The
normal product path must run on-device and must not require a Hugging Face
account, token, or cloud diarization service. Model acquisition may be an
explicit setup step; inference after the model is cached is local.

## Supported acceleration tiers

1. Apple Silicon macOS: NeMo-Speech.cpp Sortformer through Metal.
2. Linux AMD: NeMo-Speech.cpp Sortformer through Vulkan. ROCm/HIP remains a
   separate provider path to add only when the selected runtime and model are
   genuinely supported and benchmarked.
3. Linux or unsupported accelerator: native CPU fallback, clearly reported as
   a degraded performance tier rather than silently substituted.

The application owns backend selection and reports the selected runtime,
model, accelerator, and fallback reason in the Control Center.

## Provider boundaries

- Transcription: existing local whisper.cpp lane, with provider fallbacks
  explicitly configured.
- Turn diarization: local NeMo-Speech.cpp adapter.
- Persistent speaker identity: separate local voice-embedding provider. A
  session-local `Speaker 1` label is never treated as a person identity.
- Text retrieval: local lexical index always available; local semantic model
  preferred; remote embeddings opt-in and visible.

This separation keeps model and licensing decisions replaceable if the project
later becomes open source, closed source, or a paid product.

## Measurable exit criteria

- MacBook Air: local diarization completes on a 30-minute meeting without an
  online credential and records backend/model/real-time factor metadata.
- Linux AMD XTX: the same test selects Vulkan when healthy and records a CPU
  fallback only when acceleration is unavailable.
- No diarization code path sends audio or credentials to a network service.
- A missing runtime/model produces a visible setup state and never silently
  disables the feature.
- A manually confirmed speaker name is persisted and can be evaluated against
  a later call using the local identity provider.
- Context folder selection is snapshotted per meeting, and an edited source
  is reported as `needs_index` before it can be used for answer context.

## Current implementation

- NeMo runtime and local Sortformer model installed and verified on the Mac.
- `auto` diarization now selects local NeMo rather than token-gated WhisperX.
- Metal/Vulkan/CPU device selection is persisted in settings; legacy ROCm
  selections are safely mapped to the native AMD Vulkan path.
- Next-meeting context folders are separate from the permanent KB library and
  are written to `derived/context_sources.json` at session start.

## Next work

1. Add a capability doctor that actually probes Vulkan on Linux and records
   ROCm availability separately for future providers.
2. Add local voice-embedding inference without pyannote token requirements.
3. Add hardware benchmark fixtures and run them on both target machines.
4. Add model/license inventory and redistribution notes before packaging.
