# Capcode Labels + Callback Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator tag exact capcodes with institution names (persisted server-side) and auto-annotate phone numbers in page bodies with their town, so they can tell which institution a page is for on a single-carrier simulcast channel.

**Architecture:** Labels are dynamic, so they live client-side as a `capcode → name` map fetched once and updated over a new `GET`/`POST /labels` REST pair (`labels.json` on disk, atomic writes). Callback hints are static + large, so number→town lookup happens server-side in Python against a bundled Ontario NPA-NXX dataset and rides along on each record as a `hints` field. An offline generator (`build_npa_nxx.py`) builds the dataset from CNAC.

**Tech Stack:** Python 3.9+ stdlib only (`http.server`, `json`, `re`, `csv`, `urllib`, `threading`, `os`); inline vanilla JS + CSS in `viewer.py`; DOMPurify (already loaded) untouched.

---

## Reference facts (verified during planning — do not re-derive)

- **Capcode form** (`viewer.py:68`): `d["capcode"].lstrip("0") or "0"`. Every labels key, lookup, and client comparison uses this exact form.
- **CNAC CSV**: direct download `https://cnac.ca/data/COCodeStatus_NPA{NPA}.csv`. Columns in order: `NPA(0), CO Code/NXX(1), Status(2), Pooled(3), Exchange Area(4)=town, Province(5), Company(6), OCN(7), Remarks(8)`. Keep rows where `Status == "In Service"`. A non-existent NPA returns **HTTP 200 with an HTML error page** — validate by content-type `text/csv` or body starting with `"NPA"`.
- **Phone regex** (tested against the cases in Task 1/2): `r"(?<![\d.\-])(?:\+?1[ .\-]?)?\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?![\d.\-])"`. Group 1 = NPA, group 2 = NXX. This supersedes the slightly weaker pattern in the spec.
- **Ontario NPAs**: `416 647 437 905 289 365 742 519 226 548 382 613 343 753 705 249 683 807 942`.
- **Locks**: add a **new** `_labels_lock` for `_labels` (never reuse `_state_lock`). `_npa_nxx` needs no lock — loaded once in `main()` before any thread starts, then read-only.
- **HTTP/1.1 gotcha** (`viewer.py:791`): every non-streaming response MUST send a correct `Content-Length` or the browser hangs. Mirror the `/` branch (`viewer.py:799-804`). For 4xx, use `self.send_error(code)` (sets its own length).
- **DOMPurify invariant**: `renderBody()` is the only code that sets `innerHTML`. All new label/hint DOM is built with the `el()` helper (`viewer.py:541-546`, textContent only). Never add tags to `SANITIZE_CONFIG` for these.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `build_npa_nxx.py` | Offline: fetch CNAC CSVs, build `data/npa-nxx-on.json`; `--check` self-tests | Create |
| `data/npa-nxx-on.json` | Bundled NPA-NXX → "Town, PROV" data, grouped by town | Generated |
| `viewer.py` | Runtime: labels store + endpoints, NPA-NXX load + `phone_hints`, client label/hint UI | Modify |
| `CLAUDE.md` | Note the GET/POST `/labels` endpoints; labels + hints behavior | Modify |
| `README.md` | Labels/hints features, dataset regen command, config row | Modify |

Data file format (grouped by place to intern town strings):
```json
{ "Markham, ON": ["905201", "905319"], "Newmarket, ON": ["905830", "905895"] }
```
`viewer.py` inverts this to a flat `{"905201": "Markham, ON"}` lookup at load.

---

## Task 1: Offline dataset generator + bundled JSON

**Files:**
- Create: `build_npa_nxx.py`
- Generate: `data/npa-nxx-on.json`

- [ ] **Step 1: Write `build_npa_nxx.py`**

```python
#!/usr/bin/env python3
"""Generate data/npa-nxx-on.json: Ontario NPA-NXX -> rate centre (town).

Pulls per-area-code "CO Code Status" CSVs from the Canadian Numbering
Administrator (cnac.ca), keeps in-service codes, and writes a compact
JSON grouped by "Town, PROV". Re-run to refresh (numbering plans change).

    python3 build_npa_nxx.py          # fetch + write data/npa-nxx-on.json
    python3 build_npa_nxx.py --check  # run offline self-tests, no network
"""
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ONTARIO_NPAS = [
    "416", "647", "437", "905", "289", "365", "742",
    "519", "226", "548", "382", "613", "343", "753",
    "705", "249", "683", "807", "942",
]
# Ontario area codes incl. overlays; 382 and 942 intentionally extend the
# spec's list (both are Ontario codes; sparse overlays just get skipped).
CSV_URL = "https://cnac.ca/data/COCodeStatus_NPA{npa}.csv"
OUT_PATH = Path(__file__).resolve().parent / "data" / "npa-nxx-on.json"

# CNAC CSV columns (verified): NPA, CO Code (NXX), Status, Pooled,
# Exchange Area, Province, Company, OCN, Remarks
COL_NPA, COL_NXX, COL_STATUS, COL_TOWN, COL_PROV = 0, 1, 2, 4, 5
IN_SERVICE = "In Service"


def fetch_csv(npa):
    url = CSV_URL.format(npa=npa)
    req = urllib.request.Request(url, headers={"User-Agent": "flex-viewer-build"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8-sig", errors="replace")  # -sig drops any BOM
    # Non-existent NPAs return HTTP 200 with an HTML error page, not a 404.
    if "text/csv" not in ctype and not raw.lstrip().startswith('"NPA"'):
        raise ValueError("no CSV for NPA %s (content-type %s)" % (npa, ctype))
    return raw


def parse_csv(text):
    """Yield (npanxx, "Town, PROV") for in-service rows that have a town."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    for row in rows[1:]:  # skip header
        if len(row) <= COL_PROV:
            continue
        if row[COL_STATUS].strip() != IN_SERVICE:
            continue
        npa = row[COL_NPA].strip()
        nxx = row[COL_NXX].strip()
        town = row[COL_TOWN].strip()
        prov = row[COL_PROV].strip()
        if not town or not prov or len(npa) != 3 or len(nxx) != 3:
            continue
        yield npa + nxx, "%s, %s" % (town, prov)


def build():
    grouped = {}
    total = 0
    for npa in ONTARIO_NPAS:
        try:
            text = fetch_csv(npa)
        except Exception as e:
            print("skip NPA %s: %s" % (npa, e), file=sys.stderr)
            continue
        n = 0
        for npanxx, place in parse_csv(text):
            grouped.setdefault(place, []).append(npanxx)
            n += 1
            total += 1
        print("NPA %s: %d in-service codes" % (npa, n))
    for place in grouped:
        grouped[place].sort()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(grouped, f, ensure_ascii=False, sort_keys=True)
    print("wrote %s: %d places, %d codes" % (OUT_PATH, len(grouped), total))


# ---- self-tests (offline, no network) ----
SAMPLE_CSV = (
    '"NPA","CO Code (NXX)","Status","Pooled","Exchange Area","Province","Company","OCN","Remarks"\n'
    '905,200,"In Service","N","Castlemore","ON","Rogers","8377",\n'
    '905,201,"In Service","N","Markham","ON","Bell Canada","8051",\n'
    '905,999,"Not Available","N","","","","",\n'
)


def check():
    parsed = dict(parse_csv(SAMPLE_CSV))
    assert parsed == {"905200": "Castlemore, ON", "905201": "Markham, ON"}, parsed
    print("parse_csv ok")
    print("all checks passed")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        build()
```

- [ ] **Step 2: Run the parse self-test**

Run: `python3 build_npa_nxx.py --check`
Expected:
```
parse_csv ok
all checks passed
```

- [ ] **Step 3: Generate the real dataset**

Run: `python3 build_npa_nxx.py`
Expected: one `NPA <code>: <n> in-service codes` line per area code (905 alone is ~772; some sparse overlays may print `skip NPA …` to stderr — that is fine), then a final `wrote …/data/npa-nxx-on.json: <P> places, <C> codes` with C in the thousands to low tens of thousands.

- [ ] **Step 4: Sanity-check the output**

Run: `python3 -c "import json; d=json.load(open('data/npa-nxx-on.json')); print(len(d),'places'); print([p for p in d if 'Newmarket' in p][:3])"`
Expected: a place count > 100 and a list containing `Newmarket, ON` (Newmarket is in NPA 905).

- [ ] **Step 5: Commit**

```bash
git add build_npa_nxx.py data/npa-nxx-on.json
git commit -m "Add offline NPA-NXX dataset generator and bundled Ontario data"
```

---

## Task 2: Server-side callback hints (`viewer.py`)

**Files:**
- Modify: `viewer.py` (imports `:3-11`; constants `:13-16`; regex cluster `:32-43`; helpers near `:25-29`; `parse_record` return `:61-71`; `main` `:842-845`)
- Modify: `build_npa_nxx.py` (extend `check()`)

- [ ] **Step 1: Add `import os` to the imports block**

`viewer.py:3-11` currently ends with `from pathlib import Path`. Add `import os` (alphabetical, before `import queue`). Note: `os` is first *used* in Task 3 (`save_labels` → `os.replace`); it is added here so the imports land together. Harmless ahead of its consumer (stdlib, no linter):

```python
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
```

- [ ] **Step 2: Add the dataset path constant**

After `MAX_BODY = 4096` (`viewer.py:16`), add:

```python
NPA_NXX_PATH = Path(__file__).resolve().parent / "data" / "npa-nxx-on.json"
```

- [ ] **Step 3: Add the read-only NPA-NXX table global**

After `_id_lock = threading.Lock()` (`viewer.py:22`), add:

```python
_npa_nxx: dict[str, str] = {}  # "905201" -> "Markham, ON"; loaded once in main()
```

- [ ] **Step 4: Add `PHONE_RE` to the regex cluster**

After `HEADER_RE = ...` (`viewer.py:43`), add:

```python
# NANP 10-digit number in free text. Group 1 = NPA, group 2 = NXX.
# Lookarounds reject digit runs, FLEX frames (12.045) and hyphen ranges.
PHONE_RE = re.compile(
    r"(?<![\d.\-])(?:\+?1[ .\-]?)?\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?![\d.\-])"
)
```

- [ ] **Step 5: Add `load_npa_nxx()` and `phone_hints()` helpers**

After `alloc_id()` (`viewer.py:29`), add:

```python
def load_npa_nxx() -> None:
    """Load+invert the bundled NPA-NXX table. Missing file disables hints."""
    global _npa_nxx
    try:
        with NPA_NXX_PATH.open("r", encoding="utf-8") as f:
            grouped = json.load(f)
    except Exception:
        _npa_nxx = {}
        return
    table = {}
    for place, codes in grouped.items():
        for code in codes:
            table[code] = place
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
```

- [ ] **Step 6: Attach hints in `parse_record`**

In the returned dict (`viewer.py:61-71`), add a `"hints"` key after `"body": body,`:

```python
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
```

- [ ] **Step 7: Load the table in `main()` before priming**

`main()` (`viewer.py:842-845`) starts with `prime_history()`. Add `load_npa_nxx()` as the first line so hints are populated when history is parsed:

```python
def main():
    load_npa_nxx()
    prime_history()
    threading.Thread(target=tail_log, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
```

- [ ] **Step 8: Extend `build_npa_nxx.py --check` to test phone detection**

Replace the `check()` function in `build_npa_nxx.py` with the version below (adds phone assertions that import the production regex from `viewer`, so there is one source of truth). Also add the two lists above it:

```python
SHOULD_MATCH = [
    "905-555-0142", "(905) 555-0142", "905.555.0142", "9055550142",
    "+1 905 555 0142", "1-905-555-0142", "call 416-555-0173 now",
    "Ph (647) 555-0190 ext",
]
SHOULD_NOT_MATCH = [
    "1234567", "0123456789012345", "15:42:07", "12.045", "x5512", "2026-05-28",
]


def check():
    parsed = dict(parse_csv(SAMPLE_CSV))
    assert parsed == {"905200": "Castlemore, ON", "905201": "Markham, ON"}, parsed
    print("parse_csv ok")

    sys.path.insert(0, str(Path(__file__).resolve().parent))  # so import works from any cwd
    import viewer
    viewer._npa_nxx = {
        "905555": "Newmarket, ON", "416555": "Toronto, ON", "647555": "Toronto, ON",
    }
    for s in SHOULD_MATCH:
        assert viewer.phone_hints(s), "expected a hint for %r" % s
    for s in SHOULD_NOT_MATCH:
        hits = viewer.phone_hints(s)
        assert not hits, "unexpected hint for %r: %r" % (s, hits)
    print("phone_hints ok")
    print("all checks passed")
```

- [ ] **Step 9: Run the extended self-test**

Run: `python3 build_npa_nxx.py --check`
Expected:
```
parse_csv ok
phone_hints ok
all checks passed
```

- [ ] **Step 10: Verify hints attach to a record (deterministic, offline)**

Run from the repo root (so `import viewer` resolves). This overrides the table with a synthetic entry so the check is deterministic — the fictional `555-01xx` range is intentionally NOT in the real CNAC data:
```bash
python3 -c "
import viewer
viewer._npa_nxx = {'905555': 'Newmarket, ON'}
rec = viewer.parse_record('2026-05-28 12:00:00: FLEX|1600/4/K|1600/4|12.045|0001234567|ALN|please call 905 555 0142')
print('hints:', rec['hints'])
"
```
Expected exactly: `hints: [{'num': '905 555 0142', 'place': 'Newmarket, ON'}]`. (Real-dataset resolution is exercised in Task 5 Step 5, which derives a real in-service prefix.)

- [ ] **Step 11: Commit**

```bash
git add viewer.py build_npa_nxx.py
git commit -m "Add server-side callback-number town hints"
```

---

## Task 3: Server-side labels store + endpoints (`viewer.py`)

**Files:**
- Modify: `viewer.py` (constants `:13-16`; globals `:18-22`; helpers near `:25-29`; `Handler` `:790-839`; `main` `:842-845`)

- [ ] **Step 1: Add labels constants**

After the `NPA_NXX_PATH` constant added in Task 2, add:

```python
LABELS_PATH = Path(__file__).resolve().parent / "labels.json"
MAX_LABEL_LEN = 64
MAX_CAPCODE_LEN = 10
MAX_LABELS = 5000
MAX_POST_BODY = 4096
```

- [ ] **Step 2: Add the labels global + lock**

After the `_npa_nxx` global added in Task 2, add:

```python
_labels_lock = threading.Lock()
_labels: dict[str, str] = {}  # "1234567" -> "Southlake"
```

- [ ] **Step 3: Add `load_labels()` and `save_labels()` helpers**

After `phone_hints()` (added in Task 2), add:

```python
def load_labels() -> None:
    global _labels
    try:
        with LABELS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    with _labels_lock:
        _labels = {(str(k).lstrip("0") or "0"): str(v) for k, v in data.items()}


def save_labels(snapshot: dict) -> None:
    """Atomic write: temp file in the same dir, then os.replace()."""
    tmp = LABELS_PATH.with_name(LABELS_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)
    os.replace(tmp, LABELS_PATH)
```

- [ ] **Step 4: Add the `GET /labels` branch**

In `do_GET` (`viewer.py:796-839`), add this branch immediately before the final `self.send_error(404)`:

```python
        if self.path == "/labels":
            with _labels_lock:
                data = json.dumps(dict(_labels)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
```

- [ ] **Step 5: Add the `do_POST` method**

Add this method to the `Handler` class, right after `do_GET` returns (before the class ends, after `viewer.py:839`):

```python
    def do_POST(self):
        if self.path != "/labels":
            self.send_error(404)
            return
        # Local-only: reject anything not addressed to this loopback host.
        allowed = (
            "127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "127.0.0.1", "localhost",
        )
        if self.headers.get("Host", "") not in allowed:
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin is not None and not any(origin == "http://" + h for h in allowed):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0:
            self.send_error(400)
            return
        if length > MAX_POST_BODY:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            capcode = str(payload["capcode"])
            label = str(payload.get("label", ""))
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        capcode = capcode.lstrip("0") or "0"
        if not capcode.isdigit() or len(capcode) > MAX_CAPCODE_LEN:
            self.send_error(400)
            return
        label = label.replace("<", "").replace(">", "").strip()
        if len(label) > MAX_LABEL_LEN:
            self.send_error(400)
            return
        with _labels_lock:
            over_cap = (
                bool(label) and capcode not in _labels and len(_labels) >= MAX_LABELS
            )
            if not over_cap:
                if label:
                    _labels[capcode] = label
                else:
                    _labels.pop(capcode, None)
            snapshot = dict(_labels)
        if over_cap:
            self.send_error(400)
            return
        save_labels(snapshot)
        data = json.dumps(snapshot).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
```

- [ ] **Step 6: Load labels in `main()`**

Update `main()` so the first lines are (load order: NPA-NXX then labels then prime):

```python
def main():
    load_npa_nxx()
    load_labels()
    prime_history()
    threading.Thread(target=tail_log, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
```

- [ ] **Step 7: Start the server and test the endpoints**

Run (in one shell): `python3 viewer.py`
Then in another shell run each and check output:

```bash
curl -s http://127.0.0.1:8732/labels
```
Expected: `{}` (empty map; or existing labels if `labels.json` already present).

```bash
curl -s -X POST http://127.0.0.1:8732/labels \
  -H 'Content-Type: application/json' \
  -d '{"capcode":"0001234567","label":"Southlake"}'
```
Expected: `{"1234567": "Southlake"}` (note leading zeros stripped).

```bash
curl -s http://127.0.0.1:8732/labels && echo && cat labels.json
```
Expected: both print `{"1234567": "Southlake"}`.

```bash
curl -s -X POST http://127.0.0.1:8732/labels \
  -H 'Content-Type: application/json' -d '{"capcode":"1234567","label":"  "}'
```
Expected: `{}` (empty label deletes the entry).

- [ ] **Step 8: Test validation + hardening**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8732/labels \
  -H 'Content-Type: application/json' -d '{"capcode":"abc","label":"x"}'
```
Expected: `400` (non-digit capcode).

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8732/labels \
  -H 'Host: evil.example' -H 'Content-Type: application/json' \
  -d '{"capcode":"1234567","label":"x"}'
```
Expected: `403` (non-local Host).

Stop the server (Ctrl-C).

- [ ] **Step 9: Ignore `labels.json`, then commit**

`labels.json` holds the operator's institution map — sensitive per the spec's Privacy section, must never be committed. Add it to `.gitignore` (idempotent), then commit:

```bash
grep -qxF 'labels.json' .gitignore || echo 'labels.json' >> .gitignore
git add viewer.py .gitignore
git commit -m "Add server-side capcode label store and /labels GET/POST endpoints"
```

---

## Task 4: Client label display, edit, and filter (`viewer.py` JS + CSS)

**Files:**
- Modify: `viewer.py` (JS state `:420-432`; `matches` `:499-503`; `makePage` `:607-627`; list click `:706-715`; startup `:750`; CSS after `.capcode` `:329-339`)

- [ ] **Step 1: Add the client labels state**

In the JS state block, after `let filter = '';` (`viewer.py:421`), add:

```javascript
let labels = {};  // capcode -> name, fetched from /labels
```

- [ ] **Step 2: Add `loadLabels()` and `saveLabel()`**

Add these functions in the JS, just before `function connect()` (`viewer.py:717`):

```javascript
function loadLabels() {
  fetch('/labels')
    .then(r => (r.ok ? r.json() : {}))
    .then(data => { labels = data || {}; rerender(); })
    .catch(() => {});
}

function saveLabel(capcode, label) {
  fetch('/labels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ capcode: capcode, label: label }),
  })
    .then(r => (r.ok ? r.json() : null))
    .then(data => { if (data) labels = data; rerender(); })
    .catch(() => rerender());
}
```

- [ ] **Step 3: Add the inline edit interaction**

Add this function next to `saveLabel` (before `connect()`):

```javascript
function startLabelEdit(spanEl, capcode) {
  const input = el('input', 'label-edit');
  input.type = 'text';
  input.value = labels[capcode] || '';
  input.placeholder = 'label…';
  spanEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) saveLabel(capcode, input.value.trim());
    else rerender();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(false));
}
```

- [ ] **Step 4: Render the label / tag affordance in `makePage`**

In `makePage` (`viewer.py:607-627`), immediately after `meta.appendChild(el('span', 'capcode', p.capcode));` (`viewer.py:615`), insert:

```javascript
  const name = labels[p.capcode];
  const tag = name ? el('span', 'label', name) : el('span', 'tagbtn', '⊕ tag');
  tag.dataset.capcode = p.capcode;
  meta.appendChild(tag);
```

(This sits before the `badge` and before `channel`'s `margin-left:auto`, so it stays on the left next to the capcode.)

- [ ] **Step 5: Make the filter label-aware**

Replace `matches` (`viewer.py:499-503`) with:

```javascript
function matches(p) {
  if (!enabledTypes.has(bucket(p))) return false;
  if (!filter) return true;
  const lbl = (labels[p.capcode] || '').toLowerCase();
  return p.capcode.includes(filter) || p.body.toLowerCase().includes(filter) || lbl.includes(filter);
}
```

- [ ] **Step 6: Wire label/tag clicks (without breaking capcode filter)**

Replace the list click handler (`viewer.py:706-715`) with:

```javascript
list.addEventListener('click', (e) => {
  const tag = e.target.closest('.label, .tagbtn');
  if (tag) {
    startLabelEdit(tag, tag.dataset.capcode);
    return;
  }
  const cap = e.target.closest('.capcode');
  if (!cap) return;
  const code = cap.textContent.trim();
  filterInput.value = code;
  filter = code.toLowerCase();
  filterInput.focus();
  syncClearVisibility();
  rerender();
});
```

- [ ] **Step 7: Call `loadLabels()` on startup**

Replace the final `connect();` (`viewer.py:750`) with:

```javascript
loadLabels();
connect();
```

- [ ] **Step 8: Add CSS for `.label`, `.tagbtn`, `.label-edit`**

After the `.capcode:hover` rule (`viewer.py:339`), add:

```css
.label {
  color: var(--accent);
  font-size: 11px;
  font-weight: 500;
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
  transition: background 100ms, border-color 120ms;
}
.label:hover { background: rgba(255, 155, 64, 0.14); border-color: var(--accent); }
.tagbtn {
  color: var(--dim);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  cursor: pointer;
  opacity: 0;
  transition: opacity 140ms, color 120ms;
}
.page:hover .tagbtn { opacity: 0.55; }
.tagbtn:hover { color: var(--text); opacity: 1; }
.label-edit {
  background: var(--surface);
  border: 1px solid var(--accent);
  border-radius: 3px;
  color: var(--text);
  font-family: inherit;
  font-size: 11px;
  padding: 1px 6px;
  width: 150px;
}
.label-edit:focus { outline: none; }
```

- [ ] **Step 9: Manual browser verification**

Run `python3 viewer.py` (with `live.log` present, or replay a saved log). In the browser at `http://127.0.0.1:8732/`:
1. Hover a row → a faint `⊕ tag` appears next to the capcode.
2. Click it → an input appears; type `Southlake`, press Enter → the row shows a `Southlake` label pill.
3. Reload the page → the label is still there (persisted).
4. Type `southlake` in the filter box → only that capcode's pages show.
5. Click the capcode **number** → filter fills with the capcode (existing behavior intact).
6. Click the label, clear it, press Enter → label removed; reload → still removed.

Stop the server.

- [ ] **Step 10: Commit**

```bash
git add viewer.py
git commit -m "Add client-side capcode label display, inline editing, and filtering"
```

---

## Task 5: Client hint rendering + stitch preservation (`viewer.py` JS + CSS)

**Files:**
- Modify: `viewer.py` (`makePage` `:607-627`; `addPage` `:657-686`; history handler `:727-746`; CSS after `.body` `:370-382`)

- [ ] **Step 1: Render the hints line in `makePage`**

In `makePage`, replace the final two lines (`viewer.py:625-626`):

```javascript
  article.appendChild(meta);
  article.appendChild(renderBody(p.body));
  return article;
```

with:

```javascript
  article.appendChild(meta);
  article.appendChild(renderBody(p.body));
  if (p.hints && p.hints.length) {
    const hintsEl = el('div', 'hints');
    p.hints.forEach(h => hintsEl.appendChild(el('div', 'hint', '↳ ' + h.num + ' — ' + h.place)));
    article.appendChild(hintsEl);
  }
  return article;
```

- [ ] **Step 2: Preserve hints when stitching (live path)**

In `addPage` (`viewer.py:657-686`), inside the `if (target) {` block, after `target._receivedAt = p._receivedAt;` (`viewer.py:663`), add:

```javascript
    if (p.hints && p.hints.length) {
      target.hints = (target.hints || []).concat(p.hints);
    }
```

Note: hints are concatenated per fragment. A callback number split *across* the 248-char fragment boundary is not recovered — accepted as best-effort per the spec. The client must **not** re-run the phone regex (the NPA-NXX table is server-only), so do not try to recompute hints on the merged body here.

- [ ] **Step 3: Preserve hints when stitching (history path)**

In the SSE `history` handler (`viewer.py:727-746`), inside the `if (target) {` branch, after `target._partCount = (target._partCount || 1) + 1;` (`viewer.py:739`), add:

```javascript
        if (p.hints && p.hints.length) {
          target.hints = (target.hints || []).concat(p.hints);
        }
```

(The `else` branch's `Object.assign({}, p, …)` already carries `p.hints` onto a new stitched entry. Both stitch paths must stay in sync per CLAUDE.md.)

- [ ] **Step 4: Add CSS for `.hints` / `.hint`**

After the `.body b, .body strong` rule (`viewer.py:382`), add:

```css
.hints {
  padding-left: 14px;
  margin-left: 2px;
  margin-top: 6px;
}
.hint {
  color: var(--muted);
  font-family: 'IBM Plex Mono', ui-monospace, Menlo, monospace;
  font-size: 11px;
  line-height: 1.5;
}
```

- [ ] **Step 5: Manual browser verification (real dataset)**

Derive a **real in-service prefix** from the bundled dataset so the hint is guaranteed to resolve (the `555-01xx` range used elsewhere is fictional and won't). The replay line uses the real public exchange prefix plus a fictional `0142` line number — no real callback number:
```bash
python3 - <<'PY'
import json
d = json.load(open('data/npa-nxx-on.json'))
code = sorted(c for codes in d.values() for c in codes)[0]
print('use NPA/NXX:', code[:3], code[3:], '(+ line 0142)')
PY
```
Append a matching line to a replay log (substitute the printed NPA/NXX):
```
2026-05-28 12:00:00: FLEX|1600/4/K|1600/4|12.045|0009999999|ALN|please call <NPA> <NXX> 0142 for results
```
Run `python3 viewer.py` against that log. In the browser:
1. The page shows a `↳ <NPA> <NXX> 0142 — <Town>, ON` line under the body.
2. Rename the dataset away (`mv data/npa-nxx-on.json data/_off.json`), restart the viewer → no hint line appears and the viewer still loads pages normally. Restore it (`mv data/_off.json data/npa-nxx-on.json`).

Stop the server.

- [ ] **Step 6: Commit**

```bash
git add viewer.py
git commit -m "Render callback town hints and preserve them across fragment stitching"
```

---

## Task 6: Documentation (`CLAUDE.md`, `README.md`)

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Update the CLAUDE.md endpoint note**

In `CLAUDE.md`, find the `render_html()` bullet that says:

```
  **`render_html()`** returns the entire UI (CSS + JS) inline. There is no
  static file serving — `/` returns HTML and `/stream` returns SSE; everything
  else 404s. DOMPurify is loaded from jsDelivr.
```

Replace the middle sentence so it reads:

```
  **`render_html()`** returns the entire UI (CSS + JS) inline. There is no
  static file serving — `/` returns HTML, `/stream` returns SSE, and
  `/labels` serves (GET) and updates (POST) the capcode-label store; everything
  else 404s. DOMPurify is loaded from jsDelivr.
```

- [ ] **Step 2: Add a CLAUDE.md section on labels + hints**

In `CLAUDE.md`, after the "Frontend (inline JS in `viewer.py`)" section's list (after item 3 about `structureBody`), add:

```markdown
### Capcode labels and callback hints

- **Labels** are a `capcode → name` map. They live server-side in
  `labels.json` (atomic writes), are served by `GET /labels`, and updated by
  `POST /labels` ({"capcode","label"}; empty label deletes). The client holds a
  `labels` object, fetched once via `loadLabels()`, and re-renders all pages on
  change. Keys use the same leading-zero-stripped capcode form as the feed. The
  POST handler is localhost-only (Host/Origin guard), validates capcode (digits,
  ≤10) and label (≤64, `<`/`>` stripped), and caps total labels.
- **Callback hints** are computed server-side: `phone_hints()` runs `PHONE_RE`
  over the body and maps NPA-NXX → town via `_npa_nxx`, attached as `rec["hints"]`
  in `parse_record` (covers history + live). The table is loaded once by
  `load_npa_nxx()` in `main()` (read-only, no lock); a missing
  `data/npa-nxx-on.json` silently disables hints. Regenerate the dataset with
  `python3 build_npa_nxx.py` (pulls CNAC per-NPA CSVs); `--check` runs offline
  self-tests. Labels and hints render via the `el()` helper (textContent only),
  never through DOMPurify.
```

- [ ] **Step 3: Add README feature bullets**

In `README.md`, in the **Features** list, after the "Click-to-filter" bullet (`README.md:119-120`), add:

```markdown
- **Capcode labels** — click the `⊕ tag` next to any capcode to name it
  (e.g. an institution). Labels persist server-side in `labels.json`, show
  inline, and are searchable from the filter box, so typing a name surfaces
  every capcode you've given it.
- **Callback hints** — phone numbers in a page body are annotated with their
  town, looked up from a bundled Ontario NPA-NXX dataset. Useful for guessing
  which institution a page is for on a shared simulcast channel.
```

- [ ] **Step 4: Add README config row + regen note**

In `README.md`, in the **Configuration** table (`README.md:138-143`), add a row:

```markdown
| `LABELS_PATH`  | `labels.json` next to `viewer.py`        |
```

Then, after the `STITCH_WINDOW_MS` paragraph (`README.md:145-146`), add:

```markdown
The callback-hint dataset lives in `data/npa-nxx-on.json` (Ontario only).
Regenerate it from the Canadian Numbering Administrator with:

​```bash
python3 build_npa_nxx.py          # refresh data/npa-nxx-on.json
python3 build_npa_nxx.py --check  # offline self-tests
​```
```

(Remove the zero-width spaces around the inner code fence when adding — they only escape it inside this plan.)

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document capcode labels and callback hints"
```

---

## Final verification

- [ ] **Step 1: Full self-test passes**

Run: `python3 build_npa_nxx.py --check`
Expected: `parse_csv ok` / `phone_hints ok` / `all checks passed`.

- [ ] **Step 2: Clean end-to-end run**

Run `python3 viewer.py`, open the UI, and confirm against the spec's manual checklist (spec §"Testing / verification"): add/persist/delete a label; institution-name filter; capcode click still filters; a known number shows its town; renamed dataset still boots.

- [ ] **Step 3: Confirm branch state**

Run: `git log --oneline main..HEAD`
Expected: six feature commits (dataset generator, hints server, labels server, labels client, hints client, docs) on top of the spec commit.
