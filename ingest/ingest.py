#!/usr/bin/env python3
"""Ingest files to Elasticsearch"""

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import requests
from tqdm import tqdm

if not Path("./extracted/sha256").is_dir():
    Path("./extracted/sha256").mkdir()
if not Path("./extracted/sha256_rows").is_dir():
    Path("./extracted/sha256_rows").mkdir()

headers = {"content-type": "application/x-ndjson"}
success_list = [200, 201]
INDEX = "leakdata-index-000001"
# Separate from INDEX: one small document per ingest run, not leak data, so
# it has its own lifecycle and is never at risk of being mixed into a
# content search (item 25's remaining piece - the reconciliation counts
# below were previously only visible in the container's own stdout).
RUNS_INDEX = "deis-ingest-runs"
# Separate from INDEX for the same reason RUNS_INDEX is: item 21's
# structured-data-as-rows piece, .csv and .xlsx so far. One document per
# row rather than one flattened text blob per file - see parse_csv_rows(),
# parse_xlsx_rows(), and build_rows_bulk_body(). Its own index (rather
# than a field on the file's own document) because a single source file
# can have thousands of rows, and "leakdata-rows-*" already matches the
# existing Kibana index pattern's "leakdata-*" title, so no new one was
# needed there.
ROW_INDEX = "leakdata-rows-000001"
# Keep individual _bulk requests to a sane size - standard Elasticsearch
# guidance is a handful of MB per request, not so large that one slow/huge
# batch dominates a retry, not so small that batching stops paying off.
BULK_MAX_DOCS = 200
BULK_MAX_BYTES = 20 * 1024 * 1024

# What happened to a single file, for the summary at the end of a run.
INDEXED = "indexed"  # sent to Elasticsearch during this run
PRESENT = "present"  # a document for this content already existed
FAILED = "failed"  # could not be indexed, will be retried on the next run


def read_configuration(config_file):
    """Read configuration file."""
    config = configparser.RawConfigParser()
    config.read(config_file)
    if not config.sections():
        print("Can't find configuration file.")
        sys.exit(1)
    return config


def get_files(directory: Path) -> Iterable[Path]:
    """Return all files in specified and recursive directories."""
    return (file for file in directory.glob("**/*") if file.is_file())


def get_filehash(filename):
    """Return sha256 hash for filename"""
    sha256_hash = hashlib.sha256()
    try:
        with open(filename, "rb") as f:
            # Read and update hash string value in blocks of 4K
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    except FileNotFoundError:
        print("ERROR: Could not get sha256 for:", filename, flush=True)
        return None
    return sha256_hash.hexdigest()


def hash_link_exists(hash_value):
    """Return True if this hash has already been ingested successfully."""
    return Path("extracted/sha256/" + str(hash_value)).is_symlink()


def create_hash_link(hash_value, filename):
    """Create link from hash to file.

    Only called once Elasticsearch has confirmed the document (in a bulk
    response, per item), so the symlink doubles as the "this file is done"
    marker for later runs. Creating it earlier would mean a crash between the
    symlink and the upload leaves the file marked as done but never indexed -
    it would then be skipped forever.
    """
    sha256_link = Path("extracted/sha256/" + str(hash_value))
    if sha256_link.is_symlink():
        return False
    try:
        sha256_link.symlink_to("../" + str(filename).replace("extracted/", ""))
    except (FileExistsError, OSError, RuntimeError):
        if not sha256_link.is_symlink():
            print("ERROR: Could not create symlink for file:", filename, flush=True)
    return True


def row_hash_link_exists(hash_value):
    """Same idea as hash_link_exists, but tracked separately: a file whose
    content was already blob-indexed by an earlier run (before this
    feature existed, or with csv_rows disabled) still needs its rows
    picked up once csv_rows is on - a single shared marker would silently
    skip rows for the whole existing corpus forever. Keyed by the same
    content sha256, so a byte-identical duplicate never re-parses/re-sends
    rows either, the same as the blob marker already ensures for content.
    """
    return Path("extracted/sha256_rows/" + str(hash_value)).is_symlink()


def create_row_hash_link(hash_value, filename):
    """Create link from hash to file for the rows marker - see
    row_hash_link_exists. Only called once Elasticsearch has confirmed
    every row for this file (in a rows _bulk response), for the same
    crash-safety reason create_hash_link's docstring gives for the blob
    marker.
    """
    sha256_link = Path("extracted/sha256_rows/" + str(hash_value))
    if sha256_link.is_symlink():
        return False
    try:
        sha256_link.symlink_to("../" + str(filename).replace("extracted/", ""))
    except (FileExistsError, OSError, RuntimeError):
        if not sha256_link.is_symlink():
            print("ERROR: Could not create rows symlink for file:", filename, flush=True)
    return True


def index_url():
    """Return the base URL of the index.

    Credentials are never embedded here - passed separately via requests'
    auth= parameter (see elastic_auth()) instead, so a password can't end up
    in a request's URL and leak into an exception message, a printed error,
    or /logs/ingest.log the way it would if it were part of the URL string.
    """
    return "http://" + elastic_host + ":9200/" + INDEX


def elastic_auth():
    """Return the (username, password) tuple for requests' auth= parameter."""
    return ("elastic", password)


def elastic_document_count():
    """Return how many documents the index holds, or None if it can't be read.

    Refresh first, otherwise documents indexed moments ago are not counted yet.
    """
    try:
        requests.post(index_url() + "/_refresh", auth=elastic_auth(), timeout=30)
        response = requests.get(index_url() + "/_count", auth=elastic_auth(), timeout=30)
        if response.status_code not in success_list:
            return None
        return int(response.json()["count"])
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


def index_run_summary(counts):
    """Best-effort: index one reconciliation document per ingest run into
    RUNS_INDEX, so the counts print_summary() already prints to the
    container log are also visible in Kibana - a saved search sorted by
    @timestamp shows the full run history, not just whatever is in the log
    of the most recently run container. Never raises: a run that fully
    succeeded but couldn't reach Elasticsearch for this one extra write
    should not be reported as failed.
    """
    try:
        response = requests.post(
            f"http://{elastic_host}:9200/{RUNS_INDEX}/_doc",
            json=counts,
            auth=elastic_auth(),
            timeout=30,
        )
        if response.status_code not in success_list:
            print(f"ERROR: Could not index run summary (status {response.status_code}).", flush=True)
    except requests.exceptions.RequestException as error:
        print("ERROR: Could not index run summary:", error, flush=True)


def resolve_filepath(filename, hash_value):
    """Look up the original filename via sqlite, if enabled."""
    if not use_sqlite:
        return filename
    cur = con.cursor()
    res = cur.execute("SELECT original_filename FROM files WHERE sha256=?", (hash_value,))
    return res.fetchone()[0]


def parse_csv_rows(content: bytes, max_rows: int) -> tuple[list[dict], dict]:
    """Parses CSV bytes into one {column: value} dict per data row (the
    header row isn't counted as one), auto-detecting the delimiter with
    csv.Sniffer rather than assuming a fixed dialect - real corpus data
    mixes comma- and semicolon-delimited exports (Swedish locale
    conventions commonly use ';', since ',' is the decimal separator).

    The first row is always treated as a header. csv.Sniffer.has_header()
    was tried and dropped: confirmed empirically unreliable on exactly the
    realistic case this feature exists for - a header row of all-string
    column names (e.g. "namn;stad") followed by all-string data rows gives
    Sniffer no type contrast to key off, and it returns a confident but
    wrong False, silently demoting real column names to
    column_1/column_2/... for a large share of real business CSVs. A fixed
    "always a header" assumption is simpler and more predictable than a
    heuristic that fails on its own primary use case; the tradeoff is a
    genuinely header-less file has its first data row misread as column
    names instead.

    Never raises: garbage/non-CSV bytes, an empty file, or anything
    csv.Error can throw all come back as ([], {"error": ...}) instead of
    propagating - a single malformed CSV must not take down the whole
    ingest run (via prepare_file's own outer try/except turning any
    exception into a whole-file FAILED status) or block that file's own
    normal full-text indexing, only its per-row breakdown. Note this is
    "never raises", not "never produces rows for non-CSV input" - genuinely
    unparseable bytes may still yield some garbage-in-garbage-out rows
    rather than an empty result, since there's no reliable way to detect
    "this isn't really CSV-shaped" short of the parse itself succeeding.

    Encoding is guessed by trying, in order, utf-8-sig (strips a BOM -
    the common Excel-on-Windows case), cp1252 (the common Windows/
    Swedish "Save As CSV" default), then latin-1, which maps every byte
    0-255 and so can never itself raise UnicodeDecodeError - the last
    attempt always succeeds.

    Stops at max_rows and sets meta["truncated"] = True rather than
    parsing an unbounded number of rows into memory - a hostile or just
    unexpectedly huge CSV should degrade gracefully, not exhaust worker
    memory or stall the run. The file's own full-text blob document is
    unaffected by this cap either way.
    """
    try:
        text = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text or not text.strip():
            return [], {"error": "empty or undecodable content"}

        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        rows = []
        columns = None
        truncated = False
        for raw_row in csv.reader(text.splitlines(), dialect):
            if not raw_row:
                continue
            if columns is None:
                columns = [cell.strip() or f"column_{i + 1}" for i, cell in enumerate(raw_row)]
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(
                {columns[i] if i < len(columns) else f"column_{i + 1}": value for i, value in enumerate(raw_row)}
            )
        return rows, {"truncated": truncated}
    except Exception as error:  # noqa: BLE001 - one bad CSV must not stop the batch/run
        return [], {"error": repr(error)}


# item 21's .xlsx row-parsing (see parse_xlsx_rows below) is stdlib-only -
# zipfile + xml.etree.ElementTree, not openpyxl - per this project's
# minimize-dependencies posture: the actual need (cell values only, no
# styles/formulas/charts) doesn't justify a real dependency with its own
# transitive deps for what's achievable directly against the OOXML zip.

# Decompression-bomb guards for a single XML part (worksheet/sharedStrings)
# - the whole file is already bounded by ingest.py's own max_size before
# prepare_file ever reads it, but nothing else bounds how large one part
# inflates to, and unpack's own archive-bomb guards (item 18) never see
# inside a .xlsx - item 45 deliberately leaves it whole, not extracted, so
# this is the first place its internal structure gets touched. Two checks,
# mirroring item 18's own check_archive_safety() rather than inventing a
# new approach: a compression-ratio cap (the real defense - genuine
# spreadsheet XML, even heavy on repeated/formatted cells, doesn't compress
# anywhere near this; a deliberate bomb does) and a generous absolute
# backstop regardless of ratio. The first cut of this used a 100MB absolute
# cap alone, with no ratio check - live verification against this corpus's
# real .xlsx files found two legitimate payroll/timesheet reconciliation
# workbooks with a 107MB and 151MB worksheet part (heavy real formatting/
# formula-cache bloat, ordinary spreadsheets, not hostile), both wrongly
# rejected. Raised to match item 18's own max_extract_bytes/
# max_compression_ratio defaults in spirit instead of guessing again.
_XLSX_MAX_PART_BYTES = 1024 * 1024 * 1024  # 1 GiB
_XLSX_MAX_COMPRESSION_RATIO = 200  # uncompressed:compressed, same as item 18's default
# The fixed namespace URI OOXML uses for the "r:id" attribute on
# <sheet>/<c> etc. elements - a spec-defined constant, not something a
# workbook's own "r:" prefix choice can change (ElementTree resolves
# namespace prefixes to URIs, so the attribute key it hands back is always
# this URI regardless of source prefix).
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _xlsx_local_tag(elem) -> str:
    """Strips the OOXML namespace prefix ElementTree keeps on every tag
    (e.g. "{http://...}sheet" -> "sheet") so parsing code below can match
    by plain tag name without hardcoding every namespace URI involved.
    """
    return elem.tag.rsplit("}", 1)[-1]


def _xlsx_children(elem, local_name):
    """Direct children of `elem` matching `local_name`, namespace-agnostic
    (see _xlsx_local_tag) - deliberately not elem.iter(), which would also
    match nested occurrences several levels down and isn't what any
    caller below wants (a <row>'s <c> children, an <si>'s <t>/<r>
    children, etc. are always exactly one level deep in real OOXML).
    """
    return [child for child in elem if _xlsx_local_tag(child) == local_name]


def _xlsx_parse_xml_part(data: bytes) -> ET.Element:
    """Parses one OOXML XML part, rejecting anything with a DOCTYPE/ENTITY
    declaration first. Genuine Excel-produced XML never has one;
    xml.etree.ElementTree is safe against *external* entity expansion by
    default (per Python's own XML vulnerability notes) but does expand
    *internal* ones, so a hostile crafted part could otherwise cause a
    memory blowup ("billion laughs") despite that. A plain substring check
    is enough here, not a full XML-aware scan: a false positive only means
    a legitimate-but-unusual part is treated as unparseable, the same
    graceful "this file's rows didn't parse" outcome as any other parse
    failure below, never a crash.
    """
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("XML part contains a DOCTYPE/ENTITY declaration - refusing to parse")
    # Safe against external entity expansion by default; internal entity
    # expansion is guarded above. No safer stdlib parser to swap in.
    return ET.fromstring(data)


def _xlsx_read_part(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Reads one zip member by name, or None if it doesn't exist (some
    parts, like sharedStrings.xml, are legitimately optional). Checks the
    member's own compression ratio and uncompressed size - both known from
    the zip's central directory, without inflating it - against
    _XLSX_MAX_COMPRESSION_RATIO/_XLSX_MAX_PART_BYTES first; see those
    constants' own comment for why these guards exist here.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _XLSX_MAX_PART_BYTES:
        raise ValueError(f"{name} claims {info.file_size} bytes uncompressed - refusing to read")
    if info.compress_size > 0 and info.file_size / info.compress_size > _XLSX_MAX_COMPRESSION_RATIO:
        raise ValueError(f"{name} compresses {info.file_size}:{info.compress_size} - refusing to read a likely bomb")
    return zf.read(name)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Parses xl/sharedStrings.xml into an index-ordered list of strings -
    absent entirely for a workbook with no text cells, so a missing part
    is a normal empty result, not an error.
    """
    data = _xlsx_read_part(zf, "xl/sharedStrings.xml")
    if data is None:
        return []
    root = _xlsx_parse_xml_part(data)
    strings = []
    for si in _xlsx_children(root, "si"):
        # Plain <si><t>text</t></si>, or rich text split across runs -
        # <si><r><t>run</t></r><r><t>run2</t></r></si> - concatenate every
        # <t> found anywhere under this <si> either way.
        strings.append("".join(t.text or "" for t in si.iter() if _xlsx_local_tag(t) == "t"))
    return strings


def _xlsx_sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Returns [(sheet_name, worksheet_part_path), ...] in workbook order,
    by following xl/workbook.xml's <sheet r:id="rIdN"> through
    xl/_rels/workbook.xml.rels's rIdN -> Target mapping. Deliberately not
    assumed to be sheet1.xml/sheet2.xml/... in that order - real Excel
    output happens to follow that, but nothing in the format guarantees it
    for a workbook produced by an arbitrary exporter, plausible for a
    leak-corpus file.
    """
    workbook_xml = _xlsx_read_part(zf, "xl/workbook.xml")
    rels_xml = _xlsx_read_part(zf, "xl/_rels/workbook.xml.rels")
    if workbook_xml is None or rels_xml is None:
        raise ValueError("missing xl/workbook.xml or its relationships part")

    rel_target = {}
    for rel in _xlsx_children(_xlsx_parse_xml_part(rels_xml), "Relationship"):
        rel_target[rel.get("Id")] = rel.get("Target")

    targets = []
    for sheets in _xlsx_children(_xlsx_parse_xml_part(workbook_xml), "sheets"):
        for sheet in _xlsx_children(sheets, "sheet"):
            target = rel_target.get(sheet.get(_XLSX_REL_NS + "id"))
            if target is None:
                continue
            # Targets are relative to xl/ (e.g. "worksheets/sheet1.xml"),
            # occasionally already absolute ("/xl/worksheets/...") -
            # normalize both down to the same relative-to-xl/ shape, then
            # the actual in-zip path always has "xl/" back on the front.
            target = target.removeprefix("/xl/")
            targets.append((sheet.get("name", "Sheet"), f"xl/{target}"))
    return targets


def _xlsx_column_index(cell_ref: str) -> int:
    """Decodes a cell reference's column letters ("A1" -> 0, "B1" -> 1,
    "AA1" -> 26, ...) to a 0-based column index. Needed because Excel only
    emits <c> elements for non-empty cells, so a row's cells can't be
    aligned by their position in the XML alone - a genuinely empty middle
    cell (e.g. B1 missing between A1 and C1) must still leave a gap rather
    than shifting every later column left by one.
    """
    index = 0
    for char in cell_ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    """Resolves one <c> element to its display string. t="s" cells store a
    shared-string index in <v>; t="inlineStr" cells carry their own text
    directly in <is><t>; t="b" cells are "0"/"1"; anything else (numeric,
    or t="str" formula results) is <v> taken literally - dates stay their
    raw underlying serial number rather than being converted to a calendar
    date, a documented tradeoff (same spirit as CSV's undetectable
    header-less case), since resolving Excel's date system correctly needs
    the workbook's own number-format definitions, out of scope here.
    """
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        for is_elem in _xlsx_children(cell, "is"):
            return "".join(t.text or "" for t in is_elem.iter() if _xlsx_local_tag(t) == "t")
        return ""
    v_elems = _xlsx_children(cell, "v")
    if not v_elems or v_elems[0].text is None:
        return ""
    value = v_elems[0].text
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def parse_xlsx_rows(content: bytes, max_rows: int) -> tuple[list[dict], dict]:
    """Parses .xlsx bytes into one {column: value} dict per data row per
    sheet - mirrors parse_csv_rows's contract and guarantees exactly:
    never raises (garbage/corrupt/non-.xlsx bytes come back as
    ([], {"error": ...}) rather than propagating into prepare_file's own
    outer try/except, which would turn a bad .xlsx into a whole-file
    FAILED and lose that file's normal blob indexing too); the first row
    of each sheet is always treated as its header, same documented
    tradeoff as CSV.

    Each row dict additionally carries "_source_table" set to the sheet
    name - the one shape CSV never needs, since a workbook can have
    multiple sheets. row_number (assigned by the caller via enumerate, not
    here) stays a single sequence across every sheet in the file rather
    than resetting per sheet, which keeps the existing
    "<sha256>:<row_number>" _id scheme unique without needing to change it.

    max_rows caps the total across all sheets combined, consistent with
    csv_max_rows's per-file (not per-table) semantics; past the cap, stops
    and sets meta["truncated"] = True rather than continuing to parse an
    unbounded workbook into memory.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            shared_strings = _xlsx_shared_strings(zf)
            sheet_targets = _xlsx_sheet_targets(zf)

            rows: list[dict] = []
            truncated = False
            for sheet_name, part_path in sheet_targets:
                if truncated:
                    break
                sheet_xml = _xlsx_read_part(zf, part_path)
                if sheet_xml is None:
                    continue
                sheet_root = _xlsx_parse_xml_part(sheet_xml)
                columns = None
                for sheet_data in _xlsx_children(sheet_root, "sheetData"):
                    for row_elem in _xlsx_children(sheet_data, "row"):
                        cells: dict[int, str] = {}
                        for cell in _xlsx_children(row_elem, "c"):
                            ref = cell.get("r")
                            col_index = _xlsx_column_index(ref) if ref else len(cells)
                            cells[col_index] = _xlsx_cell_value(cell, shared_strings)
                        if not cells:
                            continue
                        ordered = [cells.get(i, "") for i in range(max(cells) + 1)]
                        if columns is None:
                            columns = [value.strip() or f"column_{i + 1}" for i, value in enumerate(ordered)]
                            continue
                        if len(rows) >= max_rows:
                            truncated = True
                            break
                        row = {
                            columns[i] if i < len(columns) else f"column_{i + 1}": value
                            for i, value in enumerate(ordered)
                        }
                        row["_source_table"] = sheet_name
                        rows.append(row)
            return rows, {"truncated": truncated}
    except Exception as error:  # noqa: BLE001 - one bad .xlsx must not stop the batch/run
        return [], {"error": repr(error)}


def prepare_file(fname: Path):
    """Hash and read one file, ready for a later bulk request.

    Runs in a worker process - this is the CPU/IO-bound part, still worth
    parallelizing - but never touches the network and never creates a
    marker; those only happen once a bulk request confirms success, back in
    the main process (see process_batch/process_rows_batch). Nothing raises
    out of here: a single unreadable or unusual file must not abort the
    whole run.

    Blob indexing (the file's full content, via Tika) and row indexing
    (item 21: .csv/.xlsx so far, one document per row) are tracked by two
    independent markers (see row_hash_link_exists), so a file already
    blob-indexed by an earlier run - most of the corpus, once a given
    format's row indexing ships - still gets its rows picked up rather
    than being silently skipped forever.

    Returns a dict, always with a "status" key: PRESENT (both already
    indexed, carries "sha256"), FAILED (could not even be hashed, carries
    "error"), "ready" (blob indexing needed, carries "fname", "sha256",
    "content" bytes, "message", and "rows"/"row_meta" if this file's
    extension has row indexing too), or "ready_rows_only" (blob already
    indexed, only rows are needed - carries the same keys as "ready"
    except the blob content is never sent to the file-level index, only
    used to parse rows).
    """
    try:
        sha256 = get_filehash(fname)
        if sha256 is None or len(sha256) != 64:
            return {"status": FAILED, "sha256": None, "error": f"ERROR: Could not get sha256 for file: {fname}"}

        suffix = fname.suffix.lower()
        if suffix == ".csv":
            rows_enabled, rows_max, rows_parser = csv_rows_enabled, csv_max_rows, parse_csv_rows
        elif suffix == ".xlsx":
            rows_enabled, rows_max, rows_parser = xlsx_rows_enabled, xlsx_max_rows, parse_xlsx_rows
        else:
            rows_enabled, rows_max, rows_parser = False, 0, None

        blob_needed = not hash_link_exists(sha256)
        needs_rows = rows_enabled and not row_hash_link_exists(sha256)
        if not blob_needed and not needs_rows:
            # This content is already indexed (blob and, if this file's
            # extension has row indexing, rows too), by an earlier run or
            # by an identical copy of the file elsewhere in the tree.
            return {"status": PRESENT, "sha256": sha256}

        if fname.stat().st_size > max_size:
            content = b""
            message = "to large"
        else:
            with open(fname, "rb") as f:
                content = f.read()
            message = "ok"

        rows, row_meta = (None, None)
        if needs_rows:
            rows, row_meta = rows_parser(content, rows_max) if message == "ok" else ([], {"error": message})
    except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
        return {"status": FAILED, "sha256": None, "error": f"ERROR: Failed to prepare {fname}: {error!r}"}
    return {
        "status": "ready" if blob_needed else "ready_rows_only",
        "fname": fname,
        "sha256": sha256,
        "content": content,
        "message": message,
        "rows": rows,
        "row_meta": row_meta,
    }


def load_sha256_set(path):
    """Reads a file of one sha256 per line into a set, or an empty set if it
    doesn't exist (e.g. unpack hasn't run, or ran before this file existed).
    """
    try:
        with open(path, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def load_lineage(path):
    """Reads status/lineage.jsonl (unpack/start.sh's record_lineage_edge)
    into a dict keyed by each extracted archive's own sha256 - one entry
    per archive hop, not per file inside it (item 45 found single archives
    expanding into 1000+ files, so a per-file log would be needlessly
    large; see record_lineage_edge's own docstring for why a per-hop log
    is the right size). Missing file or a malformed line are both normal,
    not errors: the former means nothing has been extracted with this
    feature active yet, the latter must never abort an ingest run over one
    bad line unpack.sh wrote (e.g. a container killed mid-write).
    """
    edges = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    edge = json.loads(line)
                    edges[edge["sha"]] = edge
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return edges


def load_source_urls(path):
    """Reads status/source_urls.jsonl (deis/done.sh) into a dict keyed by
    sha256 - one entry per downloaded file, each the root of its own
    provenance chain. Same never-raise reasoning as load_lineage above.
    """
    urls = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    urls[record["sha256"]] = record
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return urls


def _immediate_parent_sha(filename: str) -> str | None:
    """The immediate parent archive's sha256, if any - already encoded for
    free in an extracted file's own path ("extracted/files/<sha>/..."),
    the one level of nesting item 46's problem statement calls
    "accidental" but reliable. resolve_source_chain below only needs
    unpack/start.sh's lineage table (see load_lineage) for anything past
    this first hop.
    """
    parts = Path(filename).parts
    for i in range(len(parts) - 2):
        if parts[i] == "extracted" and parts[i + 1] == "files":
            candidate = parts[i + 2]
            if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
                return candidate
    return None


def resolve_source_chain(filename: str, sha256: str) -> dict:
    """Reconstructs a document's full provenance chain, root to leaf, by
    walking unpack/start.sh's recorded lineage edges (one per extracted
    archive) back to a root, then looking up that root's download URL
    (deis/done.sh's source_urls.jsonl). Never raises and never loops
    forever on a corrupted table (the `seen` guard) - a file with no
    recorded lineage or origin is a normal, expected case (added via
    `deis add-files`, or from before this feature existed), not an error.

    sha256s/filenames/archive_types deliberately exclude the document's
    own sha256/filename - those are already the document's own top-level
    fields, this is ancestors only, root-first.
    """
    sha256s: list[str] = []
    filenames: list[str] = []
    archive_types: list[str] = []

    current = _immediate_parent_sha(filename)
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        edge = lineage_by_sha.get(current)
        sha256s.insert(0, current)
        if edge is None:
            # This sha was never itself produced by an extraction - it's
            # the root, and record_lineage_edge was never called for it.
            filenames.insert(0, source_urls_by_sha.get(current, {}).get("filename", ""))
            archive_types.insert(0, "")
            break
        filenames.insert(0, edge.get("filename", ""))
        archive_types.insert(0, edge.get("archive_type", ""))
        current = edge.get("parent_sha") or None

    root = sha256s[0] if sha256s else sha256
    url = source_urls_by_sha.get(root, {}).get("url", "")
    return {"url": url, "sha256s": sha256s, "filenames": filenames, "archive_types": archive_types}


def extraction_status(hash_value):
    """Whether unpack could extract this file's content, if it was ever an
    archive at all - "encrypted"/"corrupt"/"unsafe"/"multivolume" mean the
    document's content is the opaque original, not what was inside it.
    Kibana's "File without text content" search already showed files with
    no content; this says why, for files unpack itself flagged rather than
    a still-unclassified case (an image, a format Tika doesn't parse,
    etc.) - see item 34. "unsafe" (item 18) means unpack rejected the
    archive before ever attempting extraction - a zip-slip entry, a
    decompression-bomb shape, or not enough disk space - so unlike
    "corrupt" it says nothing about whether the archive itself was
    otherwise valid. "multivolume" means a non-first volume of a
    multi-volume archive that's missing its other parts entirely (the
    ordinary case - a full, resolvable set - never reaches ingest at all;
    unpack's resolve_multivolume_stragglers disposes of those before this
    file ever runs) - unlike the others, that makes it an actionable "go
    find the missing volume" flag rather than a permanent characteristic
    of the file itself. "decrypted" means this document was individually
    password-protected (a single encrypted .docx/.xlsx/.pdf, as opposed to
    an encrypted archive - see item 21's password-cracking write-up) and
    unpack recovered it by trying deis.cfg's password list - a positive
    signal (full content is available and was Tika-parsed normally), but
    flagged so an analyst can tell "this was originally encrypted and we
    cracked it" apart from a document that was never protected, matching
    the audit-trail spirit of the other flags above. Never collides with
    them: a recovered document's hash is computed from its decrypted
    content, which by definition differs from whatever hash (if any) an
    unresolved encrypted/corrupt/unsafe/multivolume file was tracked under.
    """
    if hash_value in still_encrypted:
        return "encrypted"
    if hash_value in still_corrupt:
        return "corrupt"
    if hash_value in still_unsafe:
        return "unsafe"
    if hash_value in still_multivolume:
        return "multivolume"
    if hash_value in decrypted:
        return "decrypted"
    return "ok"


def build_bulk_body(items):
    """Builds the newline-delimited JSON body for one _bulk request.

    File content is base64-encoded: the _bulk endpoint only accepts JSON,
    not the CBOR the single-document PUT this replaced used to send raw
    bytes with (Elasticsearch rejects "Content-Type: application/cbor" on
    _bulk) - verified against the live pipeline that a base64 "data" field
    still gets correctly picked up and decoded by the attachment processor.
    """
    lines = []
    for item in items:
        lines.append(json.dumps({"index": {"_id": item["sha256"]}}))
        doc = {
            "filename": resolve_filepath(str(item["fname"]), item["sha256"]),
            "sha256": item["sha256"],
            "data": base64.b64encode(item["content"]).decode("ascii"),
            "mtime": int(item["fname"].stat().st_mtime),
            "message": item["message"],
            "extraction_status": extraction_status(item["sha256"]),
            # The raw extracted-tree path, not resolve_filepath()'s
            # possibly-sqlite-resolved "filename" above - resolve_source_chain
            # needs the real "extracted/files/<sha>/..." structure to find
            # this file's own immediate parent, which resolve_filepath's
            # sqlite lookup (use_sqlite=True) can override to an unrelated
            # original name.
            "source_chain": resolve_source_chain(str(item["fname"]), item["sha256"]),
        }
        lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode("utf-8")


def bulk_request(items, num_retries=5):
    """POSTs one _bulk request, retrying the whole batch on connection or
    transient failures - the same backoff request_retry used before.
    Returns the response's "items" list, in the same order as the request
    (guaranteed by the _bulk API), or None if every retry failed.
    """
    url = index_url() + "/_bulk?pipeline=cbor-attachment"
    body = build_bulk_body(items)
    for _ in range(num_retries):
        try:
            response = requests.post(url, data=body, headers=headers, auth=elastic_auth(), timeout=120)
            if response.status_code in success_list:
                return response.json()["items"]
            time.sleep(15)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(60)
        except requests.exceptions.RequestException as error:
            # Anything else requests can raise (SSL errors, malformed URLs,
            # too many redirects, ...). Retry rather than giving up outright.
            print("ERROR: Bulk request to Elastic failed:", error, flush=True)
            time.sleep(15)
    return None


def process_batch(items):
    """Sends one batch and applies the crash-safe marker-only-after-confirmed
    rule per document. Returns (status, sha256, error) tuples, one per item,
    the same shape prepare_file's callers already expect.
    """
    results = []
    responses = bulk_request(items)
    if responses is None:
        return [
            (FAILED, item["sha256"], f"Error sending file to Elastic (bulk request failed): {item['fname']}")
            for item in items
        ]

    retry_items = []
    for item, item_response in zip(items, responses):
        action_result = item_response["index"]
        status = action_result["status"]
        if status in success_list:
            create_hash_link(item["sha256"], item["fname"])
            results.append((INDEXED, item["sha256"], None))
        elif status == 400:
            # Tika could not parse it. Retry without content so the file is
            # still findable by name/hash, with the parser error kept in
            # "message".
            retry_items.append({**item, "content": b"", "message": json.dumps(action_result.get("error", {}))})
        else:
            results.append(
                (FAILED, item["sha256"], f"Error sending file to Elastic (status {status}): {item['fname']}")
            )

    if retry_items:
        retry_responses = bulk_request(retry_items)
        if retry_responses is None:
            results.extend(
                (FAILED, item["sha256"], f"Error sending file to Elastic (bulk retry failed): {item['fname']}")
                for item in retry_items
            )
        else:
            for item, item_response in zip(retry_items, retry_responses):
                action_result = item_response["index"]
                status = action_result["status"]
                if status in success_list:
                    create_hash_link(item["sha256"], item["fname"])
                    results.append((INDEXED, item["sha256"], None))
                else:
                    results.append(
                        (
                            FAILED,
                            item["sha256"],
                            f"Error sending file to Elastic (second try, status {status}): {item['fname']}",
                        )
                    )

    return results


def build_rows_bulk_body(items):
    """Builds the newline-delimited JSON body for one CSV-rows _bulk
    request. Unlike build_bulk_body: no cbor-attachment pipeline (rows are
    already-parsed JSON, not raw file bytes needing Tika) and no base64.
    One item here is one file, but it expands to one bulk action pair per
    row within it - _id is "<sha256>:<row_number>", deterministic given
    deterministic parsing, so re-sending the same file's rows (e.g. a
    retried batch) is a safe, idempotent overwrite rather than a duplicate.
    """
    lines = []
    for item in items:
        source_filename = resolve_filepath(str(item["fname"]), item["sha256"])
        for row_number, row in enumerate(item["rows"], start=1):
            lines.append(json.dumps({"index": {"_id": f"{item['sha256']}:{row_number}"}}))
            doc = {
                "source_sha256": item["sha256"],
                "source_filename": source_filename,
                "row_number": row_number,
                "row": row,
            }
            lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode("utf-8")


def rows_bulk_request(items, num_retries=5):
    """Same retry/backoff shape as bulk_request, against ROW_INDEX instead
    of INDEX and with no ingest pipeline - see build_rows_bulk_body.
    """
    url = f"http://{elastic_host}:9200/{ROW_INDEX}/_bulk"
    body = build_rows_bulk_body(items)
    for _ in range(num_retries):
        try:
            response = requests.post(url, data=body, headers=headers, auth=elastic_auth(), timeout=120)
            if response.status_code in success_list:
                return response.json()["items"]
            time.sleep(15)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(60)
        except requests.exceptions.RequestException as error:
            print("ERROR: Bulk request to Elastic (CSV rows) failed:", error, flush=True)
            time.sleep(15)
    return None


def process_rows_batch(items):
    """Sends one CSV-rows batch and applies the same crash-safe
    marker-only-after-confirmed rule process_batch uses, keyed per file
    (one rows marker covers all of that file's rows, not one per row).
    Returns (status, sha256, error) tuples, one per file - kept separate
    from process_batch's own results (see process_files): a CSV's row
    breakdown is additive enrichment, not a substitute for its normal
    full-text indexing, so a row-indexing failure here must never fail
    the file's own blob-indexing outcome or the run's overall exit code.
    """
    results = []
    responses = rows_bulk_request(items)
    if responses is None:
        return [
            (FAILED, item["sha256"], f"Error sending CSV rows to Elastic (bulk request failed): {item['fname']}")
            for item in items
        ]

    offset = 0
    for item in items:
        row_count = len(item["rows"])
        item_responses = responses[offset : offset + row_count]
        offset += row_count
        failed = [r for r in item_responses if r["index"]["status"] not in success_list]
        if not failed:
            create_row_hash_link(item["sha256"], item["fname"])
            results.append((INDEXED, item["sha256"], None))
        else:
            results.append(
                (
                    FAILED,
                    item["sha256"],
                    f"Error sending {len(failed)}/{row_count} CSV row(s) to Elastic: {item['fname']}",
                )
            )
    return results


def log_results(results, row_results):
    """Appends one line per interesting outcome to logs/ingest.log, matching
    unpack/start.sh's logs/unpack.log: never truncated, timestamped, and
    only outcomes worth knowing about later - not every already-indexed file
    on every run, the same way unpack's log() is never called for content
    that was already there. Row-indexing outcomes get their own tags
    (ROWS_INDEXED/ROWS_FAILED) so they're distinguishable from blob-level
    ones at a glance - the two are independent outcomes for the same file.
    """
    try:
        with open("/logs/ingest.log", "a", encoding="utf-8") as f:
            now = datetime.now(UTC).isoformat()
            for status, sha256, error in results:
                if status == INDEXED:
                    f.write(f"{now} [INDEXED] sha256={sha256}\n")
                elif status == FAILED:
                    f.write(f"{now} [FAILED] {error}\n")
            for status, sha256, error in row_results:
                if status == INDEXED:
                    f.write(f"{now} [ROWS_INDEXED] sha256={sha256}\n")
                elif status == FAILED:
                    f.write(f"{now} [ROWS_FAILED] {error}\n")
    except OSError as error:
        print("ERROR: Could not write to /logs/ingest.log:", error, flush=True)


def print_summary(results, row_results, directory):
    """Account for every file that was looked at.

    Far fewer documents than files is normal and expected - identical files
    share one document, because the document id is the sha256 - but without
    this breakdown the difference looks like data went missing.

    row_results (item 21's CSV-as-rows piece) is accounted for separately
    and deliberately does not affect the returned failure count: a row's
    structured breakdown is additive enrichment on top of a file's normal
    full-text indexing, not a substitute for it, so a row-indexing hiccup
    must never fail the run the way a file failing to index at all does.
    """
    statuses = [status for status, _, _ in results]
    errors = [error for _, _, error in results if error]
    # Files that are represented by a document, whether this run wrote it or not.
    covered = {sha256 for status, sha256, _ in results if status in (INDEXED, PRESENT)}
    counted = statuses.count(INDEXED) + statuses.count(PRESENT)

    row_statuses = [status for status, _, _ in row_results]
    row_errors = [error for _, _, error in row_results if error]

    log_results(results, row_results)
    for error in errors:
        print(error)
    for error in row_errors:
        print(error)

    print()
    print("Ingest summary")
    print("--------------")
    print(f"  files looked at:      {len(results)}")
    print(f"  unique files:         {len(covered)}")
    print(f"  duplicate copies:     {counted - len(covered)}")
    print(f"  indexed this run:     {statuses.count(INDEXED)}")
    print(f"  already indexed:      {statuses.count(PRESENT)}")
    print(f"  failed:               {statuses.count(FAILED)}")
    if row_statuses:
        print(f"  csv files w/ rows indexed: {row_statuses.count(INDEXED)}")
        if row_statuses.count(FAILED):
            print(f"  csv files w/ row errors:   {row_statuses.count(FAILED)}")

    documents = elastic_document_count()
    if documents is None:
        print("  in Elasticsearch:     could not be read")
    else:
        print(f"  in Elasticsearch:     {documents}")
        if documents < len(covered):
            print(f"  WARNING: {len(covered) - documents} unique file(s) have no document in Elasticsearch.")
        elif documents > len(covered):
            print(f"  Note: {documents - len(covered)} document(s) come from files no longer in {directory}.")
    print(flush=True)

    index_run_summary(
        {
            "@timestamp": datetime.now(UTC).isoformat(),
            "files_looked_at": len(results),
            "unique_files": len(covered),
            "duplicate_copies": counted - len(covered),
            "indexed_this_run": statuses.count(INDEXED),
            "already_indexed": statuses.count(PRESENT),
            "failed": statuses.count(FAILED),
            "csv_files_with_rows_indexed": row_statuses.count(INDEXED),
            "csv_files_with_row_errors": row_statuses.count(FAILED),
            "elasticsearch_document_count": documents,
        }
    )

    return statuses.count(FAILED)


def process_files(directory: Path):
    """Prepares files in parallel (number of CPUs), sends them to
    Elasticsearch in bulk batches, and returns the number that failed.

    Deduplicates content first seen this run before it is ever queued to
    send: ProcessPoolExecutor.map() yields results in input order, not
    completion order, so this loop - a single Python thread consuming that
    generator - sees every file in a fixed, deterministic sequence even
    though hashing/reading happens in parallel across worker processes. The
    first file with a given sha256 is queued normally; every later file with
    the same sha256 in this run is deferred instead of queued, so the same
    content is never uploaded and Tika-parsed twice just because two workers
    both happened to hash a never-before-seen duplicate before either's
    upload confirmed. No locking needed - the dedup decision itself never
    leaves this one serial loop.
    """
    results = []
    row_results = []
    batch = []
    batch_bytes = 0
    rows_batch = []
    rows_batch_count = 0
    seen_this_run = set()
    pending_duplicates = []

    def flush():
        nonlocal batch, batch_bytes
        if batch:
            results.extend(process_batch(batch))
            batch = []
            batch_bytes = 0

    def flush_rows():
        nonlocal rows_batch, rows_batch_count
        if rows_batch:
            row_results.extend(process_rows_batch(rows_batch))
            rows_batch = []
            rows_batch_count = 0

    with ProcessPoolExecutor() as executor:
        files = list(get_files(directory))
        for prepared in tqdm(
            executor.map(prepare_file, files), total=len(files), desc="Processing files", unit="files"
        ):
            status = prepared["status"]
            if status in ("ready", "ready_rows_only"):
                sha256 = prepared["sha256"]
                if sha256 in seen_this_run:
                    pending_duplicates.append(prepared)
                    continue
                seen_this_run.add(sha256)
                if status == "ready":
                    batch.append(prepared)
                    batch_bytes += len(prepared["content"])
                    if len(batch) >= BULK_MAX_DOCS or batch_bytes >= BULK_MAX_BYTES:
                        flush()
                else:
                    # "ready_rows_only": the blob is already confirmed
                    # indexed (that's what makes it rows-only), so unlike
                    # "ready" this doesn't wait on a bulk response to know
                    # its outcome - recorded here directly, or every such
                    # file would silently vanish from "files looked at"/
                    # "unique files" instead of counting as present.
                    results.append((PRESENT, sha256, None))
                if prepared.get("rows"):
                    rows_batch.append(prepared)
                    rows_batch_count += len(prepared["rows"])
                    if rows_batch_count >= BULK_MAX_DOCS:
                        flush_rows()
            else:
                results.append((status, prepared.get("sha256"), prepared.get("error")))
        flush()
        flush_rows()

    # Every representative's outcome is now known (create_hash_link only
    # runs on confirmed success), so resolve every deferred duplicate
    # against it without a second upload - two files with the same sha256
    # are byte-identical, so a retry could never produce a different result.
    # Row outcomes need no equivalent resolution here: row_hash_link_exists
    # is keyed by the same content sha256, so a duplicate's rows are
    # already covered once its representative's rows succeed.
    for item in pending_duplicates:
        if hash_link_exists(item["sha256"]):
            results.append((PRESENT, item["sha256"], None))
        else:
            results.append(
                (
                    FAILED,
                    item["sha256"],
                    f"Error sending file to Elastic (same content failed this run): {item['fname']}",
                )
            )

    return print_summary(results, row_results, directory)


cfg = read_configuration("./deis.cfg")
max_size = int(cfg.get("ingest", "max_size"))
use_sqlite = cfg.getboolean("ingest", "use_sqlite")
# fallback=True/50000, not a plain .getboolean()/.getint(): deis.cfg is
# gitignored and 'deis init' leaves an existing one alone, so a key added
# after someone's initial setup never appears in their config - reading
# that as a hard error (or silently as False/0) would either crash or
# disable a feature documented as on by default. Same reasoning as
# unpack/start.sh's config_true_default().
csv_rows_enabled = cfg.getboolean("ingest", "csv_rows", fallback=True)
csv_max_rows = cfg.getint("ingest", "csv_max_rows", fallback=50000)
xlsx_rows_enabled = cfg.getboolean("ingest", "xlsx_rows", fallback=True)
xlsx_max_rows = cfg.getint("ingest", "xlsx_max_rows", fallback=50000)
still_encrypted = load_sha256_set("status/still_encrypted.txt")
still_corrupt = load_sha256_set("status/still_corrupt.txt")
still_unsafe = load_sha256_set("status/still_unsafe.txt")
still_multivolume = load_sha256_set("status/still_multivolume.txt")
decrypted = load_sha256_set("status/decrypted.txt")
lineage_by_sha = load_lineage("status/lineage.jsonl")
source_urls_by_sha = load_source_urls("status/source_urls.jsonl")
if use_sqlite:
    con = sqlite3.connect("db/file_hashes.db")
try:
    password = os.environ["ELASTIC_PASSWORD"]
except (AttributeError, KeyError):
    password = str(cfg.get("elastic", "password"))

if Path("/.dockerenv").is_file():
    elastic_host = "elasticsearch"
else:
    elastic_host = "127.0.0.1"


if __name__ == "__main__":
    failed = process_files(Path(cfg.get("ingest", "files")))
    if failed:
        # Files that failed have no sha256 symlink, so they are picked up again
        # on the next run - but only if this run isn't marked as done.
        print("Ingest incomplete. Re-run with 'docker compose restart ingest' to retry the failed files.")
        sys.exit(1)
    Path("./status/ingest_done").touch()
    print("Ingest done.")
