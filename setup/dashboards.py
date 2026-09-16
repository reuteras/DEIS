#!/usr/bin/env python3
"""Generates DEIS's Kibana dashboards (docs/IMPROVEMENTS.md item 55) into
setup/export.ndjson - the file `deis run --only setup` imports.

    uv run python3 setup/dashboards.py          # rewrite export.ndjson
    uv run python3 setup/dashboards.py --check  # exit 1 if it would change

The original objects in export.ndjson (the "Leaked data" dashboard, the
saved searches, the data views, the photo map) were exported from Kibana
by hand and are kept verbatim. Everything this script owns has a stable
id derived from its title (uuid5), so re-running replaces exactly its own
objects and nothing else - and a dashboard edited by hand in Kibana keeps
its edits until the next regeneration, which is the price of having them
as code: the alternative, thousands of lines of hand-maintained Lens JSON
per dashboard, is what made adding a panel a chore before.

Panels are Lens "by value" objects in the 8.9-era shape the existing
dashboard already uses (Kibana migrates them on import, confirmed live
against 9.5). Only a handful of visualization types are needed: metric
tiles, horizontal bars, donuts, tables, tag clouds, date histograms, and
Discover saved-search panels. Dashboard controls (the "Subject lookup"
dashboard's dropdowns) use the options-list control group format.
"""

import json
import sys
import uuid
from pathlib import Path

EXPORT = Path(__file__).resolve().parent / "export.ndjson"
NAMESPACE = uuid.UUID("6f1b4a3e-2d5c-4b8e-9f0a-7c3d2e1f0a9b")
LEAKDATA_VIEW = "1ea47c2d-0d79-44d2-b847-96d806232ca7"
RUNS_VIEW = "0d514868-d09b-487d-87d6-0d6d1ef786c6"
TIME_FROM = "1969-12-31T23:00:00.000Z"
PANEL_VERSION = "8.9.1"
# Existing hand-exported saved searches referenced from the new dashboards.
SEARCH_ALL_FILES = "28681d75-85ef-49be-9cae-aa9294243190"
SEARCH_PII = "f2a7e58c-b0f2-4e47-ab85-45608f0ab9a5"
SEARCH_ENTITIES = "9a568b20-44fc-45a2-b3c2-d77045378687"
SEARCH_INGEST_RUNS = "fa0b1d1b-b3d9-4499-bcd5-9dbd2582f545"
SEARCH_NEEDS_ATTENTION = "a2631d64-3683-44a9-ab75-11c5f547fbe1"
SEARCH_DUPLICATES = "1c7bfbf6-e973-491b-868a-28a9122efa85"
SEARCH_CSV_ROWS = "d1f57780-a015-48a5-8fc2-ec95eedaf1c2"


def oid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


# ---------------------------------------------------------------------------
# Lens column builders


def count_col(label: str = "Documents", kql: str | None = None) -> dict:
    col = {
        "label": label,
        "dataType": "number",
        "operationType": "count",
        "isBucketed": False,
        "scale": "ratio",
        "sourceField": "___records___",
        "params": {"emptyAsNull": False},
    }
    if kql:
        col["filter"] = {"query": kql, "language": "kuery"}
    return col


def metric_col(operation: str, field: str, label: str, kql: str | None = None) -> dict:
    col = {
        "label": label,
        "dataType": "number",
        "operationType": operation,
        "sourceField": field,
        "isBucketed": False,
        "scale": "ratio",
        "params": {"emptyAsNull": False},
    }
    if kql:
        col["filter"] = {"query": kql, "language": "kuery"}
    return col


def terms_col(field: str, label: str, order_by: str, size: int = 10, missing: bool = False) -> dict:
    return {
        "label": label,
        "dataType": "string",
        "operationType": "terms",
        "scale": "ordinal",
        "sourceField": field,
        "isBucketed": True,
        "params": {
            "size": size,
            "orderBy": {"type": "column", "columnId": order_by},
            "orderDirection": "desc",
            "otherBucket": False,
            "missingBucket": missing,
            "parentFormat": {"id": "terms"},
            "include": [],
            "exclude": [],
            "includeIsRegex": False,
            "excludeIsRegex": False,
        },
    }


def date_col(field: str, label: str, interval: str = "1y") -> dict:
    return {
        "label": label,
        "dataType": "date",
        "operationType": "date_histogram",
        "sourceField": field,
        "isBucketed": True,
        "scale": "interval",
        "params": {"interval": interval, "includeEmptyRows": False, "dropPartials": False},
    }


# ---------------------------------------------------------------------------
# Panel builders. Each returns (panel dict, references list); ids are
# derived from the dashboard and panel title so they are stable.


def _lens_panel(
    dashboard: str,
    title: str,
    vis_type: str,
    vis_state: dict,
    columns: dict,
    column_order: list[str],
    layer_id: str,
    grid: tuple[int, int, int, int],
    kql: str = "",
    view: str = LEAKDATA_VIEW,
) -> tuple[dict, list[dict]]:
    panel_index = oid(f"{dashboard}/{title}/panel")
    x, y, w, h = grid
    panel = {
        "version": PANEL_VERSION,
        "type": "lens",
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_index},
        "panelIndex": panel_index,
        "embeddableConfig": {
            "attributes": {
                "title": title,
                "description": "",
                "visualizationType": vis_type,
                "type": "lens",
                "references": [
                    {"id": view, "name": f"indexpattern-datasource-layer-{layer_id}", "type": "index-pattern"}
                ],
                "state": {
                    "visualization": vis_state,
                    "query": {"query": kql, "language": "kuery"},
                    "filters": [],
                    "datasourceStates": {
                        "formBased": {
                            "layers": {
                                layer_id: {
                                    "columns": columns,
                                    "columnOrder": column_order,
                                    "incompleteColumns": {},
                                    "sampling": 1,
                                }
                            }
                        },
                        "indexpattern": {"layers": {}},
                        "textBased": {"layers": {}},
                    },
                    "internalReferences": [],
                    "adHocDataViews": {},
                },
            },
            "enhancements": {},
            "hidePanelTitles": False,
        },
        "title": title,
    }
    refs = [
        {
            "id": view,
            "name": f"{panel_index}:indexpattern-datasource-layer-{layer_id}",
            "type": "index-pattern",
        }
    ]
    return panel, refs


def metric_panel(dashboard, title, grid, *, kql_filter=None, operation="count", field=None, view=LEAKDATA_VIEW):
    layer = oid(f"{dashboard}/{title}/layer")
    col = oid(f"{dashboard}/{title}/metric")
    column = count_col(title, kql_filter) if operation == "count" else metric_col(operation, field, title, kql_filter)
    vis = {"layerId": layer, "layerType": "data", "metricAccessor": col}
    return _lens_panel(dashboard, title, "lnsMetric", vis, {col: column}, [col], layer, grid, view=view)


def bar_panel(dashboard, title, field, grid, *, size=15, kql="", missing=False, metric=None, view=LEAKDATA_VIEW):
    layer = oid(f"{dashboard}/{title}/layer")
    x_col, m_col = oid(f"{dashboard}/{title}/x"), oid(f"{dashboard}/{title}/m")
    columns = {
        x_col: terms_col(field, f"Top {size} {field}", m_col, size, missing),
        m_col: metric or count_col(),
    }
    vis = {
        "legend": {"isVisible": False, "position": "right"},
        "valueLabels": "hide",
        "fittingFunction": "None",
        "axisTitlesVisibilitySettings": {"x": False, "yLeft": False, "yRight": True},
        "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
        "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "preferredSeriesType": "bar_horizontal",
        "layers": [
            {
                "layerId": layer,
                "accessors": [m_col],
                "position": "top",
                "seriesType": "bar_horizontal",
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": x_col,
            }
        ],
    }
    return _lens_panel(dashboard, title, "lnsXY", vis, columns, [x_col, m_col], layer, grid, kql, view)


def timeline_panel(dashboard, title, field, grid, *, interval="1y", kql="", view=LEAKDATA_VIEW, breakdown=None):
    layer = oid(f"{dashboard}/{title}/layer")
    x_col, m_col = oid(f"{dashboard}/{title}/x"), oid(f"{dashboard}/{title}/m")
    columns = {x_col: date_col(field, field, interval), m_col: count_col()}
    order = [x_col, m_col]
    layer_state = {
        "layerId": layer,
        "accessors": [m_col],
        "position": "top",
        "seriesType": "bar_stacked",
        "showGridlines": False,
        "layerType": "data",
        "xAccessor": x_col,
    }
    if breakdown:
        b_col = oid(f"{dashboard}/{title}/b")
        columns[b_col] = terms_col(breakdown, breakdown, m_col, 5)
        order = [x_col, b_col, m_col]
        layer_state["splitAccessor"] = b_col
    vis = {
        "legend": {"isVisible": bool(breakdown), "position": "right"},
        "valueLabels": "hide",
        "fittingFunction": "None",
        "axisTitlesVisibilitySettings": {"x": False, "yLeft": False, "yRight": True},
        "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
        "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "preferredSeriesType": "bar_stacked",
        "layers": [layer_state],
    }
    return _lens_panel(dashboard, title, "lnsXY", vis, columns, order, layer, grid, kql, view)


def lines_panel(dashboard, title, fields: dict[str, str], grid, *, view=RUNS_VIEW, interval="auto"):
    """Several max() metrics over a time axis - the ingest-run history."""
    layer = oid(f"{dashboard}/{title}/layer")
    x_col = oid(f"{dashboard}/{title}/x")
    columns = {x_col: date_col("@timestamp", "Run", interval)}
    accessors = []
    for field, label in fields.items():
        col = oid(f"{dashboard}/{title}/{field}")
        columns[col] = metric_col("max", field, label)
        accessors.append(col)
    vis = {
        "legend": {"isVisible": True, "position": "right"},
        "valueLabels": "hide",
        "fittingFunction": "Linear",
        "axisTitlesVisibilitySettings": {"x": False, "yLeft": False, "yRight": True},
        "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
        "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "preferredSeriesType": "line",
        "layers": [
            {
                "layerId": layer,
                "accessors": accessors,
                "position": "top",
                "seriesType": "line",
                "showGridlines": False,
                "layerType": "data",
                "xAccessor": x_col,
            }
        ],
    }
    return _lens_panel(dashboard, title, "lnsXY", vis, columns, [x_col, *accessors], layer, grid, "", view)


def donut_panel(dashboard, title, field, grid, *, size=8, kql="", missing=False):
    layer = oid(f"{dashboard}/{title}/layer")
    g_col, m_col = oid(f"{dashboard}/{title}/g"), oid(f"{dashboard}/{title}/m")
    columns = {g_col: terms_col(field, field, m_col, size, missing), m_col: count_col()}
    vis = {
        "shape": "donut",
        "layers": [
            {
                "layerId": layer,
                "primaryGroups": [g_col],
                "metrics": [m_col],
                "numberDisplay": "percent",
                "categoryDisplay": "default",
                "legendDisplay": "default",
                "nestedLegend": False,
                "layerType": "data",
            }
        ],
    }
    return _lens_panel(dashboard, title, "lnsPie", vis, columns, [g_col, m_col], layer, grid, kql)


def table_panel(dashboard, title, fields: list[str], grid, *, size=20, kql="", metric=None, missing=False):
    """A terms table over one or more keyword fields plus a metric column."""
    layer = oid(f"{dashboard}/{title}/layer")
    m_col = oid(f"{dashboard}/{title}/m")
    columns = {}
    order = []
    for field in fields:
        col = oid(f"{dashboard}/{title}/{field}")
        columns[col] = terms_col(field, field, m_col, size, missing)
        order.append(col)
    columns[m_col] = metric or count_col()
    order.append(m_col)
    vis = {"layerId": layer, "layerType": "data", "columns": [{"columnId": c} for c in order]}
    return _lens_panel(dashboard, title, "lnsDatatable", vis, columns, order, layer, grid, kql)


def tagcloud_panel(dashboard, title, field, grid, *, size=50, kql=""):
    layer = oid(f"{dashboard}/{title}/layer")
    t_col, m_col = oid(f"{dashboard}/{title}/t"), oid(f"{dashboard}/{title}/m")
    columns = {t_col: terms_col(field, field, m_col, size), m_col: count_col()}
    vis = {
        "layerId": layer,
        "layerType": "data",
        "tagAccessor": t_col,
        "valueAccessor": m_col,
        "maxFontSize": 60,
        "minFontSize": 14,
        "orientation": "single",
        "showLabel": False,
    }
    return _lens_panel(dashboard, title, "lnsTagcloud", vis, columns, [t_col, m_col], layer, grid, kql)


def search_panel(dashboard, title, search_id, grid) -> tuple[dict, list[dict]]:
    panel_index = oid(f"{dashboard}/{title}/panel")
    x, y, w, h = grid
    panel = {
        "version": PANEL_VERSION,
        "type": "search",
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_index},
        "panelIndex": panel_index,
        "embeddableConfig": {"hidePanelTitles": False, "enhancements": {}},
        "title": title,
        "panelRefName": f"panel_{panel_index}",
    }
    return panel, [{"id": search_id, "name": f"{panel_index}:panel_{panel_index}", "type": "search"}]


# ---------------------------------------------------------------------------
# Saved objects


def saved_search(title: str, description: str, kql: str, columns: list[str], sort_field: str = "timestamp") -> dict:
    return {
        "attributes": {
            "columns": columns,
            "description": description,
            "grid": {"columns": {"filename": {"width": 360}, "sha256": {"width": 160}}},
            "hideChart": False,
            "isTextBasedQuery": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {
                        "query": {"query": kql, "language": "kuery"},
                        "filter": [],
                        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                    }
                )
            },
            "refreshInterval": {"pause": True, "value": 60000},
            "sort": [[sort_field, "desc"]],
            "timeRange": {"from": TIME_FROM, "to": "now"},
            "timeRestore": True,
            "title": title,
            "usesAdHocDataView": False,
        },
        "coreMigrationVersion": "8.8.0",
        "id": oid(f"search/{title}"),
        "managed": False,
        "references": [
            {"id": LEAKDATA_VIEW, "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}
        ],
        "type": "search",
        "typeMigrationVersion": "8.0.0",
    }


def control(dashboard: str, field: str, title: str, order: int) -> tuple[str, dict, dict]:
    control_id = oid(f"{dashboard}/control/{field}")
    panel = {
        "type": "optionsListControl",
        "order": order,
        "grow": True,
        "width": "medium",
        "explicitInput": {
            "id": control_id,
            "fieldName": field,
            "title": title,
            "searchTechnique": "prefix",
            "selectedOptions": [],
            "enhancements": {},
        },
    }
    ref = {"id": LEAKDATA_VIEW, "name": f"controlGroup_{control_id}:optionsListDataView", "type": "index-pattern"}
    return control_id, panel, ref


def dashboard(title: str, description: str, panels: list[tuple[dict, list[dict]]], controls=()) -> dict:
    references = []
    panel_dicts = []
    for panel, refs in panels:
        # Panel ids derive from the title, so two panels with one title
        # would silently collapse into one - refuse instead.
        if any(panel["panelIndex"] == existing["panelIndex"] for existing in panel_dicts):
            raise ValueError(f"{title}: duplicate panel title {panel['title']!r}")
        panel_dicts.append(panel)
        references.extend(refs)
    attributes = {
        "description": description,
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})
        },
        "optionsJSON": json.dumps(
            {
                "useMargins": True,
                "syncColors": True,
                "syncCursor": True,
                "syncTooltips": False,
                "hidePanelTitles": False,
            }
        ),
        "panelsJSON": json.dumps(panel_dicts),
        "refreshInterval": {"pause": True, "value": 60000},
        "timeFrom": TIME_FROM,
        "timeRestore": True,
        "timeTo": "now",
        "title": title,
        "version": 1,
    }
    if controls:
        control_panels = {}
        for order, (field, label) in enumerate(controls):
            control_id, panel, ref = control(title, field, label, order)
            control_panels[control_id] = panel
            references.append(ref)
        attributes["controlGroupInput"] = {
            "controlStyle": "oneLine",
            "chainingSystem": "HIERARCHICAL",
            "showApplySelections": False,
            "ignoreParentSettingsJSON": json.dumps(
                {"ignoreFilters": False, "ignoreQuery": False, "ignoreTimerange": False, "ignoreValidations": False}
            ),
            "panelsJSON": json.dumps(control_panels),
        }
    return {
        "attributes": attributes,
        "coreMigrationVersion": "8.8.0",
        "id": oid(f"dashboard/{title}"),
        "managed": False,
        "references": references,
        "type": "dashboard",
        "typeMigrationVersion": "8.9.0",
    }


# ---------------------------------------------------------------------------
# The dashboards themselves. Grid is 48 columns wide; heights are in rows.


def build_objects() -> list[dict]:
    objects: list[dict] = []

    # Saved searches the dashboards embed (Discover tables with the right columns).
    searches = {
        "credentials": saved_search(
            "Documents with credentials",
            "Documents where 'deis secret-scan' found a private key, API/cloud token, JWT, credential-bearing URL, "
            "or a password assignment. Leads, not proof - password_assignments in particular are unvalidated.",
            "secrets.has_secrets: true",
            [
                "filename",
                "sha256",
                "secrets.private_keys",
                "secrets.aws_access_keys",
                "secrets.credential_urls",
                "secrets.password_assignments",
                "sensitive_class",
            ],
        ),
        "artifacts": saved_search(
            "Documents with infrastructure artifacts",
            "Documents where 'deis secret-scan' found IP addresses, UNC paths, usernames from home-directory paths, "
            "hostnames, .onion addresses or checksum-valid Bitcoin addresses.",
            "artifacts.has_artifacts: true",
            [
                "filename",
                "sha256",
                "artifacts.usernames",
                "artifacts.unc_paths",
                "artifacts.private_ipv4_addresses",
                "artifacts.domains",
                "artifacts.onion_addresses",
                "artifacts.bitcoin_addresses",
            ],
        ),
        "sensitive": saved_search(
            "Sensitive files",
            "Files ingest classified by magic number, name or extension as credential stores, key material, secrets-bearing "
            "config, remote-access configs, executables, disk images, databases or ransom notes (sensitive_class).",
            "sensitive_class: *",
            ["filename", "sha256", "sensitive_class", "extension", "attachment.content_type", "file_size"],
        ),
        "ransom": saved_search(
            "Ransom notes",
            "Files whose name or opening text looks like a ransomware note - the group's own words, payment address and "
            "contact details usually live here.",
            "sensitive_class: ransom_note",
            ["filename", "sha256", "artifacts.onion_addresses", "artifacts.bitcoin_addresses", "pii.emails"],
        ),
        "email": saved_search(
            "Email messages",
            "RFC 822 messages (.eml, mbox messages, readpst output) with their parsed sender, recipients, subject, date "
            "and attachment names (item 50).",
            "email.has_email: true",
            [
                "email.date",
                "email.from_addresses",
                "email.recipients",
                "email.subject",
                "email.attachment_names",
                "filename",
                "sha256",
            ],
            sort_field="email.date",
        ),
        "truncated": saved_search(
            "Truncated documents",
            "Documents whose Tika-extracted text hit their indexed_chars cap - the rest of the document is NOT "
            "searchable. Raise indexed_chars/indexed_chars_pdf in deis.cfg and re-ingest if these matter.",
            "content_truncated: true",
            ["filename", "sha256", "extension", "file_size", "attachment.content_length", "indexed_chars_limit"],
        ),
        "mismatch": saved_search(
            "Extension does not match content",
            "Files whose extension disagrees with what Tika detected the content to be - renamed, disguised, or just "
            "mislabelled.",
            "mime_mismatch: true",
            ["filename", "sha256", "extension", "attachment.content_type", "sensitive_class", "file_size"],
        ),
        "photos": saved_search(
            "Photos with camera metadata",
            "JPEGs whose EXIF data named the camera/phone, the software that wrote the file, or when the shutter fired "
            "(deis geo-scan).",
            "exif.has_exif: true",
            ["filename", "sha256", "exif.make", "exif.model", "exif.software", "exif.datetime_original", "location"],
        ),
    }
    objects.extend(searches.values())

    # --- Subject lookup ----------------------------------------------------
    d = "Subject lookup"
    objects.append(
        dashboard(
            d,
            "Start here for 'is this person in the dump': pick a personnummer, email, name or username in the "
            "controls at the top and every panel narrows to the documents that mention them.",
            [
                metric_panel(d, "Matching documents", (0, 0, 12, 6)),
                metric_panel(d, "With personal identifiers", (12, 0, 12, 6), kql_filter="pii.has_pii: true"),
                metric_panel(d, "Email messages", (24, 0, 12, 6), kql_filter="email.has_email: true"),
                metric_panel(d, "Structured rows (files)", (36, 0, 12, 6), kql_filter="row_count > 0"),
                bar_panel(d, "Folders", "top_folder", (0, 6, 16, 14)),
                donut_panel(d, "File types", "extension", (16, 6, 16, 14)),
                timeline_panel(d, "Documents by year", "attachment.date", (32, 6, 16, 14)),
                tagcloud_panel(d, "Co-mentioned people", "entities.persons", (0, 20, 16, 14)),
                tagcloud_panel(d, "Co-mentioned organizations", "entities.organizations", (16, 20, 16, 14)),
                table_panel(d, "Personnummer in these documents", ["pii.personnummer_normalized"], (32, 20, 16, 14)),
                table_panel(d, "Email addresses in these documents", ["pii.emails"], (0, 34, 16, 14)),
                table_panel(d, "Download sources", ["source_chain.url"], (16, 34, 16, 14)),
                table_panel(d, "Mail senders", ["email.from_addresses"], (32, 34, 16, 14)),
                search_panel(d, "Documents", SEARCH_ALL_FILES, (0, 48, 48, 18)),
                search_panel(d, "Personal identifiers found", SEARCH_PII, (0, 66, 48, 14)),
            ],
            controls=(
                ("pii.personnummer_normalized", "Personnummer"),
                ("pii.emails", "Email address"),
                ("entities.persons", "Person"),
                ("artifacts.usernames", "Username"),
                ("pii.organisationsnummer", "Organisationsnummer"),
            ),
        )
    )

    # --- Personal data -------------------------------------------------------
    d = "Personal data"
    pii_types = [
        ("Personnummer", "pii.personnummer"),
        ("Organisationsnummer", "pii.organisationsnummer"),
        ("Emails", "pii.emails"),
        ("Phone numbers", "pii.phone_numbers"),
        ("IBANs", "pii.ibans"),
        ("Card numbers", "pii.card_numbers"),
        ("Bankgiro/plusgiro", "pii.bankgiro or pii.plusgiro"),
        ("Fødselsnummer (NO)", "pii.fodselsnummer"),
        ("Hetu (FI)", "pii.hetu"),
        ("CPR (DK, unvalidated)", "pii.cpr"),
    ]
    tiles = [
        metric_panel(
            d,
            f"Docs with {label.lower()}",
            (i % 6 * 8, 6 + i // 6 * 6, 8, 6),
            kql_filter=f"{field}: *"
            if " or " not in field
            else f"{field.split(' or ')[0]}: * or {field.split(' or ')[1]}: *",
        )
        for i, (label, field) in enumerate(pii_types)
    ]
    objects.append(
        dashboard(
            d,
            "What 'deis pii-scan' found: checksum-validated personal and financial identifiers, who appears most, "
            "and where in the dump they sit.",
            [
                metric_panel(d, "Documents with personal identifiers", (0, 0, 16, 6), kql_filter="pii.has_pii: true"),
                metric_panel(
                    d,
                    "Distinct personnummer",
                    (16, 0, 16, 6),
                    operation="unique_count",
                    field="pii.personnummer_normalized",
                ),
                metric_panel(
                    d,
                    "Recovered by password-cracking, with PII",
                    (32, 0, 16, 6),
                    kql_filter="pii.has_pii: true and extraction_status: decrypted",
                ),
                *tiles,
                table_panel(d, "Most-mentioned personnummer", ["pii.personnummer_normalized"], (0, 18, 16, 16)),
                bar_panel(d, "Email domains", "pii_email_domain", (16, 18, 16, 16), size=20),
                table_panel(d, "Most-mentioned organisationsnummer", ["pii.organisationsnummer"], (32, 18, 16, 16)),
                bar_panel(
                    d, "Folders with personal identifiers", "top_folder", (0, 34, 16, 14), kql="pii.has_pii: true"
                ),
                donut_panel(
                    d, "File types with personal identifiers", "extension", (16, 34, 16, 14), kql="pii.has_pii: true"
                ),
                bar_panel(
                    d,
                    "Languages of documents with identifiers",
                    "language",
                    (32, 34, 16, 14),
                    size=5,
                    kql="pii.has_pii: true",
                ),
                search_panel(d, "Identifiers per document", SEARCH_PII, (0, 48, 48, 18)),
            ],
        )
    )

    # --- Entities ------------------------------------------------------------
    d = "Entities"
    objects.append(
        dashboard(
            d,
            "What 'deis entity-scan' found: people, organizations and locations named in the text (spaCy NER, "
            "'sm'-tier models - expect the occasional misfiling).",
            [
                metric_panel(
                    d, "Documents with named entities", (0, 0, 16, 6), kql_filter="entities.has_entities: true"
                ),
                metric_panel(d, "Distinct people", (16, 0, 16, 6), operation="unique_count", field="entities.persons"),
                metric_panel(
                    d,
                    "Distinct organizations",
                    (32, 0, 16, 6),
                    operation="unique_count",
                    field="entities.organizations",
                ),
                tagcloud_panel(d, "People", "entities.persons", (0, 6, 16, 16), size=80),
                tagcloud_panel(d, "Organizations", "entities.organizations", (16, 6, 16, 16), size=80),
                tagcloud_panel(d, "Locations", "entities.locations", (32, 6, 16, 16), size=80),
                table_panel(
                    d,
                    "Organizations by distinct people mentioned alongside",
                    ["entities.organizations"],
                    (0, 22, 24, 16),
                    metric=metric_col("unique_count", "entities.persons", "Distinct people"),
                ),
                table_panel(d, "People by distinct documents", ["entities.persons"], (24, 22, 24, 16), size=30),
                bar_panel(
                    d,
                    "Entity-bearing documents by language",
                    "language",
                    (0, 38, 16, 12),
                    size=5,
                    kql="entities.has_entities: true",
                ),
                bar_panel(
                    d,
                    "Entity-bearing documents by folder",
                    "top_folder",
                    (16, 38, 32, 12),
                    kql="entities.has_entities: true",
                ),
                search_panel(d, "Entities per document", SEARCH_ENTITIES, (0, 50, 48, 18)),
            ],
        )
    )

    # --- Email ---------------------------------------------------------------
    d = "Email"
    objects.append(
        dashboard(
            d,
            "Mail in the dump (.eml, mbox messages, PST/OST output), from the headers ingest parses: who wrote to "
            "whom, when, about what, with what attached.",
            [
                metric_panel(d, "Messages", (0, 0, 12, 6), kql_filter="email.has_email: true"),
                metric_panel(
                    d, "Distinct senders", (12, 0, 12, 6), operation="unique_count", field="email.from_addresses"
                ),
                metric_panel(
                    d, "Distinct recipients", (24, 0, 12, 6), operation="unique_count", field="email.recipients"
                ),
                metric_panel(d, "Attachments", (36, 0, 12, 6), operation="sum", field="email.attachment_count"),
                timeline_panel(
                    d, "Messages per month", "email.date", (0, 6, 48, 12), interval="1M", kql="email.has_email: true"
                ),
                bar_panel(d, "Top senders", "email.from_addresses", (0, 18, 16, 16), size=20),
                bar_panel(d, "Top recipients", "email.recipients", (16, 18, 16, 16), size=20),
                donut_panel(d, "Sender domains", "email.from_domains", (32, 18, 16, 16), size=10),
                table_panel(d, "Top subjects", ["email.subject.keyword"], (0, 34, 24, 16), size=30),
                table_panel(d, "Attachment names", ["email.attachment_names"], (24, 34, 24, 16), size=30),
                search_panel(d, "Messages by date", searches["email"]["id"], (0, 50, 48, 18)),
            ],
        )
    )

    # --- Secrets and sensitive files --------------------------------------
    d = "Secrets and sensitive files"
    secret_types = [
        ("private keys", "secrets.private_keys"),
        ("AWS keys", "secrets.aws_access_keys"),
        ("GitHub tokens", "secrets.github_tokens"),
        ("Slack tokens", "secrets.slack_tokens"),
        ("Google API keys", "secrets.google_api_keys"),
        ("JWTs", "secrets.jwts"),
        ("credential URLs", "secrets.credential_urls"),
        ("password assignments", "secrets.password_assignments"),
    ]
    secret_tiles = [
        metric_panel(d, f"Docs with {label}", (i % 4 * 12, 6 + i // 4 * 6, 12, 6), kql_filter=f"{field}: *")
        for i, (label, field) in enumerate(secret_types)
    ]
    objects.append(
        dashboard(
            d,
            "Credentials 'deis secret-scan' found in text, the files ingest classified as sensitive by type, and "
            "the infrastructure the dump reveals (servers, shares, accounts, addresses).",
            [
                metric_panel(d, "Documents with credentials", (0, 0, 12, 6), kql_filter="secrets.has_secrets: true"),
                metric_panel(d, "Sensitive files", (12, 0, 12, 6), kql_filter="sensitive_class: *"),
                metric_panel(d, "Executables", (24, 0, 12, 6), kql_filter="sensitive_class: executable"),
                metric_panel(d, "Ransom notes", (36, 0, 12, 6), kql_filter="sensitive_class: ransom_note"),
                *secret_tiles,
                donut_panel(d, "Sensitive file classes", "sensitive_class", (0, 18, 16, 16), size=10),
                table_panel(
                    d,
                    "Key material and credential stores",
                    ["filename"],
                    (16, 18, 32, 16),
                    size=30,
                    kql="sensitive_class: (key_material or credential_store or config_secrets)",
                ),
                bar_panel(d, "Servers and shares (UNC paths)", "artifacts.unc_paths", (0, 34, 16, 16), size=20),
                bar_panel(
                    d, "Accounts (from paths and DOMAIN\\user)", "artifacts.usernames", (16, 34, 16, 16), size=25
                ),
                bar_panel(d, "Internal IPv4 addresses", "artifacts.private_ipv4_addresses", (32, 34, 16, 16), size=20),
                bar_panel(d, "Hostnames in URLs", "artifacts.domains", (0, 50, 16, 14), size=20),
                table_panel(d, ".onion addresses", ["artifacts.onion_addresses"], (16, 50, 16, 14)),
                table_panel(d, "Bitcoin addresses", ["artifacts.bitcoin_addresses"], (32, 50, 16, 14)),
                search_panel(d, "Credentials per document", searches["credentials"]["id"], (0, 64, 48, 14)),
                search_panel(d, "Sensitive files by name", searches["sensitive"]["id"], (0, 78, 48, 14)),
                search_panel(d, "Ransom note files", searches["ransom"]["id"], (0, 92, 48, 10)),
            ],
        )
    )

    # --- Corpus triage -------------------------------------------------------
    d = "Corpus triage"
    objects.append(
        dashboard(
            d,
            "What is actually in the dump and what the pipeline could not fully process: formats, sizes, nesting, "
            "mislabelled files, truncated text, and the structured-data columns available for exact queries.",
            [
                metric_panel(d, "Documents", (0, 0, 12, 6)),
                metric_panel(d, "Total size", (12, 0, 12, 6), operation="sum", field="file_size"),
                metric_panel(d, "Extension/content mismatches", (24, 0, 12, 6), kql_filter="mime_mismatch: true"),
                metric_panel(d, "Truncated text", (36, 0, 12, 6), kql_filter="content_truncated: true"),
                bar_panel(d, "Extensions", "extension", (0, 6, 16, 18), size=30),
                bar_panel(
                    d,
                    "No searchable text, by file type",
                    "extension",
                    (16, 6, 16, 18),
                    size=20,
                    kql="attachment.content_length <= 0 or not attachment.content_length: *",
                ),
                donut_panel(d, "Extraction status", "extraction_status", (32, 6, 16, 9)),
                bar_panel(d, "Archive nesting depth", "nesting_depth", (32, 15, 16, 9), size=8),
                table_panel(
                    d,
                    "Largest files",
                    ["filename"],
                    (0, 24, 24, 16),
                    size=25,
                    metric=metric_col("max", "file_size", "Bytes"),
                ),
                table_panel(
                    d,
                    "Extension versus detected type",
                    ["extension", "attachment.content_type.keyword"],
                    (24, 24, 24, 16),
                    size=15,
                    kql="mime_mismatch: true",
                ),
                table_panel(d, "Documents per download source", ["source_chain.url"], (0, 40, 24, 14), size=25),
                tagcloud_panel(d, "Column names in spreadsheets and CSVs", "row_columns", (24, 40, 24, 14), size=100),
                bar_panel(
                    d,
                    "Truncated documents by type",
                    "extension",
                    (0, 54, 16, 12),
                    size=10,
                    kql="content_truncated: true",
                ),
                bar_panel(d, "Sensitive file classes", "sensitive_class", (16, 54, 16, 12), size=10),
                bar_panel(
                    d,
                    "Row-indexed files with the cap hit",
                    "extension",
                    (32, 54, 16, 12),
                    size=5,
                    kql="rows_truncated: true",
                ),
                search_panel(d, "Extension does not match content", searches["mismatch"]["id"], (0, 66, 48, 12)),
                search_panel(d, "Truncated documents by name", searches["truncated"]["id"], (0, 78, 48, 12)),
                search_panel(d, "Files needing attention", SEARCH_NEEDS_ATTENTION, (0, 90, 48, 12)),
            ],
        )
    )

    # --- Timeline ------------------------------------------------------------
    d = "Timeline"
    objects.append(
        dashboard(
            d,
            "How old the data is, by every clock the dump carries: document-creation and last-modified metadata, "
            "the filesystem mtime the files arrived with, mail dates, and photo capture times.",
            [
                metric_panel(d, "Oldest document date", (0, 0, 16, 6), operation="min", field="attachment.date"),
                metric_panel(d, "Newest document date", (16, 0, 16, 6), operation="max", field="attachment.date"),
                metric_panel(d, "Newest modification", (32, 0, 16, 6), operation="max", field="attachment.modified"),
                timeline_panel(d, "Documents by creation year", "attachment.date", (0, 6, 24, 14)),
                timeline_panel(d, "Documents by last-modified year", "attachment.modified", (24, 6, 24, 14)),
                timeline_panel(d, "Files by filesystem mtime (month)", "timestamp", (0, 20, 24, 14), interval="1M"),
                timeline_panel(d, "Mail by month", "email.date", (24, 20, 24, 14), interval="1M"),
                timeline_panel(d, "Photos by capture month", "exif.datetime_original", (0, 34, 24, 14), interval="1M"),
                timeline_panel(
                    d, "Creation year by file type", "attachment.date", (24, 34, 24, 14), breakdown="extension"
                ),
                search_panel(d, "Photos with camera metadata", searches["photos"]["id"], (0, 48, 48, 14)),
            ],
        )
    )

    # --- Ingest health -------------------------------------------------------
    d = "Ingest health"
    objects.append(
        dashboard(
            d,
            "Every ingest run's reconciliation counts over time, and how far each post-ingest scan has got - "
            "the Kibana view of what the web status page and 'deis status' show.",
            [
                lines_panel(
                    d,
                    "Documents in Elasticsearch per run",
                    {"elasticsearch_document_count": "Documents", "unique_files": "Unique files"},
                    (0, 0, 24, 14),
                ),
                lines_panel(
                    d,
                    "Indexed and failed per run",
                    {"indexed_this_run": "Indexed", "failed": "Failed", "csv_files_with_rows_indexed": "Row-indexed"},
                    (24, 0, 24, 14),
                ),
                metric_panel(d, "Documents", (0, 14, 8, 6)),
                metric_panel(d, "PII-scanned", (8, 14, 8, 6), kql_filter="pii.has_pii: *"),
                metric_panel(d, "Entity-scanned", (16, 14, 8, 6), kql_filter="entities.has_entities: *"),
                metric_panel(d, "Secret-scanned", (24, 14, 8, 6), kql_filter="secrets.has_secrets: *"),
                metric_panel(d, "Language known", (32, 14, 8, 6), kql_filter="language: * and not language: unknown"),
                metric_panel(d, "Near-duplicate clustered", (40, 14, 8, 6), kql_filter="duplicate_cluster: *"),
                donut_panel(d, "Extraction status", "extraction_status", (0, 20, 16, 12)),
                bar_panel(
                    d, "Failed Tika parses by type", "extension", (16, 20, 16, 12), size=15, kql="not message: ok"
                ),
                donut_panel(d, "Language", "language", (32, 20, 16, 12)),
                search_panel(d, "Ingest run history", SEARCH_INGEST_RUNS, (0, 32, 48, 14)),
                search_panel(d, "Files needing attention", SEARCH_NEEDS_ATTENTION, (0, 46, 48, 12)),
            ],
        )
    )
    return objects


def regenerate(check: bool = False) -> int:
    generated = build_objects()
    generated_ids = {obj["id"] for obj in generated}
    kept = []
    for line in EXPORT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("id") in generated_ids:
            continue
        kept.append(line)
    # The trailing export-summary line ({"excludedObjects": ..., "exportedCount": ...}) is
    # kept last, updated to the new count, the way Kibana itself writes it.
    summary_index = next((i for i, line in enumerate(kept) if '"exportedCount"' in line), None)
    summary = json.loads(kept.pop(summary_index)) if summary_index is not None else None
    lines = kept + [json.dumps(obj, ensure_ascii=False, separators=(",", ":")) for obj in generated]
    if summary is not None:
        summary["exportedCount"] = len(lines)
        lines.append(json.dumps(summary, separators=(",", ":")))
    new_text = "\n".join(lines) + "\n"
    if check:
        if new_text != EXPORT.read_text(encoding="utf-8"):
            print(f"{EXPORT} is out of date - run: uv run python3 setup/dashboards.py", file=sys.stderr)
            return 1
        print(f"{EXPORT} is up to date.")
        return 0
    EXPORT.write_text(new_text, encoding="utf-8")
    print(f"Wrote {len(generated)} generated object(s) into {EXPORT} ({len(lines)} objects total).")
    return 0


if __name__ == "__main__":
    sys.exit(regenerate(check="--check" in sys.argv[1:]))
