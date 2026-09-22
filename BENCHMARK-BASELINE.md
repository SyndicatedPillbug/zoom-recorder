# Benchmark Baseline

Recorded: 2026-09-22  
Release reference: `v2.7`  
Fixture: external `/private/tmp/ami-es2002a-50-80.wav`, 30 seconds, 16 kHz mono PCM  
Reference: `fixtures/ami-es2002a-50-80.reference.json`

The audio is intentionally outside git. The fixture manifest records its identity
and provenance; the local file must be provisioned before a benchmark can run.

## Final-quality control

The aligned whole-slice comparison recorded in
`RESEARCH-TRANSCRIPTION-LATENCY.md` is the authoritative quality baseline:

| Model | Whole-slice WER |
| --- | ---: |
| Turbo q5_0 | 20.0% |
| `base.en` | 21.3% |

These numbers come from the complete aligned 50–80 second AMI comparison, not
from the rolling stable-prefix output below.

## Paced interim control

Command:

```bash
python3 -m hud.audio_benchmark /private/tmp/ami-es2002a-50-80.wav \
  --model ~/.cache/whisper-cpp/ggml-base.en.bin \
  --reference fixtures/ami-es2002a-50-80.reference.json \
  --window-seconds 4 --interval-seconds 0.8 \
  --output /private/tmp/zoom-recorder-base-4s-control.json
```

Observed:

- 33 rolling observations.
- Inference p50: 0.142 seconds; p95: 0.217 seconds.
- First stable word: `Okay.`
- First stable publication: 4.940 seconds into the clip, 0.140 seconds after
  the 4.8-second window completed.
- 44 committed words.
- Stable-only WER: 49.33%, intentionally tail-biased because the decoder
  withholds the newest unstable suffix.
- Per-window WER p50: 50%; p95: 100%. Diagnostic only until more fixtures and
  speech-boundary handling exist.

## Interpretation

The 4-second `base.en` setting remains the safe interim control. It produced the
correct first stable word with low post-window publication cost. Stable-only WER
must not be used to claim final transcription quality. Any candidate interim
setting must beat this control on a broader corpus without violating the final
Turbo WER or queue/drop gates.
