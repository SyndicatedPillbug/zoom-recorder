# Voice detection research and benchmark

Updated 2026-09-23 after testing the longer one-sided microphone recording
`~/ZoomRecordings/2026-09-23/06-48-13_5b9f00ce/`.

## Benchmark result

The recording is a healthy 48.83-second, 48 kHz mono microphone capture.
The recorder's verification reported 100% microphone signal coverage, with a
mean level of approximately -46.9 dBFS and a peak of -20.5 dBFS.

Replaying the source through the original live detector after downsampling to
16 kHz produced:

- Original peak-based adaptive energy VAD (`-50 dBFS` absolute floor, `6 dB`
  margin): 98.0% of 100 ms frames classified as speech.
- The detected speech formed one continuous region from approximately 1.0 s
  through the end of the recording.
- WebRTC VAD was not installed in the current Python environment, so the
  application's `auto` backend correctly falls back to adaptive energy VAD.
- The system/loopback track in this recording was silent because the default
  output was not yet the Multi-Output Device. This is a routing failure, not a
  voice-detection failure.

After the RMS/crest/two-frame hardening below, the same normalized recording
produced 0.0% speech frames. Whisper's direct full-file decode returned only
repeated `Thank you` filler rather than intelligible transcript content, so
this sample is best treated as a false-positive/noise stress test, not as a
speech-accuracy benchmark.

### Interpretation

This sample does not justify lowering the current speech threshold. The
original gate was clearly too permissive for this recording, while the
hardened gate correctly refused to forward it. Lowering the threshold would
increase the chance that fan, hum, keyboard transients, and empty-call audio
reach Whisper—the exact path that caused the earlier hallucinated “oh, oh,
oh, oh” and slide-navigation lines. A real speech recording is still needed
before changing the default toward greater sensitivity.

The immediate audio problem was routing. `zoom_record.py --fix-routing` now
repairs the existing Multi-Output Device, and `--check-routing` confirmed that
a test tone reached BlackHole after the repair. Startup logging now records
both successful reuse and explicit routing-fix failures instead of silently
continuing after a failed repair.

## Implemented hardening after the benchmark

The benchmark exposed a real weakness in the original energy gate: it relied
on peak energy, so a low-level noise bed with occasional peaks could look like
98% speech. The live gate now:

- makes its adaptive decision on RMS energy rather than a single peak sample;
- rejects frames with an excessive peak-to-RMS crest ratio, which filters
  isolated clicks and spikes; and
- requires two consecutive qualifying frames before opening speech.

The peak level is still retained for diagnostics, and the stronger Whisper
confidence and hallucination filters remain in place.

## Research findings

1. The current two-stage design is sound: a cheap source-local gate first,
   then Whisper confidence/hallucination checks. A VAD should reduce the audio
   presented to Whisper, not replace the transcript evidence checks.
2. The official whisper.cpp VAD path supports Silero VAD and exposes the
   controls that matter for live use: speech threshold, minimum speech,
   minimum silence, speech padding, and inter-window overlap. See the
   [whisper.cpp VAD documentation](https://github.com/ggml-org/whisper.cpp#voice-activity-detection-vad)
   and its [VAD segmentation example](https://github.com/ggml-org/whisper.cpp/tree/master/examples/vad-speech-segments).
3. WebRTC VAD is a reasonable low-cost optional backend, but it is not
   installed here and should remain optional. Its 20 ms framing makes it useful
   as a conservative second opinion, not as a replacement for the current
   adaptive floor on quiet microphones.
4. The next robust upgrade is a calibrated, stateful neural VAD lane with
   hysteresis: require consecutive speech frames to open, retain a short
   pre-roll, and require consecutive silence frames to close. This should be
   benchmarked against real speech, room noise, humming, clicks, and empty
   meeting audio before becoming the default.

## Recommended next experiment

Collect a small local benchmark set with four labels—speech, silence, hum,
and transient noise—and report false opens, missed speech, speech-boundary
latency, and Whisper hallucination count. Run the current energy detector,
optional WebRTC VAD, and Silero VAD against the same files. Do not select a
new default from a single clean one-sided recording.

The present implementation keeps the low-latency adaptive energy path as the
default, preserves the stronger Whisper evidence gates, and leaves the neural
VAD upgrade as an evidence-driven next phase rather than adding a model and
new runtime dependency without measured benefit.
