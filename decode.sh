#!/usr/bin/env bash
# Live FLEX decode pipeline.
# Reads audio from a CoreAudio device (default: BlackHole 2ch on macOS),
# resamples to 22050 Hz mono via sox, decodes FLEX with multimon-ng, and
# appends each page to a log file with a wall-clock timestamp.
#
# Usage:
#   ./decode.sh                              # default device + log path
#   ./decode.sh "BlackHole 2ch"              # specify input device
#   ./decode.sh "BlackHole 2ch" /tmp/my.log  # specify device + log
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEVICE="${1:-BlackHole 2ch}"
LOG_PATH="${2:-$SCRIPT_DIR/live.log}"

mkdir -p "$(dirname "$LOG_PATH")"
echo "device: $DEVICE"
echo "log:    $LOG_PATH"
echo "Ctrl-C to stop."

sox -t coreaudio "$DEVICE" -esigned-integer -b16 -r 22050 -c 1 -t raw - 2>/dev/null \
  | multimon-ng -q -t raw -a FLEX --timestamp - \
  | tee -a "$LOG_PATH"
