#!/usr/bin/env bash
# deus listen — Record from mic, transcribe with whisper.cpp, copy to clipboard.
# Phase 1: Pure shell, cross-platform (macOS, Linux, Windows/Git Bash).
set -euo pipefail

# ── Config (env vars override defaults) ──────────────────────────────────────
WHISPER_BIN="${WHISPER_BIN:-whisper-cli}"
WHISPER_MODEL="${WHISPER_MODEL:-$(cd "$(dirname "$0")/.." && pwd)/data/models/ggml-base.bin}"
WHISPER_LANG="${WHISPER_LANG:-en}"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
NO_CLIPBOARD="${DEUS_LISTEN_NO_CLIPBOARD:-0}"

# ── Dependency checks ────────────────────────────────────────────────────────
missing=()
command -v sox  >/dev/null 2>&1 || missing+=("sox")
command -v "$WHISPER_BIN" >/dev/null 2>&1 || missing+=("$WHISPER_BIN")
command -v ffmpeg >/dev/null 2>&1 || missing+=("ffmpeg")

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing dependencies: ${missing[*]}"
  echo ""
  echo "Install with:"
  case "$(uname -s)" in
    Darwin)  echo "  brew install sox whisper-cpp ffmpeg" ;;
    Linux)   echo "  sudo apt install sox libsox-fmt-all ffmpeg"
             echo "  # whisper-cpp: build from source (https://github.com/ggerganov/whisper.cpp)" ;;
    MINGW*|MSYS*|CYGWIN*)
             echo "  choco install sox.portable ffmpeg"
             echo "  # whisper-cpp: download from GitHub releases" ;;
  esac
  exit 1
fi

# ── Auto-download model on first use ─────────────────────────────────────────
if [ ! -f "$WHISPER_MODEL" ]; then
  echo "Whisper model not found at: $WHISPER_MODEL"
  echo "Downloading ggml-base.bin (148 MB)..."
  mkdir -p "$(dirname "$WHISPER_MODEL")"
  curl -L --progress-bar -o "$WHISPER_MODEL" "$MODEL_URL"
  echo "Download complete."
  echo ""
fi

# ── Clipboard helper ─────────────────────────────────────────────────────────
copy_to_clipboard() {
  if [ "$NO_CLIPBOARD" = "1" ]; then return 1; fi
  case "$(uname -s)" in
    Darwin)
      echo -n "$1" | pbcopy && return 0 ;;
    Linux)
      if command -v xclip >/dev/null 2>&1; then
        echo -n "$1" | xclip -selection clipboard && return 0
      elif command -v xsel >/dev/null 2>&1; then
        echo -n "$1" | xsel --clipboard --input && return 0
      elif command -v wl-copy >/dev/null 2>&1; then
        echo -n "$1" | wl-copy && return 0
      fi ;;
    MINGW*|MSYS*|CYGWIN*)
      echo -n "$1" | clip.exe && return 0 ;;
  esac
  return 1
}

# ── Recording ────────────────────────────────────────────────────────────────
TMPFILE="$(mktemp "${TMPDIR:-/tmp}/deus-voice-XXXXXX.wav")"
cleanup() { rm -f "$TMPFILE"; }
trap cleanup EXIT

REC_PID=""
RECORDING=true

stop_recording() {
  RECORDING=false
  if [ -n "$REC_PID" ] && kill -0 "$REC_PID" 2>/dev/null; then
    kill "$REC_PID" 2>/dev/null
    wait "$REC_PID" 2>/dev/null || true
  fi
}

# Ctrl+C stops recording (not the script)
trap stop_recording INT

echo ""
echo "  Recording... (press Enter or Ctrl+C to stop)"
echo ""

# Start recording in background: 16kHz mono WAV (whisper.cpp format)
# Redirect sox's progress output to /dev/null — we show our own indicator.
rec -q -r 16000 -c 1 -b 16 "$TMPFILE" 2>/dev/null &
REC_PID=$!

# Show recording indicator with elapsed time
START_TIME=$(date +%s)
FRAMES=("  ●" "  ◉" "  ○" "  ◉")
FRAME_IDX=0

while $RECORDING && kill -0 "$REC_PID" 2>/dev/null; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TIME))
  MINS=$((ELAPSED / 60))
  SECS=$((ELAPSED % 60))
  FRAME="${FRAMES[$FRAME_IDX]}"
  printf "\r  %s  Recording  %d:%02d  " "$FRAME" "$MINS" "$SECS"
  FRAME_IDX=$(( (FRAME_IDX + 1) % ${#FRAMES[@]} ))

  # Check for Enter key (non-blocking read with timeout)
  if read -t 0.4 -n 1 2>/dev/null; then
    stop_recording
    break
  fi
done

# Reset the Ctrl+C trap to default
trap - INT

printf "\r  %-40s\n" ""

# Check we actually recorded something
if [ ! -f "$TMPFILE" ] || [ ! -s "$TMPFILE" ]; then
  echo "  No audio recorded."
  exit 1
fi

FILE_SIZE=$(wc -c < "$TMPFILE" | tr -d ' ')
# 16kHz * 2 bytes * 1 channel = 32000 bytes/sec. 0.5s minimum.
if [ "$FILE_SIZE" -lt 16000 ]; then
  echo "  Recording too short (< 0.5s). Try again."
  exit 1
fi

# ── Transcription ────────────────────────────────────────────────────────────
echo "  Transcribing..."
echo ""

TRANSCRIPT=$("$WHISPER_BIN" -m "$WHISPER_MODEL" -f "$TMPFILE" --no-timestamps -nt -l "$WHISPER_LANG" 2>/dev/null || true)
TRANSCRIPT=$(echo "$TRANSCRIPT" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$')

if [ -z "$TRANSCRIPT" ]; then
  echo "  Could not transcribe audio. Try speaking louder or longer."
  exit 1
fi

echo "  $TRANSCRIPT"
echo ""

# ── Clipboard ────────────────────────────────────────────────────────────────
if copy_to_clipboard "$TRANSCRIPT"; then
  echo "  Copied to clipboard. Paste with Cmd+V / Ctrl+V."
else
  echo "  (clipboard not available — copy the text above)"
fi
echo ""
