#!/bin/bash
# Records Zoom + microphone audio, auto-merges on stop, and transcribes.
# Segments are written every SEGMENT_MINUTES minutes for crash safety.
# On Ctrl+C: segments merge → ~/ZoomRecordings/<date>/recording.wav → transcript.md
#
# Usage:
#   ./zoom-record.sh          # default 5-min segments
#   ./zoom-record.sh 10       # 10-min segments

set -euo pipefail

SEGMENT_MINUTES="${1:-5}"
BASEDIR="$HOME/ZoomRecordings"
DATE="$(date +%Y-%m-%d)"
OUTDIR="$BASEDIR/$DATE"
TMPDIR="/tmp/zoom_segments_$$"
MODEL="$HOME/.cache/whisper-cpp/ggml-base.en.bin"

mkdir -p "$OUTDIR" "$TMPDIR"

PATTERN="$TMPDIR/seg_%04d.wav"

cleanup() {
  echo ""
  echo "Stopping recording..."

  # Kill ffmpeg if still running
  if [[ -n "${FFMPEG_PID:-}" ]] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
    kill "$FFMPEG_PID" 2>/dev/null || true
    wait "$FFMPEG_PID" 2>/dev/null || true
  fi

  # Merge segments
  SEG_COUNT=$(find "$TMPDIR" -name 'seg_*.wav' 2>/dev/null | wc -l)
  if [[ "$SEG_COUNT" -eq 0 ]]; then
    echo "No segments recorded."
    rm -rf "$TMPDIR"
    exit 1
  fi

  echo "Merging $SEG_COUNT segments..."
  MERGED="$OUTDIR/recording.wav"

  if [[ "$SEG_COUNT" -eq 1 ]]; then
    mv "$TMPDIR"/seg_*.wav "$MERGED"
  else
    # Build concat list
    CONCAT_LIST="$TMPDIR/concat.txt"
    : > "$CONCAT_LIST"
    for f in $(ls "$TMPDIR"/seg_*.wav | sort); do
      echo "file '$f'" >> "$CONCAT_LIST"
    done
    ffmpeg -y -f concat -safe 0 -i "$CONCAT_LIST" -c copy "$MERGED" 2>/dev/null
  fi

  echo "Saved: $MERGED"

  # Transcribe
  if [[ -f "$MODEL" ]]; then
    echo "Transcribing (this may take a while)..."
    TRANSCRIPT="$OUTDIR/transcript.md"
    whisper-cpp \
      -m "$MODEL" \
      -f "$MERGED" \
      --output-format md \
      --output-dir "$OUTDIR" \
      2>/dev/null

    # whisper-cpp names output after input file; rename to transcript.md
    WHISPER_OUT="$OUTDIR/recording.md"
    if [[ -f "$WHISPER_OUT" ]]; then
      mv "$WHISPER_OUT" "$TRANSCRIPT"
    fi
    echo "Transcript: $TRANSCRIPT"
  else
    echo "WARNING: Model not found at $MODEL — skipping transcription."
    echo "Download it: curl -L -o $MODEL https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
  fi

  # Cleanup temp segments
  rm -rf "$TMPDIR"
  echo "Done. Files in $OUTDIR/"
  exit 0
}

trap cleanup SIGINT SIGTERM

echo "Recording Zoom + mic → $OUTDIR"
echo "Segment length: ${SEGMENT_MINUTES} min"
echo "Press Ctrl+C to stop and merge."

ffmpeg \
  -f avfoundation \
  -thread_queue_size 1024 \
  -i ":0" \
  -f avfoundation \
  -thread_queue_size 1024 \
  -i ":1" \
  -filter_complex "[0:a][1:a]amix=inputs=2:duration=longest[a]" \
  -map "[a]" \
  -ac 2 \
  -ar 48000 \
  -c:a pcm_s16le \
  -f segment \
  -segment_time $((SEGMENT_MINUTES * 60)) \
  -reset_timestamps 1 \
  "$PATTERN" &

FFMPEG_PID=$!
wait "$FFMPEG_PID" 2>/dev/null || true
