# Group-Tagging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect group broadcasts (same body to many capcodes in a short wall-clock window) and let the operator bulk-tag every co-recipient with one edit.

**Architecture:** Detection is a pure client-side scan over the existing `pages` buffer; the server gains a bulk form on `POST /labels` (one atomic write). The label editor becomes group-aware: a default-checked "also tag N others" checkbox applies the label to the whole broadcast (overwrite).

**Tech Stack:** Python 3 stdlib (`http.server`, `json`, `threading`); inline vanilla JS + CSS in `viewer.py`. No test framework (a node syntax/logic check + curl + a browser e2e are the verification).

**Branch:** `group-tagging` (stacked on `capcode-labels` / PR #3).

---

## Reference facts (verified during planning — do not re-derive)

- **Record/`pages` shape:** `ts` is **`"HH:MM:SS"`** (time-only); `capcode` is leading-zero-stripped (`"0001234567"→"1234567"`, all-zeros→`"0"`); `body` may contain `\n`; client `pages[]` entries also carry `_receivedAt`/`_partCount`. Group detection keys on `.ts` + `.body` + `.capcode`.
- **Detection rule:** identical normalized body + wall-clock proximity, NOT frame. (`frameDistance` is for stitching only — do not reuse.)
- **Verified detection functions** (node, 18/18 cases): `groupKey`, `tsDistance`, `findGroup` — source in Task 2, used verbatim.
- **`do_POST` is a linear pipeline** converging on one `snapshot` dict written once under `_labels_lock`; the bulk branch reuses the guards, the lock section tail, and the 200-response unchanged.
- **Two easily-confused constants:** `MAX_BODY` (viewer.py:17, page-body cap) stays `4096`; **`MAX_POST_BODY`** (viewer.py:23, request size guard) becomes `65536`. Add `MAX_BULK = 500`.
- **DOMPurify invariant:** `renderBody` is the only `innerHTML`. All new DOM via `el()` (textContent) or `document.createElement` + property assignment (for the `<input>`). Never `innerHTML`.
- **`el()` can't set input type** — native inputs use `document.createElement('input'); .type=...` (as the existing text input does).

> **Operational note — ISOLATE all server tests from the operator's live instance.** A viewer is normally running on port 8732 with the operator's real `labels.json` (their actual tag map). Server tests MUST NOT read, write, or collide with either. Do NOT try to back up/restore the real file — isolate instead:
> - **Syntax checks** need no server: `python3 -c "import viewer; open('/tmp/page.html','wb').write(viewer.render_html())"`.
> - **Endpoint tests** run a throwaway COPY of the viewer pointed at a temp labels file on a *different* port (8799), so they can never touch the real `labels.json` or the live instance:
>   ```bash
>   pkill -f 'viewer_test.py' 2>/dev/null; rm -f /tmp/labels_test.json
>   cp viewer.py /tmp/viewer_test.py
>   python3 - <<'PY'
>   s=open('/tmp/viewer_test.py').read()
>   s=s.replace('LABELS_PATH = Path(__file__).resolve().parent / "labels.json"','LABELS_PATH = Path("/tmp/labels_test.json")')
>   s=s.replace('PORT = 8732','PORT = 8799')
>   open('/tmp/viewer_test.py','w').write(s)
>   PY
>   python3 /tmp/viewer_test.py >/tmp/vt.log 2>&1 &   # 127.0.0.1:8799, writes /tmp/labels_test.json only
>   sleep 1
>   # … curl against :8799 with Origin http://127.0.0.1:8799 …
>   pkill -f 'viewer_test.py'; rm -f /tmp/viewer_test.py /tmp/labels_test.json /tmp/vt.log
>   ```
> Note the `pkill` pattern is `viewer_test.py` (unique to the copy) — never `pkill -f 'python3 viewer.py'`, which on macOS may not match the framework-Python live instance anyway. The real `labels.json` and the live viewer on 8732 are never touched by any test.

## File structure

| File | Change |
|---|---|
| `viewer.py` | `do_POST` bulk branch; constants (`MAX_POST_BODY`→64K, add `MAX_BULK`); JS `groupKey`/`tsDistance`/`findGroup`/`saveLabels` + `GROUP_*` consts; group-aware `startLabelEdit`; `makePage` id stamp + click handler passthrough; CSS for the checkbox row |
| `CLAUDE.md`, `README.md` | document the bulk `/labels` form + group-tagging |

---

## Task 1: Server-side bulk `POST /labels` (`viewer.py`)

**Files:**
- Modify: `viewer.py` constants (`:17-23`) and `do_POST` (`:1028-1087`)

- [ ] **Step 1: Raise the request cap and add the bulk cap**

In the constants block, change `MAX_POST_BODY` and add `MAX_BULK`. The block at `viewer.py:17-23` is:

```python
MAX_BODY = 4096
NPA_NXX_PATH = Path(__file__).resolve().parent / "data" / "npa-nxx-on.json"
LABELS_PATH = Path(__file__).resolve().parent / "labels.json"
MAX_LABEL_LEN = 64
MAX_CAPCODE_LEN = 10
MAX_LABELS = 5000
MAX_POST_BODY = 4096
```

Change the last line and add `MAX_BULK` after it (leave `MAX_BODY` at 4096 — it is unrelated):

```python
MAX_POST_BODY = 65536  # 64 KB: fits a ~8 KB 500-capcode bulk request under the Content-Length guard
MAX_BULK = 500         # max capcodes[] per bulk POST /labels
```

- [ ] **Step 2: Replace `do_POST` with the single-or-bulk version**

Replace the entire `do_POST` method (`viewer.py:1028-1087`) with the version below. It accepts either `{"capcode","label"}` or `{"capcodes":[...],"label"}` by treating single as a one-element list, reuses every existing guard, does a whole-batch `MAX_LABELS` pre-check, and writes once under the lock:

```python
    def do_POST(self):
        if self.path != "/labels":
            self.send_error(404)
            return
        # Local-only: reject anything not addressed to this loopback host.
        allowed = ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)
        if self.headers.get("Host", "") not in allowed:
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin is None or not any(origin == "http://" + h for h in allowed):
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
            if "capcodes" in payload:
                raw_capcodes = payload["capcodes"]
                if not isinstance(raw_capcodes, list):
                    raise TypeError
            else:
                raw_capcodes = [payload["capcode"]]
            label = str(payload.get("label", ""))
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        if len(raw_capcodes) == 0 or len(raw_capcodes) > MAX_BULK:
            self.send_error(400)
            return
        capcodes = []
        for c in raw_capcodes:
            cc = str(c).lstrip("0") or "0"
            if not cc.isascii() or not cc.isdigit() or len(cc) > MAX_CAPCODE_LEN:
                self.send_error(400)
                return
            capcodes.append(cc)
        label = label.replace("<", "").replace(">", "").strip()
        if len(label) > MAX_LABEL_LEN:
            self.send_error(400)
            return
        with _labels_lock:
            if label:
                new_count = len({c for c in capcodes if c not in _labels})
                over_cap = len(_labels) + new_count > MAX_LABELS
            else:
                over_cap = False
            if not over_cap:
                for cc in capcodes:
                    if label:
                        _labels[cc] = label
                    else:
                        _labels.pop(cc, None)
                snapshot = dict(_labels)
                save_labels(snapshot)
        if over_cap:
            self.send_error(400)
            return
        data = json.dumps(snapshot).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
```

- [ ] **Step 3: Confirm the module still imports**

Run: `python3 -c "import viewer; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Test single + bulk + validation against an ISOLATED server**

Per the Operational note, run a throwaway copy on port 8799 with a temp labels file — this never touches the real `labels.json` or the live viewer on 8732:
```bash
pkill -f 'viewer_test.py' 2>/dev/null; rm -f /tmp/labels_test.json
cp viewer.py /tmp/viewer_test.py
python3 - <<'PY'
s=open('/tmp/viewer_test.py').read()
s=s.replace('LABELS_PATH = Path(__file__).resolve().parent / "labels.json"','LABELS_PATH = Path("/tmp/labels_test.json")')
s=s.replace('PORT = 8732','PORT = 8799')
open('/tmp/viewer_test.py','w').write(s)
PY
python3 /tmp/viewer_test.py >/tmp/vt.log 2>&1 &
sleep 1
```
Then run each and confirm (note: port and Origin are **8799**):

Single still works (the `Origin` header is required since the labels feature):
```bash
curl -s -X POST http://127.0.0.1:8799/labels -H 'Content-Type: application/json' \
  -H 'Origin: http://127.0.0.1:8799' -d '{"capcode":"1","label":"X"}'
```
Expected: `{"1": "X"}`

Bulk sets several at once and returns the full map:
```bash
curl -s -X POST http://127.0.0.1:8799/labels -H 'Content-Type: application/json' \
  -H 'Origin: http://127.0.0.1:8799' -d '{"capcodes":["10","20","0030"],"label":"Team"}'
```
Expected: `{"1": "X", "10": "Team", "20": "Team", "30": "Team"}` (note `0030`→`30`).

Bulk delete (empty label removes the listed set):
```bash
curl -s -X POST http://127.0.0.1:8799/labels -H 'Content-Type: application/json' \
  -H 'Origin: http://127.0.0.1:8799' -d '{"capcodes":["10","20","30"],"label":""}'
```
Expected: `{"1": "X"}`

Validation (each prints the HTTP code):
```bash
# bad capcode in the list -> 400 (no partial write)
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8799/labels \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8799' \
  -d '{"capcodes":["10","abc"],"label":"Y"}'        # -> 400
# over MAX_BULK (501 entries) -> 400
python3 -c "import json;print(json.dumps({'capcodes':[str(n) for n in range(501)],'label':'Y'}))" > /tmp/big.json
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8799/labels \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8799' --data @/tmp/big.json   # -> 400
# capcodes not a list -> 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8799/labels \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8799' \
  -d '{"capcodes":"10","label":"Y"}'                # -> 400
# body over MAX_POST_BODY (64 KB) -> 413  (regression check: MAX_POST_BODY was raised 16x)
python3 -c "print('{\"capcode\":\"1\",\"label\":\"' + 'a'*70000 + '\"}')" > /tmp/huge.json
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8799/labels \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8799' --data @/tmp/huge.json   # -> 413
```
Then confirm `curl -s http://127.0.0.1:8799/labels` still shows `{"1": "X"}` (the bad-capcode batch did NOT partially write). Clean up (and confirm the real file is untouched):
```bash
pkill -f 'viewer_test.py'; rm -f /tmp/viewer_test.py /tmp/labels_test.json /tmp/big.json /tmp/huge.json /tmp/vt.log
```

- [ ] **Step 5: Commit**

```bash
git add viewer.py
git commit -m "Add bulk capcodes[] form to POST /labels"
```

---

## Task 2: Client detection functions + bulk save (`viewer.py` JS)

**Files:**
- Modify: `viewer.py` JS — constants block (`:534-537`), helpers near `frameDistance` (`:553-568`) and `saveLabel` (`:857-866`)

- [ ] **Step 1: Add the group constants**

Immediately after `const STITCH_WINDOW_MS = 8000;` (`viewer.py:537`), add:

```javascript
const GROUP_TIME_WINDOW = 60;  // seconds (ts is HH:MM:SS) — broadcast co-recipient window
const GROUP_MIN_SIZE = 3;      // distinct capcodes to count as a group, not a coincidence
```

- [ ] **Step 2: Add `groupKey`, `tsDistance`, `findGroup`**

Add these three functions right after `frameDistance()` (after `viewer.py:568`). They are the node-verified versions; `findGroup` reads the global `pages`, `GROUP_TIME_WINDOW`, and `isTest`:

```javascript
function groupKey(s) {
  // Normalize a body so byte-identical broadcasts match despite whitespace jitter.
  return s.trim().replace(/\s+/g, ' ');
}

function tsDistance(a, b) {
  // Wrap-aware distance in seconds between two "HH:MM:SS" stamps (handles midnight).
  const toSec = (t) => { const p = t.split(':').map(Number); return p[0] * 3600 + p[1] * 60 + p[2]; };
  let d = Math.abs(toSec(a) - toSec(b));
  if (d > 43200) d = 86400 - d;
  return d;
}

function findGroup(page) {
  // Distinct capcodes that received the same broadcast as `page` (identical body
  // within GROUP_TIME_WINDOW seconds). Excludes TEST pages. Includes page's own
  // capcode. Caller checks length >= GROUP_MIN_SIZE.
  if (isTest(page.body)) return [];
  const key = groupKey(page.body);
  const seen = new Set();
  const out = [];
  for (const q of pages) {
    if (isTest(q.body)) continue;
    if (groupKey(q.body) !== key) continue;
    if (tsDistance(q.ts, page.ts) > GROUP_TIME_WINDOW) continue;
    if (seen.has(q.capcode)) continue;
    seen.add(q.capcode);
    out.push(q.capcode);
  }
  if (!seen.has(page.capcode)) out.push(page.capcode);
  return out;
}
```

- [ ] **Step 3: Add `saveLabels` next to `saveLabel`**

Immediately after `saveLabel()` (after `viewer.py:866`), add the bulk sibling (identical except the body is `{capcodes,label}`):

```javascript
function saveLabels(capcodes, label) {
  fetch('/labels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ capcodes: capcodes, label: label }),
  })
    .then(r => (r.ok ? r.json() : null))
    .then(data => { if (data) labels = data; rerender(); })
    .catch(() => rerender());
}
```

- [ ] **Step 4: Syntax-check the inline JS (no server needed)**

Render the HTML directly via `render_html()` and extract the app JS — no server, no port, no `labels.json`:
```bash
python3 -c "import viewer; open('/tmp/page.html','wb').write(viewer.render_html())"
python3 -c "import re; h=open('/tmp/page.html').read(); m=re.findall(r'<script>(.*?)</script>', h, re.S); open('/tmp/app.js','w').write(max(m, key=len))"
node --check /tmp/app.js && echo "JS SYNTAX OK"
rm -f /tmp/page.html /tmp/app.js
```
Expected: `JS SYNTAX OK`.

- [ ] **Step 5: Unit-test the detection logic in node**

Create `/tmp/group_test.js` with the functions (copied from Step 2, but with `pages`/window/isTest passed in so they run standalone) and assertions, then run it:

```javascript
function groupKey(s){ return s.trim().replace(/\s+/g,' '); }
function tsDistance(a,b){ const toSec=t=>{const p=t.split(':').map(Number);return p[0]*3600+p[1]*60+p[2];}; let d=Math.abs(toSec(a)-toSec(b)); if(d>43200)d=86400-d; return d; }
const isTest = b => /THIS IS A TEST PERIODIC PAGE/i.test(b);
function findGroup(page, pages, windowSec){
  if(isTest(page.body)) return [];
  const key=groupKey(page.body); const seen=new Set(); const out=[];
  for(const q of pages){
    if(isTest(q.body))continue;
    if(groupKey(q.body)!==key)continue;
    if(tsDistance(q.ts,page.ts)>windowSec)continue;
    if(seen.has(q.capcode))continue;
    seen.add(q.capcode); out.push(q.capcode);
  }
  if(!seen.has(page.capcode)) out.push(page.capcode);
  return out;
}
function eq(a,b,m){ if(JSON.stringify(a)!==JSON.stringify(b)) throw new Error('FAIL '+m+': '+JSON.stringify(a)); console.log('PASS '+m); }
const B='TTA LEVEL 2: GO TO EMERG';
const bc=[ {capcode:'1',ts:'14:00:00',body:B},{capcode:'2',ts:'14:00:05',body:B},
           {capcode:'3',ts:'14:00:10',body:B},{capcode:'4',ts:'14:00:20',body:B},{capcode:'5',ts:'14:00:25',body:B} ];
eq(findGroup(bc[0],bc,60).sort(), ['1','2','3','4','5'], 'broadcast 5 distinct');
const dup=bc.concat([{capcode:'3',ts:'14:00:11',body:B}]);
eq(findGroup(dup[0],dup,60).sort(), ['1','2','3','4','5'], 'de-dupe');
const far=[ {capcode:'1',ts:'14:00:00',body:B},{capcode:'2',ts:'14:00:05',body:B},{capcode:'9',ts:'14:20:00',body:B} ];
eq(findGroup(far[0],far,60).sort(), ['1','2'], 'time-separated excluded');
eq(findGroup({capcode:'1',ts:'14:00:00',body:'THIS IS A TEST PERIODIC PAGE 7'},[],60), [], 'TEST excluded');
const jit=[ {capcode:'1',ts:'14:00:00',body:'A  B'},{capcode:'2',ts:'14:00:01',body:'A B '},{capcode:'3',ts:'14:00:02',body:' A B'} ];
eq(findGroup(jit[0],jit,60).sort(), ['1','2','3'], 'whitespace jitter');
const wrap=[ {capcode:'1',ts:'23:59:58',body:B},{capcode:'2',ts:'00:00:03',body:B},{capcode:'3',ts:'00:00:05',body:B} ];
eq(findGroup(wrap[0],wrap,60).sort(), ['1','2','3'], 'midnight wrap');
console.log('ALL PASS');
```

Run: `node /tmp/group_test.js`
Expected: six `PASS …` lines then `ALL PASS`. Then `rm -f /tmp/group_test.js /tmp/page.html /tmp/app.js`.

- [ ] **Step 6: Commit**

```bash
git add viewer.py
git commit -m "Add client-side broadcast group detection and bulk label save"
```

---

## Task 3: Group-aware label editor + checkbox row (`viewer.py` JS + CSS)

**Files:**
- Modify: `viewer.py` — `makePage` (`:723-752`), list click handler (`:834-848`), `startLabelEdit` (`:868-888`), CSS after `.label-edit:focus` (`:442`)

- [ ] **Step 1: Stamp the page id on the tag span**

In `makePage` (`viewer.py:723-752`), the tag span currently carries only the capcode:

```javascript
  const name = labels[p.capcode];
  const tag = name ? el('span', 'label', name) : el('span', 'tagbtn', '⊕ tag');
  tag.dataset.capcode = p.capcode;
  meta.appendChild(tag);
```

Add the page id so the editor can resolve the exact clicked page (for its `ts`/`body`):

```javascript
  const name = labels[p.capcode];
  const tag = name ? el('span', 'label', name) : el('span', 'tagbtn', '⊕ tag');
  tag.dataset.capcode = p.capcode;
  tag.dataset.id = p.id;
  meta.appendChild(tag);
```

- [ ] **Step 2: Pass the id through the click handler**

In the list click handler (`viewer.py:834-848`), the tag branch is:

```javascript
  const tag = e.target.closest('.label, .tagbtn');
  if (tag) {
    startLabelEdit(tag, tag.dataset.capcode);
    return;
  }
```

Pass the id too:

```javascript
  const tag = e.target.closest('.label, .tagbtn');
  if (tag) {
    startLabelEdit(tag, tag.dataset.capcode, tag.dataset.id);
    return;
  }
```

- [ ] **Step 3: Replace `startLabelEdit` with the group-aware version**

Replace `startLabelEdit` (`viewer.py:868-888`) entirely. It now: resolves the clicked page, computes the group, and (when the group is big enough) shows a default-checked checkbox row with an overwrite-impact note. All DOM is built with `el()`/`createElement` (textContent only). The cancel-on-blur is moved to a wrapper `focusout` that ignores focus moving to the checkbox; `keydown` is on the wrapper so Enter/Esc work from either field:

```javascript
function startLabelEdit(spanEl, capcode, pageId) {
  const page = pages.find(p => String(p.id) === String(pageId));
  const group = page ? findGroup(page) : [capcode];
  const isGroup = group.length >= GROUP_MIN_SIZE;

  const wrap = el('div', 'label-edit-group');
  const input = el('input', 'label-edit');
  input.type = 'text';
  input.value = labels[capcode] || '';
  input.placeholder = 'label…';
  wrap.appendChild(input);

  let checkbox = null;
  if (isGroup) {
    const others = group.filter(c => c !== capcode);
    const row = el('label', 'group-row');
    checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = true;
    row.appendChild(checkbox);
    const note = el('span', 'group-note', '');
    // Count only members that currently carry a DIFFERENT non-empty label, i.e.
    // would actually be re-tagged by the value being typed. Recompute as they type.
    const updateNote = () => {
      const val = input.value.trim();
      const changing = others.filter(c => {
        const cur = labels[c] || '';
        return cur !== '' && cur !== val;
      }).length;
      let t = 'also tag ' + others.length + ' others in this broadcast';
      if (changing > 0) t += ' (' + changing + ' will be re-tagged from a different label)';
      note.textContent = t;
    };
    updateNote();
    input.addEventListener('input', updateNote);
    row.appendChild(note);
    wrap.appendChild(row);
  }

  spanEl.replaceWith(wrap);
  input.focus();
  input.select();

  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) {
      const val = input.value.trim();
      if (checkbox && checkbox.checked) saveLabels(group, val);
      else saveLabel(capcode, val);
    } else {
      rerender();
    }
  };
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  // Cancel only when focus leaves the whole editor — not when it moves to the checkbox.
  wrap.addEventListener('focusout', (e) => {
    if (wrap.contains(e.relatedTarget)) return;
    finish(false);
  });
}
```

- [ ] **Step 4: Add CSS for the group row**

After the `.label-edit:focus { outline: none; }` rule (`viewer.py:442`), add:

```css
.label-edit-group {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
  vertical-align: middle;
}
.label-edit-group .group-row {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.label-edit-group .group-row input[type="checkbox"] {
  accent-color: var(--accent);
  margin: 0;
}
.label-edit-group .group-note {
  color: var(--muted);
  font-size: 10.5px;
  letter-spacing: 0.02em;
}
```

- [ ] **Step 5: Syntax-check (no server needed)**

```bash
python3 -c "import viewer; open('/tmp/page.html','wb').write(viewer.render_html())"
python3 -c "import re; h=open('/tmp/page.html').read(); m=re.findall(r'<script>(.*?)</script>', h, re.S); open('/tmp/app.js','w').write(max(m, key=len))"
node --check /tmp/app.js && echo "JS SYNTAX OK"
grep -c 'label-edit-group' /tmp/page.html   # >= 1
rm -f /tmp/page.html /tmp/app.js
```
Expected: `JS SYNTAX OK` and the grep count ≥ 1. (Interactive behavior is validated in the browser e2e below.)

- [ ] **Step 6: Commit**

```bash
git add viewer.py
git commit -m "Make label editor group-aware with bulk tag-all checkbox"
```

---

## Task 4: Documentation (`CLAUDE.md`, `README.md`)

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Extend the CLAUDE.md labels/hints note**

In `CLAUDE.md`, in the "### Capcode labels and callback hints" subsection, locate the sentence ending `POST /labels` ({"capcode","label"}; empty label deletes). — it is **mid-bullet**, immediately followed by `The client holds a `labels` object…` in the same bullet. Insert this sentence **immediately after** `empty label deletes).` and **before** `The client holds`:

```markdown
  `POST /labels` also accepts a bulk form `{"capcodes": [...], "label": "X"}` (one
  atomic write, capped at `MAX_BULK`) used by group-tagging.
```

Then add a new subsection after that one:

```markdown
### Group-tagging

A "group broadcast" is one message sent to many pagers at once (e.g. a trauma
team activation). `findGroup(page)` (client) finds the distinct capcodes that
received an **identical body within `GROUP_TIME_WINDOW` seconds** (keyed on the
`ts` field — broadcasts spread across FLEX home frames, so wall-clock proximity,
not frame distance, is the grouping axis; TEST pages excluded; `≥ GROUP_MIN_SIZE`
distinct capcodes to count). When you edit a label on a page that's part of a
group, the editor shows a default-checked "also tag N others" checkbox; checked +
Enter bulk-tags the whole broadcast (overwrite) via the bulk `POST /labels`.
```

- [ ] **Step 2: Add a README feature bullet**

In `README.md`, in the **Features** list, after the "Callback hints" bullet, add:

```markdown
- **Group tagging** — a single broadcast (e.g. a trauma team activation) goes
  out to a whole team's pagers at once. When you tag a capcode that's part of
  such a broadcast, the editor offers to label every co-recipient in one action,
  so one edit maps an entire team.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document group-tagging and the bulk /labels form"
```

---

## Final verification

- [ ] **Step 1: Self-tests / syntax green**

Run from the repo root:
```bash
python3 -c "import viewer; print('import ok')"
python3 build_npa_nxx.py --check
```
Expected: `import ok`, then `parse_csv ok` / `phone_hints ok` / `all checks passed` (the labels-feature self-test must still pass — group-tagging didn't touch it).

- [ ] **Step 2: Browser end-to-end (driven separately)**

A dedicated browser pass validates: a replay log with one body broadcast to ≥3 capcodes within the window → the editor shows "also tag N others" checked → tag-all persists every member to `labels.json` and filtering by the label surfaces the whole group; a single-recipient page shows **no** checkbox; unchecking tags only the one; clicking the checkbox does NOT cancel the edit.

- [ ] **Step 3: Confirm branch state**

Run: `git log --oneline capcode-labels..HEAD`
Expected: four group-tagging commits (bulk endpoint, detection+save, group-aware editor, docs) on top of the `capcode-labels` branch.
