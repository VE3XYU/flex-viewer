# Capcode Labels + Callback Hints — Design

- **Date:** 2026-05-28
- **Status:** Approved (brainstorm), pending implementation plan
- **Component:** `viewer.py` (FLEX live page viewer)

## Goal

Help the operator answer *"which institution is this page for?"* on a paging
channel where the RF cannot tell you.

The decoded channel (929.2875 MHz in Newmarket, ON) is a single carrier's
(**Paging Network of Canada Inc.**) province-wide **simulcast** network — 67
Ontario transmitter sites, many co-located at hospitals, all keying up with the
same bitstream. Because pages are injected upstream at the carrier's terminal
and broadcast everywhere identically, neither the transmitter, the licence, nor
signal direction distinguishes one institution from another. That information
exists only in two places in what we decode:

1. **The capcode** — the carrier assigns each customer a block of capcodes, so a
   given institution's pages cluster onto a stable set of capcodes.
2. **The body** — department names, code calls, and especially **callback
   numbers** whose area code + exchange (NPA-NXX) map to a town.

This feature turns the viewer from a passive feed into a tool for building that
who's-who map over time.

## Scope

### In scope (v1)
- **Capcode labels** — manually tag an exact capcode with a free-text name
  ("Southlake"). The label follows that capcode through live + history,
  persists server-side, and is filterable.
- **Callback hints** — auto-detect phone numbers in page bodies and annotate
  each with its **town** (rate centre), via a bundled Ontario NPA-NXX dataset.

### Out of scope (explicit non-goals)
- Capcode **ranges/blocks** (label one exact capcode at a time).
- **User-taught** exchange→place mappings (the bundled dataset is the only
  source of town hints).
- Non-Ontario number data.
- Any **individual-level** identification. Institution/operator level only.

## Architecture

One deliberate split, driven by how the two data sources behave:

| Concern | Lives | Why |
|---|---|---|
| **Capcode labels** | **Client** (JS map over a REST pair) | Dynamic: added at runtime, and every visible page for that capcode must update instantly. A client-held map makes that a cheap re-render. |
| **Callback hints** | **Server** (Python annotation) | Static + large: a multi-thousand-row NPA-NXX table. Doing the lookup in Python and attaching a `hints` field per record keeps the table entirely server-side and out of every browser payload. |

The rejected alternative — shipping the whole NPA-NXX table to the browser and
doing lookups in JS — bloats every page load and splits logic across two places.

Both record-producing paths (`prime_history` at startup, `tail_log` live) flow
through `parse_record`, so attaching hints there covers history + live with one
call site. Labels are applied at render time from the client's map, so they
update live regardless of when a page arrived.

## Component 1 — Capcode labels

### Data model
`labels.json`, next to `viewer.py`:

```json
{ "1234567": "Southlake", "1234570": "Sunnybrook" }
```

Capcode keys use the **leading-zero-stripped** form the UI already displays
(`parse_record` does `lstrip("0")`).

### Server
- Load `labels.json` into an in-memory dict at startup; guard with a lock
  (mirrors `_state_lock` / `_subscribers` pattern). Missing/corrupt file → start
  empty, log nothing fatal.
- Atomic writes: write to a temp file in the same dir, then `os.replace()`.

### Endpoints
The server stops being strictly GET-only. This is the one accepted ethos shift;
CLAUDE.md's "everything else 404s" note must be updated.

- **`GET /labels`** → `200 application/json`, body is the full map. Client
  fetches once on load.
- **`POST /labels`** → request body `{"capcode": "1234567", "label": "Southlake"}`.
  An empty/whitespace label **deletes** the entry. On success returns `200` with
  the **full updated map** (client replaces its copy wholesale — no merge logic).

### Validation & hardening
- `capcode`: digits only, length ≤ 10; reject otherwise (`400`).
- `label`: trimmed; length ≤ 64; stored as plain text with any HTML/`<`/`>`
  stripped (it is rendered via `textContent`, never as HTML).
- Reject `Content-Length` greater than a few KB (`413`).
- Cap total labels at ~5000 (`400` when exceeded).
- **Local-only guard:** server already binds `127.0.0.1`. Additionally, on
  `POST`, reject (`403`) if the `Host` header is not `127.0.0.1[:PORT]` /
  `localhost`, or if an `Origin` header is present and is not localhost. Cheap
  defense against a stray web page poking the port.

### Client
- A `labels` object (`capcode → name`) is the display source of truth.
- On load: `fetch('/labels')` → populate `labels` → `rerender()`. SSE
  (`connect()`) runs independently; any later label load just re-renders.
- **Render:** in the `.meta` row, immediately after the capcode:
  - Labeled → `<span class="label">Southlake</span>`.
  - Unlabeled → a faint `⊕ tag` affordance, visible on row hover.
- **Edit interaction** (does not collide with existing capcode click-to-filter):
  - Clicking the **capcode number** keeps current behavior (filter by capcode).
  - Clicking the **label** or **`⊕ tag`** swaps it for an inline `<input>`
    prefilled with the current label, focused. **Enter** → `POST /labels`, then
    update `labels` + `rerender()`. **Esc** → cancel. Empty + **Enter** → delete.
- **Filter integration:** `matches()` also tests label text, so typing
  `southlake` in the existing filter box surfaces every capcode tagged Southlake
  — the "group by institution" view, with no new UI:

  ```js
  const lbl = (labels[p.capcode] || '').toLowerCase();
  return p.capcode.includes(filter)
      || p.body.toLowerCase().includes(filter)
      || lbl.includes(filter);
  ```
- A label change triggers a full `rerender()` (cheap at ≤ 500 pages) so all
  visible pages for that capcode update at once.

## Component 2 — Callback hints

### Dataset
- **`build_npa_nxx.py`** — a standalone, run-once-offline generator (stdlib
  `urllib` only; **not** part of the runtime stack). For each Ontario NPA it
  fetches CNAC's CO Code Status CSV, keeps in-service codes, and writes a compact
  `data/npa-nxx-on.json`.
  - Ontario NPAs: `416 647 437 905 289 365 742 519 226 548 613 343 753 705 249
    683 807` (overlays included where present).
  - Source: CNAC (`cnac.ca`) per-NPA "CO Code Status" CSV exports.
  - Compact, town-interned format to keep size down (a few hundred KB expected):

    ```json
    { "Newmarket, ON": ["905895", "905830"], "Toronto, ON": ["416340"] }
    ```
  - Re-runnable to refresh; documented in the README.
- **`--check` flag** on the generator runs inline assertions on the hot,
  error-prone bits (phone-shape regex + NPA-NXX lookup) against synthetic cases.

### Server
- At startup, load `data/npa-nxx-on.json` and invert to a flat read-only dict
  `{"905895": "Newmarket, ON"}` (no lock — read-only after load).
- **Missing file → hints silently disable**; the viewer still boots and serves.
- `phone_hints(body)` helper, called inside `parse_record`, attaches:

  ```python
  rec["hints"] = [{"num": "905-555-0142", "place": "Newmarket, ON"}, ...]
  ```

- **Detection** — conservative NANP shapes only. Proposed pattern:

  ```python
  PHONE_RE = re.compile(
      r'(?<!\d)(?:\+?1[ .\-]?)?\(?([2-9]\d{2})\)?[ .\-]?([2-9]\d{2})[ .\-]?(\d{4})(?!\d)'
  )
  ```

  NPA and NXX both start `2-9` (NANP rule); `(?<!\d)`/`(?!\d)` prevent matching
  inside longer digit runs. Only **10-digit** numbers resolve (need NPA+NXX);
  bare extensions and 7-digit locals are ambiguous and left alone. Best-effort —
  occasional false positives in packed numeric pages are acceptable.

### Client
- Render hints as their own line **below** the body, e.g.
  `↳ 905-555-0142 — Newmarket, ON`, built with `textContent` only.
- This **never** touches the DOMPurify HTML path. The invariant — *DOMPurify is
  the only thing that writes HTML into the DOM* — is preserved.

## UI / layout

Label sits after the capcode; hint line sits under the body. *(Phone numbers
below are synthetic.)*

```
 15:42:07  1234567 · Southlake        ALN   A · 12.045
 ┃ Code team to ER, callback 905-555-0142
 ↳ 905-555-0142 — Newmarket, ON

 15:42:09  7654321  ⊕ tag             NUM   A · 12.061     ← unlabeled: faint on hover
 ┃ please call 416-555-0173
 ↳ 416-555-0173 — Toronto, ON
```

New CSS classes: `.label`, `.tagbtn`, `.label-edit` (input), `.hints` /
`.hint`. Style consistent with existing `.badge` / `.capcode` treatment.

## Privacy

System/institution level only, per project rules and the standing "no PHI in
artifacts" guidance. All examples in this spec, the README, and generator
self-check data are **synthetic** — no real decoded page bodies, no real pager
callback numbers — even though rate-centre data itself is public.

## Testing / verification

No test framework added (project is stdlib-only, no suite). Instead:

- `build_npa_nxx.py --check`: inline assertions for phone-shape detection and
  NPA-NXX → town lookup on synthetic cases.
- Manual checklist:
  1. Add a label → reload the page → label still present (persisted server-side).
  2. Edit a label to empty + Enter → it is removed, and stays removed after reload.
  3. A page body with a known synthetic in-dataset number shows the correct town.
  4. Rename `data/npa-nxx-on.json` away → viewer still boots, hints absent.
  5. Click capcode number → still filters by capcode (no regression).
  6. Type an institution name in the filter → all its capcodes appear.

## Footprint / file changes

- **`viewer.py`** (runtime, stays the only runtime Python file, stdlib only):
  labels dict + lock + load/save, `GET`/`POST /labels`, NPA-NXX load,
  `phone_hints()`, `rec["hints"]`, new CSS + JS for label UI, hint rendering,
  `matches()` change.
- **`data/npa-nxx-on.json`** (new, bundled data).
- **`build_npa_nxx.py`** (new, offline tool, not runtime).
- **`CLAUDE.md`** + **README**: note the server is no longer GET-only; document
  the labels store and the dataset regeneration command.

## Future (deferred, not v1)
- Capcode range/block labels with precedence rules.
- User-taught exchange→place mappings layered over the dataset.
- Non-Ontario NPA coverage.
- Optional "group by label" dedicated view (filter-box search covers it for now).
