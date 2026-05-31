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
        if not town or not prov or not npa.isdigit() or not nxx.isdigit() or len(npa) != 3 or len(nxx) != 3:
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
        grouped[place] = sorted(set(grouped[place]))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(grouped, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print("wrote %s: %d places, %d codes" % (OUT_PATH, len(grouped), total))


# ---- self-tests (offline, no network) ----
SAMPLE_CSV = (
    '"NPA","CO Code (NXX)","Status","Pooled","Exchange Area","Province","Company","OCN","Remarks"\n'
    '905,200,"In Service","N","Castlemore","ON","Rogers","8377",\n'
    '905,201,"In Service","N","Markham","ON","Bell Canada","8051",\n'
    '905,999,"Not Available","N","","","","",\n'
)


SHOULD_MATCH = [
    "905-555-0142", "(905) 555-0142", "905.555.0142", "9055550142",
    "+1 905 555 0142", "1-905-555-0142", "call 416-555-0173 now",
    "Ph (647) 555-0190 ext",
]
SHOULD_NOT_MATCH = [
    "1234567", "0123456789012345", "15:42:07", "12.045", "x5512", "2026-05-28",
    "ID9055550142",
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


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        build()
