# zoom-recorder

Records Zoom meeting audio (speaker + microphone) with automatic device
selection, continuous capture verification, dynamic failover, and transcription.

## Requirements

- macOS with Zoom installed
- Homebrew
- Python 3 (the system `python3` is fine)

## Setup

```bash
brew install ffmpeg whisper-cpp
mkdir -p ~/.cache/whisper-cpp
curl -L -o ~/.cache/whisper-cpp/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

# optional: menu-bar start/stop toggle
pip3 install --user rumps
```

## Usage

```bash
./zoom-record.sh              # 5-min segments, devices chosen automatically
./zoom-record.sh 10           # 10-min segments
./zoom_record.py --list       # list audio inputs/outputs and exit
./zoom_record.py --self-test  # play a tone and verify the capture path
./menubar.py                  # menu-bar toggle: click to start/stop
```

Press **Ctrl+C** (or click "Stop Recording" in the menu bar) to stop. Mic and
system audio are recorded and merged as **separate original files**:

```
~/ZoomRecordings/<YYYY-MM-DD>/
  recording_mic.wav       read-only original, mic track
  recording_sys.wav       read-only original, system/loopback track (if used)
  .segments/              raw crash-safe segments, archived not deleted
  derived/
    recording_mixed.wav   mixed copy for transcription only -- not an original
    transcript.txt
  capture.log
~/ZoomRecordings/manifest.jsonl   append-only sha256 + duration + coverage log
```

### Why originals are protected

On 2026-09-16, running a third-party audio enhancer with its output pointed
at the same file as the only copy of a recording silently zeroed it out, and
the raw segments had already been cleaned up by the recorder itself. To make
that structurally impossible:

- Segments are **archived, never deleted** (`.segments/`), and are a bit-exact
  fallback since the merge is a lossless concat.
- Merged originals are **chmod 0o444** (read-only) immediately after they're
  verified.
- Every run appends a line to `manifest.jsonl` with a sha256 checksum,
  duration, and signal-coverage percentage — `sha256sum` against that line
  answers "does this still match what was recorded," forever.
- Any downstream tool (transcription, enhancement, etc.) should write into
  `derived/`, never over an original. Wrap it with `safe_derive.py`, which
  refuses to run if the declared output path lands inside a dated recording
  folder outside `derived/`:

  ```bash
  ./safe_derive.py --out ~/ZoomRecordings/2026-09-16/derived/enhanced.wav -- \
      some-enhancer --in ~/ZoomRecordings/2026-09-16/recording_mic.wav \
                     --out ~/ZoomRecordings/2026-09-16/derived/enhanced.wav
  ```

### Verification on stop

Right after merging and before anything is archived or locked, the recorder:
- Sanity-checks merged duration against the sum of segment durations (catches
  a broken concat).
- Runs a full-file silence scan and reports **% of the recording with signal**
  above `--silence-db`, plus any stretches over 60s of silence.
- If coverage is below 50%, it's flagged loudly (terminal bell + macOS
  notification) instead of being something you discover during transcription
  weeks later.

Mic and system audio are **never pre-mixed** during capture — they're kept as
separate mono originals so a later enhancement/leveling pass can be applied
per-source. `derived/recording_mixed.wav` is a disposable mixdown built only
for the transcription step.

### Menu-bar activation (`menubar.py`)

One-keystroke-equivalent start/stop with no background daemon: the menu-bar
icon (🎙 idle / 🔴 REC active) only spawns the recorder subprocess while
you're actually recording, so there's never a persistent, silent
mic-access process for an EDR to flag or that could record something by
accident. Click "Stop Recording" to send the same clean-shutdown signal
Ctrl+C would.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mic NAME` | auto | Force a microphone by name (env `MIC_AUDIO_DEVICE`) |
| `--system NAME` | auto | Force the system/loopback input (env `ZOOM_AUDIO_DEVICE`) |
| `--no-system` | off | Record the microphone only |
| `--chunk-seconds N` | 5 | How often the active mic is tested |
| `--fail-threshold N` | 3 | Consecutive silent checks before cycling inputs |
| `--cycle-seconds N` | 60 | How often every inactive input is tested |
| `--probe-seconds N` | 1.5 | Length of each signal probe |
| `--silence-db N` | -60 | `max_volume` below this counts as silence |
| `--no-transcribe` | off | Skip transcription |
| `--model PATH` | base.en | Whisper model to use |

## How It Works

- Audio devices are matched by **name**, not positional index (avfoundation's
  index order changes with what's connected and whether Zoom is outputting audio).
- The **best mic is selected automatically**: every candidate is probed with
  ffmpeg's `volumedetect`, and the pick prefers a device that is actually
  delivering signal, then falls back to a quality ranking
  (wired external > built-in > Bluetooth). A system/loopback device
  (`ZoomAudioDevice`, BlackHole, Loopback, …) is selected for meeting audio.
- Microphone and system audio are mixed and written as crash-safe segments.
- **Every `--chunk-seconds` the active mic is tested.** A dead capture reads about
  `-91 dB`, so `--silence-db` cleanly separates a dead input from a live one.
- After `--fail-threshold` consecutive silent checks, the recorder **cycles
  through every other candidate**, probes them, and switches to the best one
  that produces signal. The current capture keeps running throughout, so
  natural meeting silence never drops audio.
- Every `--cycle-seconds` all inactive inputs are tested and their levels logged.
- Every run writes `capture.log` next to the recording, documenting the devices
  selected, every probe level, and every failover.

## Troubleshooting

- **Recording is silent** — the recorder now detects this itself and will fail
  over to another input. Check `capture.log` to see which devices were probed
  and why a switch did or did not happen.
- **No system/meeting audio** — no loopback device was found. Start Zoom so
  `ZoomAudioDevice` appears, or install
  [BlackHole](https://github.com/ExistentialAudio/BlackHole) and route Zoom's
  output through it.
- **"ERROR: ZoomAudioDevice not found"** — pass `--system NAME` to choose a
  different device, or run `./zoom_record.py --list` to see what is available.
- **Is Zoom's output actually capturable?** — run `./zoom_record.py --self-test`;
  it plays a short tone and checks whether a loopback input records it.
