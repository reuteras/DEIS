"""Tests for setup/dashboards.py (item 55): the generated Kibana saved
objects are structurally sound and export.ndjson is in sync with them.
Every field a Lens panel or a control reads must exist - either mapped in
setup/entrypoint.sh, or one of the fields Tika/the pipeline create
dynamically - so a renamed field cannot silently leave a panel empty.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_dashboards", REPO_ROOT / "setup" / "dashboards.py")
dashboards = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboards
spec.loader.exec_module(dashboards)

# Created by the attachment processor / the pipeline / runtime scripts, not
# in the explicit mapping.
_DYNAMIC_FIELDS = {
    "___records___",
    "attachment.date",
    "attachment.modified",
    "attachment.content_type",
    "attachment.content_type.keyword",
    "attachment.content_length",
    "timestamp",
    "message",
    "top_folder",
    "pii_email_domain",
    "@timestamp",
    "elasticsearch_document_count",
    "unique_files",
    "indexed_this_run",
    "failed",
    "csv_files_with_rows_indexed",
    "email.subject.keyword",
}


def _mapped_field_names() -> set[str]:
    text = (REPO_ROOT / "setup" / "entrypoint.sh").read_text(encoding="utf-8")
    names = set(re.findall(r'"([A-Za-z_@][A-Za-z0-9_.]*)"\s*:\s*\{\s*"type"', text))
    names |= set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\{\s*"properties"', text))
    return names


def _field_known(field: str, mapped: set[str]) -> bool:
    if field in _DYNAMIC_FIELDS or field in mapped:
        return True
    # Nested "group.leaf": both parts must be mapped.
    if "." in field:
        group, leaf = field.split(".", 1)
        return group in mapped and leaf in mapped
    return False


def _export_objects() -> list[dict]:
    return [json.loads(line) for line in dashboards.EXPORT.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestGeneratedObjects:
    def test_ids_are_unique_and_stable(self):
        objects = dashboards.build_objects()
        ids = [obj["id"] for obj in objects]
        assert len(ids) == len(set(ids))
        assert ids == [obj["id"] for obj in dashboards.build_objects()]

    def test_export_is_in_sync(self):
        assert dashboards.regenerate(check=True) == 0

    def test_every_reference_resolves(self):
        exported = {obj["id"]: obj for obj in _export_objects() if "id" in obj}
        for obj in dashboards.build_objects():
            for ref in obj["references"]:
                assert ref["id"] in exported, f"{obj['attributes']['title']}: dangling reference {ref}"
                assert exported[ref["id"]]["type"] == ref["type"]

    def test_panel_references_match_panels(self):
        for obj in dashboards.build_objects():
            if obj["type"] != "dashboard":
                continue
            panels = json.loads(obj["attributes"]["panelsJSON"])
            ref_names = {ref["name"] for ref in obj["references"]}
            indexes = [panel["panelIndex"] for panel in panels]
            assert len(indexes) == len(set(indexes)), obj["attributes"]["title"]
            for panel in panels:
                if panel["type"] == "search":
                    assert f"{panel['panelIndex']}:{panel['panelRefName']}" in ref_names
                else:
                    layer_ids = panel["embeddableConfig"]["attributes"]["state"]["datasourceStates"]["formBased"][
                        "layers"
                    ]
                    for layer_id in layer_ids:
                        assert f"{panel['panelIndex']}:indexpattern-datasource-layer-{layer_id}" in ref_names

    def test_panels_fit_the_grid(self):
        for obj in dashboards.build_objects():
            if obj["type"] != "dashboard":
                continue
            for panel in json.loads(obj["attributes"]["panelsJSON"]):
                grid = panel["gridData"]
                assert grid["x"] + grid["w"] <= 48, (obj["attributes"]["title"], panel["title"])
                assert grid["w"] > 0 and grid["h"] > 0

    def test_every_lens_field_and_control_field_is_known(self):
        mapped = _mapped_field_names()
        for obj in dashboards.build_objects():
            if obj["type"] != "dashboard":
                continue
            title = obj["attributes"]["title"]
            for panel in json.loads(obj["attributes"]["panelsJSON"]):
                if panel["type"] != "lens":
                    continue
                layers = panel["embeddableConfig"]["attributes"]["state"]["datasourceStates"]["formBased"]["layers"]
                for layer in layers.values():
                    for column in layer["columns"].values():
                        assert _field_known(column["sourceField"], mapped), (
                            title,
                            panel["title"],
                            column["sourceField"],
                        )
            control_group = obj["attributes"].get("controlGroupInput")
            if control_group:
                for control in json.loads(control_group["panelsJSON"]).values():
                    assert _field_known(control["explicitInput"]["fieldName"], mapped), (title, control)

    def test_subject_lookup_has_controls(self):
        subject = next(o for o in dashboards.build_objects() if o["attributes"]["title"] == "Subject lookup")
        controls = json.loads(subject["attributes"]["controlGroupInput"]["panelsJSON"])
        fields = {c["explicitInput"]["fieldName"] for c in controls.values()}
        assert {"pii.personnummer_normalized", "pii.emails", "entities.persons"} <= fields

    def test_saved_search_columns_are_known(self):
        mapped = _mapped_field_names()
        for obj in dashboards.build_objects():
            if obj["type"] == "search":
                for column in obj["attributes"]["columns"]:
                    assert _field_known(column, mapped) or column in ("filename", "sha256", "location"), column
