# Transcription latency research

This note records the external research behind the next local-transcription
pass. It is scoped to the current app-agnostic pipeline and the 24 GB
Apple-silicon Mac used for development.

## Findings

### 1. The current whisper.cpp runtime is already on the right hardware path

The upstream project treats Apple Silicon as a first-class target, with Metal,
Accelerate, Core ML, quantization, and VAD support. Its current server exposes
Flash Attention, and the installed binary reports Flash Attention enabled by
default. The upstream M4 benchmark shows a meaningful Flash Attention benefit
for larger models, so adding a redundant flag is unlikely to be a material win
on this machine.

Sources:

- https://github.com/ggml-org/whisper.cpp#readme
- https://github.com/ggml-org/whisper.cpp/blob/master/examples/server/README.md
- https://github.com/ggml-org/whisper.cpp/discussions/2995

### 2. Warm the long-lived server before capture

The first request can include model/runtime initialization. SimulStreaming's
server documentation calls out a warmup file for exactly this reason. The HUD
already starts its local server before the recorder begins capture, so a short
silence request can absorb that one-time cost without delaying the live audio
pipeline. This is now implemented as a best-effort 250 ms warmup.

Source:

- https://github.com/ufal/SimulStreaming#usage-server--real-time-from-mic

### 3. True streaming needs a stability policy, not only smaller chunks

Whisper is an offline-window model. Whisper-Streaming and its successor
SimulStreaming use local agreement / simultaneous policies with self-adaptive
latency to decide which prefix is safe to commit. The current app's stable
partial decoder already follows that principle, but its interim worker shares
the same inference lane as authoritative Turbo decoding. That is why interim
updates are sometimes suppressed even though the capture path stays healthy.

Sources:

- https://github.com/ufal/whisper_streaming
- https://github.com/ufal/SimulStreaming

### 4. MLX and faster-whisper are experiments, not immediate replacements

MLX Whisper is attractive on Apple Silicon and supports Python use, stdin, and
word timestamps, but its upstream documentation does not provide a maintained
drop-in realtime server, and its realtime issue documents the same word-cutting
problem that smaller fixed windows create. faster-whisper provides useful
Silero VAD and batched inference, but its documented performance path is CUDA;
the current M4 Metal path is better aligned with whisper.cpp. Both are worth a
side-by-side benchmark, not a blind migration.

Sources:

- https://github.com/ml-explore/mlx-examples/tree/main/whisper
- https://github.com/ml-explore/mlx-examples/issues/1258
- https://github.com/SYSTRAN/faster-whisper

### 5. Groq remains the latency/cost reference

Groq documents `whisper-large-v3-turbo` as its speed/price choice and lists a
20 RPM limit plus audio-second limits on the Developer plan. It also documents
a 10-second minimum billed length. The app's two-source guard therefore needs
to remain rate-aware; smaller remote windows are not automatically better when
two channels are active.

Sources:

- https://console.groq.com/docs/speech-to-text
- https://console.groq.com/docs/rate-limits

## Recommended next experiments

1. Measure the new warmup on the same 15-second and 45-second fixtures,
   separating first-inference latency from steady-state latency.
2. Add an optional, much smaller local interim model (base.en or small.en)
   while keeping Turbo as the authoritative final model. The interim lane can
   then remain live while Turbo is decoding, with local-agreement commits still
   protecting accuracy.
3. Benchmark an actual SimulStreaming-style policy against the current
   `StablePartialDecoder`, using word error rate and first-stable-word latency,
   not transcript smoothness alone.
4. Only after those measurements, investigate Core ML or ANEForge encoder
   acceleration. Those paths accelerate the encoder only; they are promising,
   but they do not guarantee an equivalent end-to-end latency improvement.

## Explicit non-recommendations

- Do not lower the final Turbo floor below 2.5 seconds without an accuracy
  benchmark; the 2.0-second replay lost an opening word.
- Do not replace the current runtime with MLX or faster-whisper solely because
  they advertise streaming or batching; their Apple-silicon integration and
  end-to-end behavior need to be measured in this application first.
- Do not make live WhisperX diarization part of the hot path. The current
  post-call attribution remains the safe accuracy/performance boundary.
