# flex-viewer

A small local web UI for watching FLEX paging traffic decoded by
[multimon-ng](https://github.com/EliasOenal/multimon-ng) in real time.

Tails a log file that `multimon-ng` writes to, parses each page, and
serves a wire-feed at `http://127.0.0.1:8732/` with type filters,
click-to-filter-by-capcode, automatic re-stitching of multi-fragment
messages, and field formatting for email-style pages.

## Requirements

- macOS (the included `decode.sh` uses CoreAudio; Linux works if you swap the
  sox input to `alsa` / `pulse` and skip BlackHole)
- Python 3.9+ (stdlib only)
- `multimon-ng` and `sox` on PATH
- An SDR feeding audio into the system — on macOS, [BlackHole](https://github.com/ExistentialAudio/BlackHole) provides a
  virtual loopback device so an SDR app (SDR++, SDR#, Gqrx) can pipe its
  demodulated NFM audio to the decoder without going through speakers.

Install on macOS:

```bash
brew install multimon-ng sox blackhole-2ch
```

## Quick start

1. **Tune your SDR** to a FLEX paging channel (US: 929–932 MHz). Demod
   mode **NFM**, bandwidth **≥15 kHz**, de-emphasis **off**, squelch
   **off**. Set the audio sink to `BlackHole 2ch`.

2. **Start the decoder** in one terminal:
   ```bash
   ./decode.sh
   ```
   This writes to `~/Documents/flex waves/live.log` by default. Keep it
   running.

3. **Start the viewer** in another terminal:
   ```bash
   python3 viewer.py
   ```
   It opens `http://127.0.0.1:8732/` in your default browser and tails
   the log.

## Features

- **Live wire feed** — newest pages on top, each row glows amber on
  arrival and fades over ~2.5 s. When a burst of pages lands together
  you see a cluster of glowing rows.
- **Type chips** — toggle ALN / NUM / TON / Test independently. State
  persists in `localStorage`. ALN pages whose body is just digits
  (callback numbers, beeper codes) are rebucketed as NUM so toggling
  that one chip catches them all.
- **Click-to-filter** — clicking any capcode fills the text filter with
  it; clear the filter to see everything again.
- **Free-text filter** — searches both capcode and message body.
- **Multi-fragment re-stitching** — FLEX caps a single page at ~248
  characters; long medical/SCADA messages get fragmented. The viewer
  merges consecutive fragments from the same capcode (within 8 s live,
  or sharing a decode timestamp in history) into one row, showing
  `N parts` after the type badge.
- **Email-style formatting** — pages packed with `Label:value` pairs
  separated by double-spaces (the FLEX convention when newlines aren't
  available) get rendered with a newline before each field.
- **Embedded HTML rendering** — some senders include `<b>` and
  `<font color="…">` tags in the payload. These are sanitized via
  DOMPurify and rendered, not shown as literal text.

## Configuration

Edit the constants near the top of `viewer.py`:

| Constant         | Default                                       |
| ---------------- | --------------------------------------------- |
| `LOG_PATH`       | `~/Documents/flex waves/live.log`             |
| `PORT`           | `8732` (binds 127.0.0.1 only)                 |
| `HISTORY_SIZE`   | `200` pages buffered for new connections      |
| `MAX_BODY`       | `4096` chars per page body                    |

The 8-second stitch window is `STITCH_WINDOW_MS` in the JS block.

## Privacy

US FLEX paging is unencrypted and legally receivable, but real traffic
on commercial channels routinely includes PHI (hospital pages with
patient names, MRNs, vitals), industrial alarms, and other content that
should not be redistributed. Use the log for personal monitoring only.
