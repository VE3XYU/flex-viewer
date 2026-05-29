#!/usr/bin/env python3
"""FLEX live page viewer — tails live.log, serves a local web UI."""
import http.server
import json
import os
import queue
import re
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "live.log"
PORT = 8732
HISTORY_SIZE = 200
MAX_BODY = 4096
NPA_NXX_PATH = Path(__file__).resolve().parent / "data" / "npa-nxx-on.json"

_state_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_history: deque = deque(maxlen=HISTORY_SIZE)
_next_id = 0
_id_lock = threading.Lock()
_npa_nxx: dict[str, str] = {}  # "905201" -> "Markham, ON"; loaded once in main()


def alloc_id() -> int:
    global _next_id
    with _id_lock:
        _next_id += 1
        return _next_id


def load_npa_nxx() -> None:
    """Load+invert the bundled NPA-NXX table. Missing file disables hints."""
    global _npa_nxx
    table = {}
    try:
        with NPA_NXX_PATH.open("r", encoding="utf-8") as f:
            grouped = json.load(f)
        for place, codes in grouped.items():
            for code in codes:
                table[code] = place
    except Exception:
        _npa_nxx = {}
        return
    _npa_nxx = table


def phone_hints(body: str) -> list:
    """Detect NANP numbers in body and map NPA-NXX -> town. Best-effort."""
    if not _npa_nxx:
        return []
    hints = []
    seen = set()
    for m in PHONE_RE.finditer(body):
        place = _npa_nxx.get(m.group(1) + m.group(2))
        if not place:
            continue
        num = m.group(0).strip()
        if num in seen:
            continue
        seen.add(num)
        hints.append({"num": num, "place": place})
    return hints


PIPE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}): (?P<proto>FLEX(?:_NEXT)?)"
    r"\|[^|]+\|(?P<mode>[^|]+)\|(?P<frame>[^|]+)\|(?P<capcode>[^|]+)"
    r"\|(?P<type>[^|]+)\|(?P<body>.*)$"
)
LEGACY_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}): (?P<proto>FLEX(?:_NEXT)?): "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
    r"(?P<mode>\S+) (?P<frame>\S+) \[(?P<capcode>\d+)\] "
    r"(?P<type>\S+) (?P<body>.*)$"
)
HEADER_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}: FLEX")
# NANP 10-digit number in free text. Group 1 = NPA, group 2 = NXX.
# Lookarounds reject digit runs, FLEX frames (12.045) and hyphen ranges.
PHONE_RE = re.compile(
    r"(?<![\w.\-])(?:\+?1[ .\-]?)?\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?![\w.\-])"
)


def parse_record(text: str):
    lines = text.splitlines()
    if not lines:
        return None
    first = lines[0]
    extra = "\n".join(lines[1:])
    m = PIPE_RE.match(first) or LEGACY_RE.match(first)
    if not m:
        return None
    d = m.groupdict()
    body = d["body"]
    if extra:
        body = f"{body}\n{extra}"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "…"
    return {
        "id": alloc_id(),
        "ts": d["ts"][11:],
        "date": d["ts"][:10],
        "proto": d["proto"],
        "mode": d["mode"],
        "frame": d["frame"],
        "capcode": d["capcode"].lstrip("0") or "0",
        "type": d["type"],
        "body": body,
        "hints": phone_hints(body),
    }


def broadcast(rec: dict) -> None:
    payload = ("data: " + json.dumps(rec) + "\n\n").encode("utf-8")
    with _state_lock:
        _history.append(rec)
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


def flush(buf: list[str]) -> None:
    if not buf:
        return
    rec = parse_record("\n".join(buf))
    if rec:
        broadcast(rec)


def prime_history() -> None:
    if not LOG_PATH.exists():
        return
    with LOG_PATH.open("r", errors="replace") as f:
        text = f.read()
    records, buf = [], []
    for line in text.splitlines():
        if HEADER_RE.match(line):
            if buf:
                rec = parse_record("\n".join(buf))
                if rec:
                    records.append(rec)
            buf = [line]
        elif buf:
            buf.append(line)
    if buf:
        rec = parse_record("\n".join(buf))
        if rec:
            records.append(rec)
    with _state_lock:
        for r in records[-HISTORY_SIZE:]:
            _history.append(r)


def tail_log() -> None:
    while not LOG_PATH.exists():
        time.sleep(0.5)
    with LOG_PATH.open("r", errors="replace") as f:
        f.seek(0, 2)
        buf: list[str] = []
        idle = 0.0
        while True:
            line = f.readline()
            if not line:
                idle += 0.5
                if buf and idle > 2.0:
                    flush(buf)
                    buf = []
                    idle = 0.0
                time.sleep(0.5)
                continue
            idle = 0.0
            line = line.rstrip("\n")
            if HEADER_RE.match(line):
                flush(buf)
                buf = [line]
            elif buf:
                buf.append(line)


CSS = """
:root {
  --bg: #0a0c10;
  --surface: #13161c;
  --border: #1f242e;
  --text: #e8ecf3;
  --dim: #6b7280;
  --muted: #9aa3b0;
  --accent: #ff9b40;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
header {
  position: sticky; top: 0; z-index: 10;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  background: rgba(10, 12, 16, 0.85);
  border-bottom: 1px solid var(--border);
  padding: 14px 32px;
  display: flex; align-items: center; gap: 28px;
}
h1 {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.16em;
  color: var(--text);
}
.live {
  display: flex; align-items: center; gap: 9px;
  font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.12em;
  font-weight: 500;
}
.live .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent);
  animation: pulse 2.2s ease-in-out infinite;
}
.live.offline .dot { background: var(--dim); animation: none; }
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(255,155,64,0.45); }
  70%  { box-shadow: 0 0 0 8px rgba(255,155,64,0);  }
  100% { box-shadow: 0 0 0 0 rgba(255,155,64,0);    }
}
.count {
  font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}
.chips {
  display: flex; gap: 7px; align-items: center;
  margin-left: 4px;
}
.chip {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 3px 6px 3px 11px;
  font-family: inherit;
  font-size: 10.5px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
  display: inline-flex; align-items: center; gap: 7px;
  font-weight: 500;
  transition: opacity 140ms, color 120ms, border-color 120ms;
}
.chip:hover { color: var(--text); border-color: var(--dim); }
.chip:focus-visible { outline: none; border-color: var(--accent); }
.chip .count {
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  color: var(--dim);
  letter-spacing: 0;
  padding: 1px 6px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  min-width: 22px;
  text-align: center;
}
.chip.off {
  opacity: 0.34;
  border-style: dashed;
}
.chip.off:hover { opacity: 0.6; }
.filter-wrap {
  margin-left: auto;
  position: relative;
  display: inline-flex;
  align-items: center;
}
input[type=text] {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  padding: 6px 28px 6px 10px;
  font-family: inherit;
  font-size: 13px;
  width: 240px;
  transition: border-color 120ms, background 120ms;
}
input[type=text]:focus {
  outline: none;
  border-color: var(--accent);
  background: #161a22;
}
input[type=text]::placeholder { color: var(--dim); }
.filter-clear {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  background: transparent;
  border: none;
  color: var(--dim);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 3px;
  display: none;
  font-family: inherit;
  transition: color 120ms, background 120ms;
}
.filter-clear:hover { color: var(--text); background: rgba(255,255,255,0.06); }
.filter-clear:focus-visible { outline: none; color: var(--accent); }
.filter-clear.visible { display: block; }
main {
  max-width: 960px;
  margin: 0 auto;
  padding: 6px 32px 96px;
}
.page {
  padding: 18px 20px 20px;
  margin: 0 -20px;
  border-bottom: 1px solid var(--border);
  animation: slide-in 280ms ease-out;
}
.page.test { opacity: 0.42; }
.page.test:hover { opacity: 0.75; }
.page.fresh {
  animation: slide-in 280ms ease-out, glow 2600ms ease-out;
}
@keyframes slide-in {
  from { opacity: 0; transform: translateY(-6px); }
  to   { opacity: 1; transform: none; }
}
@keyframes glow {
  0%   { background-color: rgba(255, 155, 64, 0.16); }
  18%  { background-color: rgba(255, 155, 64, 0.16); }
  100% { background-color: transparent; }
}
.meta {
  display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--muted);
  margin-bottom: 9px;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}
.ts {
  color: var(--accent);
  font-weight: 500;
}
.capcode {
  color: var(--text);
  font-weight: 500;
  font-size: 12px;
  cursor: pointer;
  padding: 1px 5px;
  margin: -1px -5px;
  border-radius: 3px;
  transition: background 100ms;
}
.capcode:hover { background: rgba(255, 155, 64, 0.14); }
.badge {
  font-size: 10.5px;
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 3px;
  color: var(--muted);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 500;
}
.badge.ALN { color: #a8d5a8; border-color: #2a3a2a; }
.badge.NUM { color: #a8c1d5; border-color: #2a323a; }
.channel {
  color: var(--dim);
  font-size: 11px;
  margin-left: auto;
  letter-spacing: 0.02em;
}
.proto {
  color: var(--dim);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.parts {
  color: var(--accent);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  font-weight: 500;
}
.body {
  font-family: 'IBM Plex Mono', ui-monospace, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
  padding-left: 14px;
  border-left: 1px solid var(--border);
  margin-left: 2px;
}
.body font { font-family: inherit; }
.body b, .body strong { color: #fff; font-weight: 600; }
.empty {
  padding: 96px 0;
  text-align: center;
  color: var(--dim);
  font-size: 13px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 500;
}
::selection { background: var(--accent); color: #0a0c10; }
"""


JS = r"""
const list = document.getElementById('list');
const filterInput = document.getElementById('filter');
const filterClear = document.getElementById('filterClear');
const countEl = document.getElementById('count');
const statusEl = document.getElementById('status');
const statusText = document.getElementById('statusText');

function syncClearVisibility() {
  filterClear.classList.toggle('visible', filterInput.value.length > 0);
}

function clearFilter() {
  filterInput.value = '';
  filter = '';
  syncClearVisibility();
  rerender();
}

const SANITIZE_CONFIG = {
  ALLOWED_TAGS: ['b', 'i', 'u', 'em', 'strong', 'br', 'font'],
  ALLOWED_ATTR: ['color', 'style'],
};

const pages = [];
let filter = '';
const STITCH_WINDOW_MS = 8000;

const ALL_TYPES = ['ALN', 'NUM', 'TON', 'TEST'];
const STORAGE_KEY = 'flexViewer.enabledTypes';
let enabledTypes;
try {
  const raw = localStorage.getItem(STORAGE_KEY);
  enabledTypes = new Set(raw ? JSON.parse(raw) : ALL_TYPES);
} catch (e) {
  enabledTypes = new Set(ALL_TYPES);
}

function isTest(body) {
  return /THIS IS A TEST PERIODIC PAGE/i.test(body);
}

function frameDistance(a, b) {
  // OTA frame distance between two "CC.FFF" strings, handling cycle 14 -> 0
  // wraparound. FLEX has 15 cycles per hour, 128 frames per cycle.
  const parts = (s) => {
    const m = String(s).trim().match(/^(\d+)\.(\d+)$/);
    return m ? [parseInt(m[1], 10), parseInt(m[2], 10)] : null;
  };
  const pa = parts(a); const pb = parts(b);
  if (!pa || !pb) return Infinity;
  const ai = pa[0] * 128 + pa[1];
  const bi = pb[0] * 128 + pb[1];
  const span = 15 * 128;
  let d = Math.abs(ai - bi);
  if (d > span / 2) d = span - d;
  return d;
}

function looksComplete(body) {
  // Fragments cut by the 248-char FLEX limit almost never end on punctuation;
  // they end mid-word. If prev ends with a terminator, treat as complete.
  const trimmed = String(body || '').trim();
  if (!trimmed) return true;
  return /[.!?;)\]>]$/.test(trimmed);
}

function shouldStitch(prev, cur) {
  if (!prev || !cur) return false;
  // Only ALN messages fragment. NUM and TON are short and self-contained.
  if (prev.type !== 'ALN' || cur.type !== 'ALN') return false;
  if (prev.capcode !== cur.capcode) return false;
  if (prev.proto !== cur.proto) return false;
  if (isTest(prev.body) || isTest(cur.body)) return false;
  // Don't merge a complete-looking prior with anything — likely a repeat,
  // not a continuation (e.g., recurring "Battery Low ..." alerts).
  if (looksComplete(prev.body)) return false;
  // Fragments arrive in consecutive frames for the same recipient. Allow
  // up to 5 frames of slack (~9.4 s OTA) to handle frame-group bouncing.
  if (frameDistance(prev.frame, cur.frame) > 5) return false;
  return true;
}

function findStitchTarget(cur) {
  // Scan recent pages (not just pages[0]) — other capcodes' pages can
  // interleave between our two fragments. Cap the scan for performance.
  const LIMIT = 30;
  for (let i = 0; i < Math.min(pages.length, LIMIT); i++) {
    if (shouldStitch(pages[i], cur)) return pages[i];
  }
  return null;
}

const DIGITS_ONLY = /^[\d\s\-]+$/;
function bucket(p) {
  if (isTest(p.body)) return 'TEST';
  // ALN messages that are really just numeric content (callback codes,
  // phone numbers, beeper codes) get rebucketed as NUM so the NUM chip
  // can hide them in one place.
  if (p.type === 'ALN' && DIGITS_ONLY.test(p.body.trim())) return 'NUM';
  return ALL_TYPES.includes(p.type) ? p.type : p.type;
}

function matches(p) {
  if (!enabledTypes.has(bucket(p))) return false;
  if (!filter) return true;
  return p.capcode.includes(filter) || p.body.toLowerCase().includes(filter);
}

function refreshChipCounts() {
  const counts = { ALN: 0, NUM: 0, TON: 0, TEST: 0 };
  for (const p of pages) {
    const b = bucket(p);
    if (counts[b] != null) counts[b]++;
  }
  for (const t of ALL_TYPES) {
    const el = document.querySelector('[data-type-count="' + t + '"]');
    if (el) el.textContent = counts[t];
  }
}

function applyChipState() {
  document.querySelectorAll('.chip').forEach(c => {
    if (enabledTypes.has(c.dataset.type)) {
      c.classList.remove('off');
      c.setAttribute('aria-pressed', 'true');
    } else {
      c.classList.add('off');
      c.setAttribute('aria-pressed', 'false');
    }
  });
}

document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => {
    const t = c.dataset.type;
    if (enabledTypes.has(t)) enabledTypes.delete(t);
    else enabledTypes.add(t);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...enabledTypes])); } catch (e) {}
    applyChipState();
    rerender();
  });
});
applyChipState();

function el(tag, className, textValue) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (textValue != null) e.textContent = textValue;
  return e;
}

function structureBody(text) {
  // Per-line: find "Label:" anchors. If a line has 2+ of them, treat it
  // as a packed tabular line and insert a newline before each (except
  // one at the very start).
  //
  // Two alternatives in the anchor regex:
  //   A) Short 2-char labels like "ID:", "Re:", "Fr:" — must end at the
  //      colon immediately (no multi-word extension), so "PM Time..."
  //      can't sneak through.
  //   B) 3+ char labels with optional multi-word body up to ~20 chars,
  //      e.g. "Date:", "Last Name:", "Time/date offset:", "CallBack #:".
  //
  // First-word class excludes underscore (`_`) so underscore-joined
  // value identifiers don't get swallowed as one giant label.
  // Negative lookahead rejects URLs ("HTTP://...") by refusing a `/`
  // right after the colon. Plain word chars or end of line are fine,
  // which is what catches "Time/date offset:UTC".
  // Continuation allows single spaces but NOT consecutive spaces — `  `
  // is the FLEX field separator. Without this guard, a multi-word match
  // would eat across the `  ` and absorb the prior field's value into
  // the next label.
  const labelRe = /(?:^|\s)((?:[A-Z][a-zA-Z0-9/.\-]:|[A-Z][a-zA-Z0-9/.\-]{2,14}(?:[a-zA-Z0-9/#.\-]| (?! )){0,20}?:))(?!\/)/g;
  const lines = String(text).split('\n');
  const out = [];
  for (const line of lines) {
    const hits = [];
    for (const m of line.matchAll(labelRe)) {
      const labelStart = m.index + (m[0].length - m[1].length);
      hits.push({ start: labelStart, end: labelStart + m[1].length, label: m[1] });
    }
    if (hits.length < 2) {
      out.push(line);
      continue;
    }
    let rebuilt = '';
    let cursor = 0;
    for (const h of hits) {
      const between = line.slice(cursor, h.start).replace(/[ \t]+$/, '');
      rebuilt += (h.start === 0) ? h.label : (between + '\n' + h.label);
      cursor = h.end;
    }
    // After the last label, any remaining `  +` is also a FLEX field
    // separator — typically the boundary between the final labelled
    // value and a trailing freeform message body. Break on it too.
    rebuilt += line.slice(cursor).replace(/  +/g, '\n');
    out.push(rebuilt);
  }
  return out.join('\n');
}

function renderBody(text) {
  const div = el('div', 'body');
  const structured = structureBody(String(text));
  const withBreaks = structured.replace(/\n/g, '<br>');
  // DOMPurify is the only thing allowed to write HTML into the DOM.
  div.innerHTML = DOMPurify.sanitize(withBreaks, SANITIZE_CONFIG);
  return div;
}

function makePage(p, fresh) {
  const test = isTest(p.body);
  const article = document.createElement('article');
  article.className = 'page' + (test ? ' test' : '') + (fresh ? ' fresh' : '');
  article.dataset.id = p.id;

  const meta = el('div', 'meta');
  meta.appendChild(el('span', 'ts', p.ts));
  meta.appendChild(el('span', 'capcode', p.capcode));
  meta.appendChild(el('span', 'badge ' + p.type, p.type));
  if (p._partCount > 1) {
    meta.appendChild(el('span', 'parts', p._partCount + ' parts'));
  }
  meta.appendChild(el('span', 'channel', p.mode + ' · ' + p.frame));
  if (p.proto === 'FLEX_NEXT') {
    meta.appendChild(el('span', 'proto', 'next'));
  }
  article.appendChild(meta);
  article.appendChild(renderBody(p.body));
  return article;
}

function updateCount() {
  const visible = pages.filter(matches).length;
  countEl.textContent = (visible === pages.length)
    ? pages.length + ' pages'
    : visible + ' / ' + pages.length + ' pages';
  refreshChipCounts();
}

function showEmpty(text) {
  list.replaceChildren();
  list.appendChild(el('div', 'empty', text));
}

function rerender() {
  const filtered = pages.filter(matches);
  if (!pages.length) {
    showEmpty('Listening for pages…');
  } else if (!filtered.length) {
    showEmpty('No pages match filter');
  } else {
    list.replaceChildren();
    const frag = document.createDocumentFragment();
    filtered.forEach(p => frag.appendChild(makePage(p, false)));
    list.appendChild(frag);
  }
  updateCount();
}

function addPage(p) {
  p._receivedAt = Date.now();
  const target = findStitchTarget(p);
  if (target) {
    target.body = target.body + p.body;
    target._partCount = (target._partCount || 1) + 1;
    target._receivedAt = p._receivedAt;
    if (matches(target)) {
      const existing = list.querySelector('[data-id="' + target.id + '"]');
      if (existing) {
        existing.replaceWith(makePage(target, true));
      } else {
        const empty = list.querySelector('.empty');
        if (empty) empty.remove();
        list.insertBefore(makePage(target, true), list.firstChild);
      }
    }
    updateCount();
    return;
  }
  p._partCount = 1;
  pages.unshift(p);
  if (pages.length > 500) pages.length = 500;
  if (matches(p)) {
    const empty = list.querySelector('.empty');
    if (empty) empty.remove();
    list.insertBefore(makePage(p, true), list.firstChild);
  }
  updateCount();
}

filterInput.addEventListener('input', () => {
  filter = filterInput.value.trim().toLowerCase();
  syncClearVisibility();
  rerender();
});

filterInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && filterInput.value) {
    e.preventDefault();
    clearFilter();
  }
});

filterClear.addEventListener('click', () => {
  clearFilter();
  filterInput.focus();
});

list.addEventListener('click', (e) => {
  const cap = e.target.closest('.capcode');
  if (!cap) return;
  const code = cap.textContent.trim();
  filterInput.value = code;
  filter = code.toLowerCase();
  filterInput.focus();
  syncClearVisibility();
  rerender();
});

function connect() {
  const src = new EventSource('/stream');
  src.onopen = () => {
    statusEl.classList.remove('offline');
    statusText.textContent = 'Live';
  };
  src.onerror = () => {
    statusEl.classList.add('offline');
    statusText.textContent = 'Reconnecting';
  };
  src.addEventListener('history', e => {
    const arr = JSON.parse(e.data);  // oldest-first
    pages.length = 0;
    const stitched = [];
    const LOOKBACK = 30;
    for (const p of arr) {
      let target = null;
      for (let i = stitched.length - 1; i >= Math.max(0, stitched.length - LOOKBACK); i--) {
        if (shouldStitch(stitched[i], p)) { target = stitched[i]; break; }
      }
      if (target) {
        target.body = target.body + p.body;
        target._partCount = (target._partCount || 1) + 1;
      } else {
        stitched.push(Object.assign({}, p, { _partCount: 1 }));
      }
    }
    stitched.reverse().forEach(p => pages.push(p));
    rerender();
  });
  src.onmessage = e => addPage(JSON.parse(e.data));
}

connect();
"""


def render_html() -> bytes:
    parts = [
        "<!doctype html>\n",
        '<html lang="en">\n<head>\n',
        '<meta charset="utf-8">\n',
        "<title>FLEX Feed</title>\n",
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n',
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">\n',
        '<script src="https://cdn.jsdelivr.net/npm/dompurify@3.2.7/dist/purify.min.js" crossorigin="anonymous"></script>\n',
        "<style>",
        CSS,
        "</style>\n</head>\n<body>\n",
        '<header>\n',
        '  <h1>FLEX Feed</h1>\n',
        '  <div class="live" id="status"><span class="dot"></span><span id="statusText">Connecting</span></div>\n',
        '  <span class="count" id="count">0 pages</span>\n',
        '  <div class="chips" id="chips">\n',
        '    <button class="chip" data-type="ALN" type="button">ALN<span class="count" data-type-count="ALN">0</span></button>\n',
        '    <button class="chip" data-type="NUM" type="button">NUM<span class="count" data-type-count="NUM">0</span></button>\n',
        '    <button class="chip" data-type="TON" type="button">TON<span class="count" data-type-count="TON">0</span></button>\n',
        '    <button class="chip" data-type="TEST" type="button">Test<span class="count" data-type-count="TEST">0</span></button>\n',
        '  </div>\n',
        '  <div class="filter-wrap">\n',
        '    <input type="text" id="filter" placeholder="filter capcode or text…" autocomplete="off">\n',
        '    <button class="filter-clear" id="filterClear" type="button" aria-label="Clear filter" title="Clear filter (Esc)">×</button>\n',
        '  </div>\n',
        '</header>\n',
        '<main id="list"><div class="empty">Listening for pages…</div></main>\n',
        "<script>",
        JS,
        "</script>\n</body>\n</html>\n",
    ]
    return "".join(parts).encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            with _state_lock:
                hist = list(_history)
                _subscribers.append(q)
            try:
                self.wfile.write(
                    b"event: history\ndata: "
                    + json.dumps(hist).encode("utf-8")
                    + b"\n\n"
                )
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _state_lock:
                    if q in _subscribers:
                        _subscribers.remove(q)
            return
        self.send_error(404)


def main():
    load_npa_nxx()
    prime_history()
    threading.Thread(target=tail_log, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"FLEX viewer: {url}")
    print(f"Tailing:     {LOG_PATH}")
    print("Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
