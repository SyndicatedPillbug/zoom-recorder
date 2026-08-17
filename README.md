# zoom-recorder

Records Zoom meeting audio (speaker + microphone) with automatic transcription.

## Requirements

- macOS with Zoom installed
- Homebrew

## Setup

```bash
brew install ffmpeg whisper-cpp
mkdir -p ~/.cache/whisper-cpp
curl -L -o ~/.cache/whisper-cpp/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

## Usage

```bash
./zoom-record.sh          # 5-min segments (default)
./zoom-record.sh 10       # 10-min segments
```

Press **Ctrl+C** to stop. The script will:
1. Merge all segments into a single `recording.wav`
2. Save to `~/ZoomRecordings/<YYYY-MM-DD>/`
3. Generate a timestamped `transcript.md` via whisper-cpp

## How It Works

- Records from `ZoomAudioDevice` (Zoom's virtual audio) and `MacBook Air Microphone`
- Uses ffmpeg's segment muxer to write new files every N minutes for crash safety
- On stop, merges segments with `ffmpeg -f concat`
- Transcribes with `whisper-cpp` (base.en model)
