#!/usr/bin/env bash
# Start both the FLEX decoder and the web viewer.
# Ctrl-C tears down both.
#
# Usage:
#   ./start.sh                          # default device + log
#   DEVICE="BlackHole 2ch" ./start.sh   # override input device
#   LOG_PATH="/tmp/flex.log" ./start.sh # override log path
set -uo pipefail
cd "$(dirname "$0")"

DEVICE="${DEVICE:-BlackHole 2ch}"
LOG_PATH="${LOG_PATH:-$PWD/live.log}"

mkdir -p "$(dirname "$LOG_PATH")"

cleanup() {
  echo
  echo "Stopping decoder and viewer..."
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "device:  $DEVICE"
echo "log:     $LOG_PATH"
echo "viewer:  http://127.0.0.1:8732/"
echo "Ctrl-C to stop both."
echo

# Decoder in the background (entire pipeline runs in this process group)
{
  sox -t coreaudio "$DEVICE" -esigned-integer -b16 -r 22050 -c 1 -t raw - 2>/dev/null \
    | multimon-ng -q -t raw -a FLEX --timestamp - \
    | tee -a "$LOG_PATH" > /dev/null
} &

# Viewer in the foreground
python3 viewer.py
