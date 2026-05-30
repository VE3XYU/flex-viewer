# Group-Tagging — Design

- **Date:** 2026-05-29
- **Status:** Approved (brainstorm), pending implementation plan
- **Component:** `viewer.py` (FLEX live page viewer)
- **Builds on:** the capcode-labels feature (branch `capcode-labels` / PR #3). This work is stacked on that branch.

## Goal

Turn a single **group broadcast** into a labeled team in one action.

Many institutional pages are broadcasts: one event (a trauma team activation, a code, a mass alert) is sent simultaneously to *many* pagers. The same message body lands on dozens of capcodes at once. That co-occurrence is the strongest attribution signal available — it identifies an entire functional group (e.g. a hospital's trauma team) in one shot. Today you can only label those capcodes one at a time. This feature detects the broadcast and lets you **bulk-tag every co-recipient** with one edit.

## Key insight: group by time, not frame

FLEX assigns each capcode a **home frame**, so a single broadcast to N pagers is delivered across *many different frames* as each recipient's frame airs — frame-distance between co-recipients is unreliable (can span much of a cycle). The robust signal that pages belong to the same broadcast is **identical body + close in wall-clock time**. So group detection keys on the `ts` field, not `frame`. (This is the opposite axis from multi-fragment *stitching*, which correctly uses frame proximity for one recipient.)

## Scope

### In scope (v1)
- **Group detection** (client-side) over the buffered `pages`.
- **Bulk-tag UX**: an inline "also tag the N co-recipients" control on the existing label editor; **overwrite** mode.
- **Bulk write** (server): `POST /labels` extended to accept a list of capcodes in one atomic write.

### Out of scope (deferred — see end)
- A standalone "detected groups" panel.
- UI-configurable thresholds.
- Cross-session group memory.
- **Tag-filter chips** (user-requested, for later — see Deferred).

## Group detection (client-side)

A **group** for a page `p` is the set of buffered pages `q` where:

1. **Identical normalized body** — `groupKey(body) === groupKey(p.body)`, where `groupKey(s) = s.trim().replace(/\s+/g, ' ')` (collapse whitespace; tolerates minor decoder jitter while staying exact in substance).
2. **Within a time window** — `tsDistance(q.ts, p.ts) <= GROUP_TIME_WINDOW`. `ts` is `HH:MM:SS`; parse to seconds-of-day and take the wrap-aware distance: `d = abs(a-b); d = min(d, 86400 - d)`. Default `GROUP_TIME_WINDOW = 60` (s).
3. Collected into **≥ `GROUP_MIN_SIZE` distinct capcodes** (default `3`). Two could be coincidence; three identical bodies in a 60-second window is a broadcast.

Additional rules:
- **Exclude TEST pages** (`isTest(body)`): carrier "THIS IS A TEST PERIODIC PAGE" broadcasts go to everyone and would form a giant useless group.
- Detection runs over the live `pages` array (history-primed + live, ≤500). `findGroup(p)` is a pure scan — cheap at this size, recomputed when the editor opens.
- Identical-body is the strong discriminator; the time window mainly separates *repeat* activations into distinct groups. Both thresholds are tunable constants declared alongside `STITCH_WINDOW_MS`.

`findGroup(p)` returns the array of **distinct capcodes** in the group (including `p`'s own), or a short/empty result when no group is found (size `< GROUP_MIN_SIZE`).

## Architecture — client detects, server stores

| Concern | Lives | Change |
|---|---|---|
| Group **detection** | Client | New `findGroup(p)` pure function over `pages`. No server involvement — the client already holds every page and the labels map. |
| Bulk **write** | Server | Extend `POST /labels` to also accept `{"capcodes": [...], "label": "X"}` and apply all under one `_labels_lock` + one atomic `save_labels`. |

### Server: bulk `POST /labels`
The handler accepts **either** shape:
- existing single: `{"capcode": "1234567", "label": "X"}`
- new bulk: `{"capcodes": ["1234567", "7654321", ...], "label": "X"}`

Bulk behavior:
- Each capcode is normalized (`lstrip("0") or "0"`) and validated (digits, `≤ MAX_CAPCODE_LEN`); an invalid entry → `400`.
- Label validated as today (`≤ MAX_LABEL_LEN`, `<`/`>` stripped); empty label deletes each listed capcode.
- A per-request cap `MAX_BULK = 500` on list length → `400` if exceeded (bounds the request; far above any real broadcast, which is dozens). To keep the two caps consistent, `MAX_POST_BODY` is raised from 4 KB to **64 KB** so a full bulk request fits under the Content-Length guard (a 500-capcode list is ~8 KB). The single-tag path is unaffected, and the endpoint stays localhost-only and guarded, so 64 KB remains a tight cap.
- All mutations applied **under one `_labels_lock`**, respecting `MAX_LABELS` (if applying the batch would exceed it, reject `400` without partial application). One `save_labels(snapshot)` write. Returns the **full updated map** (client replaces its copy wholesale, as today).
- The existing single-capcode form and all its validation/guards (localhost Host/Origin, Content-Length, 400/403/413) are unchanged.

### Client: bulk write
A new `saveLabels(capcodes, label)` posts the bulk shape and, on success, replaces `labels` with the returned map and calls `rerender()`. The existing `saveLabel(capcode, label)` is unchanged for the single case.

## UX — inline on the existing editor

When `startLabelEdit(spanEl, capcode)` opens, it computes `findGroup(p)` for that capcode's page. If the group size ≥ `GROUP_MIN_SIZE`, the editor renders an extra row under the input:

```
 14:02:07  1234567  [ Sunnybrook – Trauma________ ]
                    ☑ also tag 18 others in this broadcast
                       (3 will be re-tagged from a different label)
                    Enter saves all 19 · Esc cancels
```

- A native `<input type="checkbox">` (default **checked**) plus a short text note built via the `el()` helper (textContent only — the DOMPurify-only-writes-HTML invariant is preserved; no `innerHTML`).
- The note shows the co-recipient count and, when relevant, how many currently carry a *different* non-empty label (`will be re-tagged`) — surfacing the **overwrite** impact inline, no separate modal.
- **Checked + Enter** → `saveLabels(groupCapcodes, value)` (overwrite every member). **Unchecked + Enter** → `saveLabel(capcode, value)` (today's single behavior). **Esc** cancels. Empty value + Enter deletes (single or whole group per the checkbox).
- After save, `rerender()` lights up the whole group at once; filtering by the label then surfaces the team.
- A page with no detected group shows the editor exactly as today (no checkbox).

## Privacy
Unchanged from the labels feature: institution/operator level only; `labels.json` stays gitignored; no real callback numbers or decoded bodies in code, docs, or fixtures (examples synthetic).

## Testing / verification
No test framework added (project ethos). Verification:
- **Server (curl):** bulk form sets multiple capcodes in one call and returns the full map; a list longer than `MAX_BULK` → `400`; a body over `MAX_POST_BODY` → `413`; a bad capcode in the list → `400` (no partial write); empty label deletes the listed set.
- **Browser e2e:** a replay log with one body broadcast to N (≥3) capcodes within the window → the editor shows "also tag N others" checked by default → tag-all persists every member to `labels.json` and filtering by the label surfaces the group; a single-recipient page shows **no** checkbox; unchecking tags only the one.

## Footprint
- `viewer.py` (single runtime file, stdlib only): `do_POST` bulk branch + `MAX_BULK` (and `MAX_POST_BODY` raised to 64 KB); JS `groupKey`, `tsDistance`, `findGroup`, group-aware `startLabelEdit`, `saveLabels`, the `GROUP_TIME_WINDOW`/`GROUP_MIN_SIZE` constants; a few CSS lines for the checkbox row.
- `CLAUDE.md` + `README`: document the bulk `/labels` form and group-tagging.

## Deferred / future
- **Tag-filter chips (user-requested, for later):** per-label toggle chips to show/hide pages by tag, most likely a **second chip row in the header** mirroring the ALN/NUM/TON/TEST type chips and reusing that toggle interaction. Placement and behavior to be designed in its own pass.
- Standalone "detected groups" panel (browse all clusters at once).
- UI-configurable detection thresholds (window / min size).
- Cross-session persistence of detected groups.
- Group-aware *suggestions* (e.g. "this capcode usually co-occurs with the Sunnybrook trauma group").
