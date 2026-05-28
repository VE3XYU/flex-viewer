#!/usr/bin/env python3
"""FLEX live page viewer — tails live.log, serves a local web UI."""
import http.server
import json
import queue
import re
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

LOG_PATH = Path.home() / "Documents" / "flex waves" / "live.log"
PORT = 8732
HISTORY_SIZE = 200
MAX_BODY = 4096

_state_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_history: deque = deque(maxlen=HISTORY_SIZE)
_next_id = 0
_id_lock = threading.Lock()


def alloc_id() -> int:
    global _next_id
    with _id_lock:
        _next_id += 1
        return _next_id


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
input[type=text] {
  margin-left: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  padding: 6px 10px;
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
const countEl = document.getElementById('count');
const statusEl = document.getElementById('status');
const statusText = document.getElementById('statusText');

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

function shouldStitch(prev, cur) {
  if (!prev || !cur) return false;
  if (prev.capcode !== cur.capcode) return false;
  if (prev.type !== cur.type) return false;
  if (isTest(prev.body) || isTest(cur.body)) return false;
  // Live arrivals: real-time window
  if (cur._receivedAt && prev._receivedAt) {
    return (cur._receivedAt - prev._receivedAt) <= STITCH_WINDOW_MS;
  }
  // Historical replay: same decode second is a strong signal
  return prev.ts === cur.ts;
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
  // FLEX email-style pages often pack fields onto one line using 2+ spaces
  // as separators. Insert a newline before any "Label:" preceded by 2+
  // spaces. Excludes URLs (Label://) and lowercase tokens to avoid breaking
  // times like "9:05" or words like "is:".
  const sep = /[ \t]{2,}(?=[A-Z][\w &#/.-]{0,30}?:(?!\/\/))/g;
  return text.replace(sep, '\n');
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
  const prev = pages[0];
  if (shouldStitch(prev, p)) {
    prev.body = prev.body + p.body;
    prev._partCount = (prev._partCount || 1) + 1;
    prev._receivedAt = p._receivedAt;
    if (matches(prev)) {
      const existing = list.querySelector('[data-id="' + prev.id + '"]');
      if (existing) {
        const newNode = makePage(prev, true);
        existing.replaceWith(newNode);
      } else {
        const empty = list.querySelector('.empty');
        if (empty) empty.remove();
        list.insertBefore(makePage(prev, true), list.firstChild);
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
    const node = makePage(p, true);
    list.insertBefore(node, list.firstChild);
  }
  updateCount();
}

filterInput.addEventListener('input', () => {
  filter = filterInput.value.trim().toLowerCase();
  rerender();
});

list.addEventListener('click', (e) => {
  const cap = e.target.closest('.capcode');
  if (!cap) return;
  const code = cap.textContent.trim();
  filterInput.value = code;
  filter = code.toLowerCase();
  filterInput.focus();
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
    for (const p of arr) {
      const last = stitched[stitched.length - 1];
      if (shouldStitch(last, p)) {
        last.body = last.body + p.body;
        last._partCount = (last._partCount || 1) + 1;
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
        '  <input type="text" id="filter" placeholder="filter capcode or text…" autocomplete="off">\n',
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
