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

## Synthetic non-speech safety run

The deterministic silence/noise fixtures were generated with
`hud.synthetic_fixtures` and run through `hud.benchmark_suite` using the local
`base.en` model alongside the provisioned AMI speech slice. The complete run
finished 3/3 fixtures with zero skips.

| Fixture | Observations | Filtered hallucinations | Committed words |
| --- | ---: | ---: | ---: |
| AMI ES2002a speech, 30 seconds | 33 | 0 | 44 |
| synthetic silence, 10 seconds | 0 | 8 | 0 |
| seeded low-level noise, 10 seconds | 0 | 0 | 0 |

The AMI rolling stable-only WER in this run was 49.33%; it is tail-biased and
does not replace the whole-slice final-quality WER above. The silence/noise
rows are safety evidence only, not speech accuracy results.

## Interim-window decision: 3 seconds versus 4 seconds

Same AMI slice, `base.en`, paced 0.8-second cadence, same hallucination filter:

| Window | First stable word | Window-to-publication | Committed words | Stable-only WER |
| ---: | --- | ---: | ---: | ---: |
| 3 seconds | `Nice.` (wrong) | 0.133 seconds | 34 | 62.67% |
| 4 seconds | `Okay.` (correct) | 0.115 seconds | 44 | 49.33% |

The 3-second candidate is rejected. It is both less accurate at the opening
boundary and less complete, while not improving publication latency. Keep the
4-second interim default until a broader speech corpus produces a different
result.

## Large-vault retrieval control

The reproducible synthetic benchmark is:

```bash
python3 -m hud.kb_benchmark --chunks 6000 --queries 40 --top-k 5 \
  --output /tmp/kb-retrieval-6000.json
```

On 2026-09-22, using the production `KBIndex`, deterministic hashing embeddings,
and SQLite FTS candidate filtering:

| Scope | Query p50 | Query p95 | Max query | Setup |
| --- | ---: | ---: | ---: | ---: |
| Unscoped 6,000 chunks | 116.7 ms | 210.3 ms | 295.9 ms | 5.37 s |
| `enterprise` tag scope | 30.2 ms | 48.0 ms | 66.3 ms | 1.70 s |

This is a retrieval-latency control, not a claim about embedding-model quality
or production hardware. Memory was not measured and is recorded as `null` by
the result schema. The tag-scoped result demonstrates the expected latency
benefit when a large vault is organized with useful frontmatter; target-machine
measurements remain the release gate.
