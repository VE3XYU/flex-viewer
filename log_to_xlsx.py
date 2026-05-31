#!/usr/bin/env python3
"""Convert the FLEX decode log into a spreadsheet (.xlsx + .csv).

Reuses viewer.py's parser so capcode/body/field handling matches the live feed.
Note: multi-fragment ALN messages are emitted as one row per OTA fragment (the
live UI stitches them into a single "N parts" page; this export does not).
Joins each row with its capcode label from labels.json, and adds a
callback-town hints column from the bundled NPA-NXX data.

  python3 log_to_xlsx.py                 # -> ./flex-log-<timestamp>.xlsx + .csv
  python3 log_to_xlsx.py /path/to/out    # -> /path/to/out.xlsx + .csv
  LOG_PATH=/tmp/other.log python3 log_to_xlsx.py

Output is real Excel (frozen header + auto-filter on every column) built with
the standard library only -- no pip, no openpyxl.

PRIVACY: the log is real decoded paging traffic (may contain PHI). Output is
written into the project directory (next to this script) and *.csv/*.xlsx are
gitignored, so exports stay local and can't be committed. Still: keep them
local, do not share.
"""
import csv
import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import viewer  # noqa: E402  (sibling module; reuse its parser + paths)

COLUMNS = ["Date", "Time", "Capcode", "Label", "Type", "Mode",
           "Frame", "Proto", "Callback hints", "Body"]

# XML 1.0 forbids most control chars; strip them so the .xlsx stays valid.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Spreadsheet formula-injection guard. Page bodies are attacker-influenceable
# (anyone can transmit a FLEX page), and Excel/Numbers/LibreOffice treat a
# cell beginning with = + - @ (or a leading tab/CR) as a live formula when a
# CSV is opened -- so "=HYPERLINK(...)"/"=cmd|..." in a body would execute.
# Prefix such values with a single quote, the standard CSV-injection defense,
# which the app shows as plain text. (The .xlsx path is already safe: cells are
# written as inlineStr text, never formulas -- this guard is for the .csv.)
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(val):
    s = str(val)
    if s and s[0] in _FORMULA_LEAD:
        return "'" + s
    return s


def _esc(s):
    s = _ILLEGAL.sub("", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col(n):  # 1 -> A, 27 -> AA
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def read_records():
    log = viewer.LOG_PATH
    if not log.exists():
        sys.exit("log not found: %s" % log)
    viewer.load_npa_nxx()  # so parse_record fills the hints column
    try:
        with open(viewer.LABELS_PATH, encoding="utf-8") as f:
            labels = json.load(f)
        if not isinstance(labels, dict):
            labels = {}
    except Exception:
        labels = {}
    records, buf = [], []
    with open(log, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if viewer.HEADER_RE.match(line):
                if buf:
                    rec = viewer.parse_record("\n".join(buf))
                    if rec:
                        records.append(rec)
                buf = [line]
            elif buf:
                buf.append(line)
    if buf:
        rec = viewer.parse_record("\n".join(buf))
        if rec:
            records.append(rec)
    return records, labels, log


def row_for(rec, labels):
    hints = "; ".join("%s -> %s" % (h["num"], h["place"]) for h in rec.get("hints", []))
    body = (rec.get("body") or "").replace("\n", " / ")  # one clean line per record
    return [rec["date"], rec["ts"], rec["capcode"], labels.get(rec["capcode"], ""),
            rec["type"], rec["mode"], rec["frame"], rec["proto"], hints, body]


def write_csv(path, rows):
    # utf-8-sig so Excel reads accents correctly on open. Every value is run
    # through _csv_safe to neutralize spreadsheet formula injection.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([_csv_safe(v) for v in r])


def _sheet_xml(rows):
    ncols = len(COLUMNS)
    last = _col(ncols)
    nrows = len(rows) + 1
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>',
        '</sheetView></sheetViews>',
        "<sheetData>",
    ]

    def emit(rownum, values):
        cells = []
        for ci, val in enumerate(values, start=1):
            ref = "%s%d" % (_col(ci), rownum)
            cells.append(
                '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (ref, _esc(str(val)))
            )
        parts.append('<row r="%d">%s</row>' % (rownum, "".join(cells)))

    emit(1, COLUMNS)
    for i, r in enumerate(rows, start=2):
        emit(i, r)
    parts.append("</sheetData>")
    parts.append('<autoFilter ref="A1:%s%d"/>' % (last, nrows))
    parts.append("</worksheet>")
    return "".join(parts)


def write_xlsx(path, rows):
    sheet = _sheet_xml(rows)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="FLEX log" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def _safe_xml(data):
    # Project is dependency-free, so no defusedxml. We only ever parse our own
    # freshly-written, escaped output here -- but reject any DTD/entity
    # declaration anyway to neutralize XXE / billion-laughs vectors. The XML
    # markup keywords are case-sensitive uppercase, so a plain substring check
    # is correct and cheap (no whole-blob uppercasing).
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("refusing to parse XML with a DOCTYPE/ENTITY declaration")
    return ET.fromstring(data)


def validate_xlsx(path, expected_rows):
    """Re-open and parse it so we know Excel will too."""
    with zipfile.ZipFile(path) as z:
        need = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml"}
        missing = need - set(z.namelist())
        if missing:
            raise ValueError("xlsx missing parts: %s" % missing)
        _safe_xml(z.read("xl/workbook.xml"))
        sheet = _safe_xml(z.read("xl/worksheets/sheet1.xml"))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rowcount = len(list(sheet.iter(ns + "row")))
    if rowcount != expected_rows + 1:
        raise ValueError("row count %d != expected %d" % (rowcount, expected_rows + 1))
    return rowcount


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) > 1:
        base = os.path.abspath(os.path.expanduser(sys.argv[1]))
        # The .gitignore safety net only covers the project dir. Warn (don't
        # block) if the operator aims a PHI-bearing export elsewhere.
        if os.path.commonpath([base, script_dir]) != script_dir:
            print("warning: writing PHI export outside the project dir (%s) -- "
                  "not gitignored there; keep it local" % os.path.dirname(base),
                  file=sys.stderr)
    else:
        # Default: write next to this script (the project dir). *.csv/*.xlsx are
        # gitignored, so the PHI-bearing export can't be committed.
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(script_dir, "flex-log-" + stamp)
    records, labels, log = read_records()
    rows = [row_for(r, labels) for r in records]

    xlsx_path = base + ".xlsx"
    csv_path = base + ".csv"
    write_xlsx(xlsx_path, rows)
    write_csv(csv_path, rows)
    rc = validate_xlsx(xlsx_path, len(rows))

    tagged = sum(1 for r in records if labels.get(r["capcode"]))
    with_hints = sum(1 for r in records if r.get("hints"))
    print("source log     : %s (%.1f KB)" % (log, log.stat().st_size / 1024))
    print("records         : %d" % len(rows))
    print("  with a label  : %d" % tagged)
    print("  with hints    : %d" % with_hints)
    print("xlsx (validated): %s  [%d rows incl. header]" % (xlsx_path, rc))
    print("csv             : %s" % csv_path)


if __name__ == "__main__":
    main()
