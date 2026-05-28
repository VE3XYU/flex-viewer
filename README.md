# flex-viewer

A local web UI for watching FLEX paging traffic decoded by
[multimon-ng](https://github.com/EliasOenal/multimon-ng) in real time.

Tails a log file written by `multimon-ng`, parses each page, and serves
a wire-feed at `http://127.0.0.1:8732/` with type filters,
click-to-filter-by-capcode, automatic re-stitching of multi-fragment
messages, and field formatting for email-style pages.

```
┌──────────────────────────────────────────────────────────┐
│ FLEX FEED   ● LIVE   320 pages   [ALN][NUM][TON][Test]   │
├──────────────────────────────────────────────────────────┤
│ 21:53:53  365030  ALN          1600/4/K/B · 13.070       │
│ │ THIS IS A TEST PERIODIC PAGE SEQUENTIAL NUMBER 8433    │
│                                                          │
│ 21:53:42  1234567  ALN  3 parts  1600/4/K/A · 13.064     │
│ │ From:Sender, J                                         │
│ │ MRN: *0000000*                                         │
│ │ Last Name: Doe                                         │
│ │ CallBack #:555-0100                                    │
│ │ Priority:High                                          │
│ │ Urgent MRI spine overnight requested...                │
└──────────────────────────────────────────────────────────┘
```

## What you need

- **macOS** (the audio pipeline uses CoreAudio; Linux works if you swap
  the sox input to `alsa` or `pulse`)
- **An SDR** with a receiver app — anything that can demod NFM and route
  audio to a virtual output works (SDR++, SDR#, Gqrx, CubicSDR, …)
- **Python 3.9+** (stdlib only, no `pip install` needed)
- **Homebrew** to install the audio + decoder tools

## First-time setup

### 1. Install the tools

```bash
brew install multimon-ng sox blackhole-2ch
```

- `multimon-ng` is the FLEX decoder
- `sox` resamples the audio from your SDR's rate to 22050 Hz mono
- `blackhole-2ch` is a virtual audio loopback device so an SDR app
  can pipe demodulated audio into the decoder without going through
  your speakers

### 2. Get the viewer

```bash
git clone https://github.com/VE3XYU/flex-viewer.git
cd flex-viewer
```

### 3. Configure your SDR app

Tune to a FLEX paging channel. In North America these are in the
**929–932 MHz** band; common active frequencies include 929.6625,
929.6125, and 929.2875 MHz. Look on the waterfall for a constant
data-modulation signature.

Demodulator settings — these matter, get them wrong and nothing
decodes:

| Setting        | Value                          |
| -------------- | ------------------------------ |
| Demod mode     | NFM (narrow FM)                |
| Bandwidth      | 25 kHz                         |
| De-emphasis    | **OFF** (this is the big one)  |
| Squelch        | **OFF**                        |
| AGC            | Fast (or off)                  |
| Audio sink     | **BlackHole 2ch**              |

De-emphasis is the most common cause of "looks like signal, decodes
as garbage" because it's the default for voice NFM. Turn it off.

### 4. (Optional) Hear the audio too

By default audio routed to BlackHole is silent. To monitor it on your
speakers as well:

1. Open **Audio MIDI Setup** (`/Applications/Utilities/Audio MIDI Setup.app`)
2. Click **+** bottom-left → **Create Multi-Output Device**
3. Tick **BlackHole 2ch** and your normal output device
4. In your SDR app, set the audio sink to the new Multi-Output Device
   instead of BlackHole directly

### 5. Run it

```bash
./start.sh
```

This launches the decoder pipeline and the web viewer together, then
opens `http://127.0.0.1:8732/` in your browser. Ctrl-C in the terminal
stops both.

If you'd rather run them separately (e.g., to watch decoder output
or send it through `tee` to multiple destinations):

```bash
./decode.sh &       # decoder in background
python3 viewer.py   # viewer in foreground
```

## Features

- **Live wire feed** — newest pages on top, each row glows amber on
  arrival and fades over ~2.5 s. When several pages land in the same
  burst, you see a cluster of glowing rows and know exactly where the
  new traffic starts.
- **Type chips** — toggle **ALN** / **NUM** / **TON** / **Test**
  independently. State persists in `localStorage`. ALN pages whose
  body is just digits (callback numbers, beeper codes) get
  reclassified as NUM so toggling that one chip hides them all.
- **Click-to-filter** — clicking any capcode fills the text filter
  with it. Clear the filter to see everything again.
- **Free-text filter** — searches both capcode and message body.
- **Multi-fragment re-stitching** — FLEX caps a single page at ~248
  characters; long medical/SCADA messages get fragmented across
  consecutive transmissions. The viewer merges fragments from the
  same capcode (within 8 s live, or sharing a decode timestamp in
  history) into one row with a `N parts` indicator.
- **Email-style formatting** — pages packed with `Label:value` pairs
  separated by double-spaces (the FLEX convention when newlines
  aren't available) render with a newline before each field.
- **Embedded HTML rendering** — some senders include `<b>` and
  `<font color="…">` tags in the payload. These are sanitized via
  DOMPurify and rendered.

## Configuration

Edit the constants near the top of `viewer.py`:

| Constant       | Default                                  |
| -------------- | ---------------------------------------- |
| `LOG_PATH`     | `~/Documents/flex waves/live.log`        |
| `PORT`         | `8732` (binds 127.0.0.1 only)            |
| `HISTORY_SIZE` | `200` pages buffered for new connections |
| `MAX_BODY`     | `4096` chars per page body               |

`STITCH_WINDOW_MS` (in the JS block) controls the live-stitching
window — 8000 ms by default.

The decoder log path is also overridable via env vars when launching:

```bash
LOG_PATH="$HOME/paging/today.log" ./start.sh
DEVICE="BlackHole 16ch" ./start.sh
```

## Troubleshooting

**Viewer loads but stays empty / "Listening for pages…"**
The decoder pipeline isn't producing output. Check that:
- SDR++ (or your app) is actually sinking to BlackHole, not the
  default speakers
- `multimon-ng` and `sox` are both on PATH (`which multimon-ng`)
- The log file is being written: `tail -f ~/Documents/flex\ waves/live.log`

**Decoder runs but no pages decode (only `Unknown Sync code` in verbose
mode)**
Signal quality issue. The decoder achieves bit-lock but the sync word
recovery is wrong. Almost always one of:
- De-emphasis is on (turn it OFF in the SDR app — this is #1 by far)
- Squelch is cutting the start of each burst
- AGC has slow attack so the first symbols get sliced at wrong levels
- You're tuned to the wrong frequency or a non-FLEX channel

**Pages get cut off mid-word**
Either the signal dropped mid-burst (improve antenna / SNR) or you're
seeing FLEX's natural 248-char-per-page limit. The viewer's
re-stitching handles the latter for consecutive same-capcode pages.

**Safari shows "127.0.0.1 refused to connect"**
The viewer isn't running. Start it with `./start.sh` or
`python3 viewer.py`.

## Privacy

FLEX paging is unencrypted, and reception is generally legal across
North America. But the live traffic on commercial channels routinely
includes PHI (hospital pages with patient names, MRNs, vitals),
industrial alarms, dispatch traffic, and other content that should
not be redistributed. Use the log for personal monitoring only.

Disclosure of intercepted communications is restricted by both
communications and privacy law in most jurisdictions — in **Canada**
by the Criminal Code (s. 193 on use/disclosure of intercepted
private communications) and federal/provincial health privacy law
(PIPEDA, plus provincial acts like Ontario PHIPA, Alberta HIA, BC
PIPA); in the **US** by ECPA and HIPAA. Treat anything you decode
as sensitive: do not publish capture logs, screenshots showing real
page content, or repo issues containing it.

