#!/usr/bin/env python3
"""Run structural, oracle, provenance, and privacy QA for the E0 fixture.

This development script is deliberately fixture-specific. It uses the E0 raw
OOXML observer as a test oracle and must not be imported by the production
review-ingest path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from docx import Document
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {
    "w": W_NS,
    "cp": CP_NS,
    "dc": DC_NS,
    "ep": EP_NS,
    "pr": PKG_REL_NS,
    "ct": CT_NS,
}
LOCAL_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|file:(?://|\\\\)|\\\\[^\\/\s]+[\\/]"
    r"|/(?:Users|home|tmp|var/tmp|private|mnt/[A-Za-z])/)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=reject_duplicate_json_keys)


def assert_within(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{label} must remain inside {resolved_root}: {resolved_candidate}"
        ) from exc
    return resolved_candidate


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def run_observer(project_root: Path, docx_path: Path) -> dict[str, Any]:
    command = [sys.executable, str(project_root / "scripts" / "e0_inspect_docx.py"), str(docx_path)]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"observer timed out for {docx_path.name}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"observer failed for {docx_path.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def source_counts(source_root: Path) -> dict[str, int]:
    tex_files = sorted(source_root.rglob("*.tex"))
    bib_files = sorted(source_root.rglob("*.bib"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in tex_files)
    return {
        "tex_files": len(tex_files),
        "input_commands": len(re.findall(r"\\input\s*\{", text)),
        "sections": len(re.findall(r"\\section\s*\{", text)),
        "inline_math_segments": len(re.findall(r"(?<!\\)\$(?!\$).*?(?<!\\)\$", text, re.S)),
        "display_math_environments": len(re.findall(r"\\begin\{(?:equation|align)\*?\}", text)),
        "equation_rows": len(re.findall(r"&=", text))
        + len(re.findall(r"\\begin\{equation\*?\}", text)),
        "labels": len(re.findall(r"\\label\s*\{", text)),
        "references": len(re.findall(r"\\(?:eqref|ref)\s*\{", text)),
        "figure_environments": len(re.findall(r"\\begin\{figure\*?\}", text)),
        "includegraphics_commands": len(re.findall(r"\\includegraphics(?:\[[^]]*\])?\s*\{", text)),
        "table_environments": len(re.findall(r"\\begin\{table\*?\}", text)),
        "citations": len(re.findall(r"\\cite\w*(?:\[[^]]*\])?\s*\{", text)),
        "bibliography_commands": len(re.findall(r"\\bibliography\s*\{", text)),
        "bib_files": len(bib_files),
    }


def main_story(observation: dict[str, Any]) -> dict[str, Any]:
    matches = [story for story in observation["stories"] if story["part"] == "word/document.xml"]
    if len(matches) != 1:
        raise RuntimeError("observer did not return exactly one main document story")
    return matches[0]


def xml_root(archive: zipfile.ZipFile, part: str) -> etree._Element:
    data = archive.read(part)
    declaration_scan = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise RuntimeError(f"DTD or entity declaration is forbidden in {part}")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    return etree.fromstring(data, parser=parser)


def track_revisions_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        root = xml_root(archive, "word/settings.xml")
        return len(root.findall(".//w:trackRevisions", namespaces=NS))


def move_marker_counts(path: Path) -> dict[str, int]:
    names = ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")
    with zipfile.ZipFile(path) as archive:
        root = xml_root(archive, "word/document.xml")
        return {name: len(root.findall(f".//w:{name}", namespaces=NS)) for name in names}


def move_marker_details(path: Path) -> list[dict[str, str | None]]:
    names = ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")
    details: list[dict[str, str | None]] = []
    with zipfile.ZipFile(path) as archive:
        root = xml_root(archive, "word/document.xml")
    for name in names:
        for node in root.findall(f".//w:{name}", namespaces=NS):
            details.append(
                {
                    "element": f"w:{name}",
                    "id": node.get(f"{{{W_NS}}}id"),
                    "author": node.get(f"{{{W_NS}}}author"),
                    "date": node.get(f"{{{W_NS}}}date"),
                    "name": node.get(f"{{{W_NS}}}name"),
                }
            )
    return details


def revision_text_markup(path: Path) -> dict[str, int]:
    """Count the Word-compatible text nodes used by delete and move revisions."""
    with zipfile.ZipFile(path) as archive:
        root = xml_root(archive, "word/document.xml")
    deletions = root.findall(".//w:del", namespaces=NS)
    move_sources = root.findall(".//w:moveFrom", namespaces=NS)
    return {
        "w:del/w:delText": sum(
            len(node.findall(".//w:delText", namespaces=NS)) for node in deletions
        ),
        "w:del/w:t": sum(len(node.findall(".//w:t", namespaces=NS)) for node in deletions),
        "w:moveFrom/w:t": sum(len(node.findall(".//w:t", namespaces=NS)) for node in move_sources),
        "w:moveFrom/w:delText": sum(
            len(node.findall(".//w:delText", namespaces=NS)) for node in move_sources
        ),
    }


def comment_anchor_texts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        root = xml_root(archive, "word/document.xml")
    anchors: dict[str, list[str]] = {}
    for paragraph in root.findall(".//w:p", namespaces=NS):
        active: dict[str, list[str]] = {}
        for node in paragraph.iter():
            if node.tag == f"{{{W_NS}}}commentRangeStart":
                comment_id = node.get(f"{{{W_NS}}}id")
                if comment_id is not None:
                    active[comment_id] = []
            elif node.tag in {f"{{{W_NS}}}t", f"{{{W_NS}}}delText"}:
                for chunks in active.values():
                    chunks.append(node.text or "")
            elif node.tag == f"{{{W_NS}}}commentRangeEnd":
                comment_id = node.get(f"{{{W_NS}}}id")
                if comment_id in active:
                    anchors[comment_id] = active.pop(comment_id)
        if active:
            raise RuntimeError(f"unterminated comment range(s): {sorted(active)}")
    return {key: "".join(value) for key, value in sorted(anchors.items())}


def comment_plumbing(path: Path) -> dict[str, int]:
    comments_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    comments_content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    )
    with zipfile.ZipFile(path) as archive:
        parts = set(archive.namelist())
        relationship_count = 0
        if "word/_rels/document.xml.rels" in parts:
            rels = xml_root(archive, "word/_rels/document.xml.rels")
            relationship_count = sum(
                1
                for item in rels.findall("pr:Relationship", namespaces=NS)
                if item.get("Type") == comments_type and item.get("Target") == "comments.xml"
            )
        content_types = xml_root(archive, "[Content_Types].xml")
        override_count = sum(
            1
            for item in content_types.findall("ct:Override", namespaces=NS)
            if item.get("PartName") == "/word/comments.xml"
            and item.get("ContentType") == comments_content_type
        )
        return {
            "comments_part": int("word/comments.xml" in parts),
            "comments_relationships": relationship_count,
            "comments_content_type_overrides": override_count,
        }


def docx_structure(path: Path, observation: dict[str, Any]) -> dict[str, Any]:
    story = main_story(observation)
    anchors = story["comment_anchors"]
    start_ids = sorted(value for value in anchors["start"] if value is not None)
    end_ids = sorted(value for value in anchors["end"] if value is not None)
    reference_ids = sorted(value for value in anchors["reference"] if value is not None)
    if not (start_ids == end_ids == reference_ids):
        raise RuntimeError(
            f"comment anchor mismatch in {path.name}: "
            f"start={start_ids}, end={end_ids}, refs={reference_ids}"
        )
    result = {
        "track_revisions": track_revisions_count(path),
        "revision_counts": story["revision_counts"],
        "comments": len(observation["comments"]),
        "comment_anchor_ids": start_ids,
        "tables": story["tables"],
        "images": sum(1 for part in observation["parts"] if part.startswith("word/media/")),
        "math_objects": story["math_objects"],
        "math_paragraphs": story["math_paragraphs"],
        "bookmark_names": story["bookmark_names"],
        "move_range_markers": move_marker_counts(path),
    }
    result.update(comment_plumbing(path))
    return result


def revision_index(
    observation: dict[str, Any],
) -> tuple[dict[tuple[str, str | None], dict[str, Any]], list[tuple[str, str | None]]]:
    index: dict[tuple[str, str | None], dict[str, Any]] = {}
    duplicates: list[tuple[str, str | None]] = []
    for event in main_story(observation)["revisions"]:
        key = (event["kind"], event["id"])
        if key in index:
            duplicates.append(key)
        else:
            index[key] = event
    return index, duplicates


def display_event_keys(keys: list[tuple[str, str | None]]) -> list[dict[str, str | None]]:
    return [{"kind": kind, "id": event_id} for kind, event_id in keys]


def check_changeset_oracle(
    expected: dict[str, Any], returned: dict[str, Any], returned_path: Path
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    story = main_story(returned)
    revisions, duplicate_revision_keys = revision_index(returned)

    expected_raw = expected["raw_revision_element_counts"]
    actual_raw = {
        "w:ins": story["revision_counts"].get("insert", 0),
        "w:del": story["revision_counts"].get("delete", 0),
        "w:moveFrom": story["revision_counts"].get("move_from", 0),
        "w:moveTo": story["revision_counts"].get("move_to", 0),
        "w:rPrChange": story["revision_counts"].get("run_format", 0),
        "w:comment": len(returned["comments"]),
    }
    checks.append(
        {
            "name": "raw_event_counts",
            "expected": expected_raw,
            "actual": actual_raw,
            "pass": actual_raw == expected_raw,
        }
    )
    expected_text_markup = {
        "w:del/w:delText": 2,
        "w:del/w:t": 0,
        "w:moveFrom/w:t": 1,
        "w:moveFrom/w:delText": 0,
    }
    actual_text_markup = revision_text_markup(returned_path)
    checks.append(
        {
            "name": "word_compatible_revision_text_markup",
            "expected": expected_text_markup,
            "actual": actual_text_markup,
            "pass": actual_text_markup == expected_text_markup,
        }
    )

    kind_map = {
        "w:ins": "insert",
        "w:del": "delete",
        "w:moveFrom": "move_from",
        "w:moveTo": "move_to",
        "w:rPrChange": "run_format",
    }
    expected_revision_keys = [
        (kind_map[raw["element"]], raw["w_id"])
        for change in expected["changes"]
        if change["kind"] != "comment"
        for raw in change["raw_events"]
    ]
    actual_revision_keys = [(event["kind"], event["id"]) for event in story["revisions"]]
    expected_duplicate_keys = [
        key for key, count in Counter(expected_revision_keys).items() if count > 1
    ]
    checks.append(
        {
            "name": "revision_event_identities_exact",
            "expected": display_event_keys(expected_revision_keys),
            "actual": display_event_keys(actual_revision_keys),
            "pass": Counter(actual_revision_keys) == Counter(expected_revision_keys),
        }
    )
    checks.append(
        {
            "name": "revision_event_identities_unique",
            "expected": [],
            "actual": display_event_keys(duplicate_revision_keys),
            "oracle_duplicates": display_event_keys(expected_duplicate_keys),
            "pass": not duplicate_revision_keys and not expected_duplicate_keys,
        }
    )

    for change in expected["changes"]:
        if change["kind"] == "comment":
            continue
        for raw in change["raw_events"]:
            key = (kind_map[raw["element"]], raw["w_id"])
            event = revisions.get(key)
            passed = (
                event is not None
                and event["author"] == change["author"]
                and event["date"] == change["timestamp"]
            )
            if event is not None:
                if key[0] == "run_format":
                    passed = (
                        passed
                        and event["text"] == change["text"]
                        and event.get("format_before") == change["before"]
                        and event.get("format_after") == change["after"]
                    )
                else:
                    expected_text = (
                        change.get("before")
                        if key[0] in {"delete", "move_from"}
                        else change.get("after")
                    )
                    passed = passed and event["text"] == expected_text
            checks.append(
                {
                    "name": f"{change['change_id']}:{raw['element']}",
                    "expected": change,
                    "actual": event,
                    "pass": passed,
                }
            )

    marker_details = move_marker_details(returned_path)
    expected_marker_keys: list[tuple[str, str | None]] = []
    for change in expected["changes"]:
        if change["kind"] != "move":
            continue
        range_names: list[str] = []
        for raw in change["raw_events"]:
            base_name = raw["element"].removeprefix("w:")
            start_element = f"w:{base_name}RangeStart"
            end_element = f"w:{base_name}RangeEnd"
            range_id = raw["range_id"]
            expected_marker_keys.extend(((start_element, range_id), (end_element, range_id)))
            starts = [
                item
                for item in marker_details
                if item["element"] == start_element and item["id"] == range_id
            ]
            ends = [
                item
                for item in marker_details
                if item["element"] == end_element and item["id"] == range_id
            ]
            if len(starts) == 1 and starts[0]["name"]:
                range_names.append(starts[0]["name"])
            passed = (
                len(starts) == 1
                and len(ends) == 1
                and starts[0]["author"] == change["author"]
                and starts[0]["date"] == change["timestamp"]
                and bool(starts[0]["name"])
            )
            checks.append(
                {
                    "name": f"{change['change_id']}:{raw['element']}:range",
                    "expected": {
                        "range_id": range_id,
                        "author": change["author"],
                        "date": change["timestamp"],
                        "non_empty_name": True,
                    },
                    "actual": {"start": starts, "end": ends},
                    "pass": passed,
                }
            )
        checks.append(
            {
                "name": f"{change['change_id']}:range_name_consistency",
                "expected": "one shared non-empty move name",
                "actual": range_names,
                "pass": len(range_names) == len(change["raw_events"])
                and len(set(range_names)) == 1,
            }
        )
    actual_marker_keys = [(item["element"], item["id"]) for item in marker_details]
    checks.append(
        {
            "name": "move_range_marker_identities_exact",
            "expected": [
                {"element": element, "id": marker_id} for element, marker_id in expected_marker_keys
            ],
            "actual": [
                {"element": element, "id": marker_id} for element, marker_id in actual_marker_keys
            ],
            "pass": Counter(actual_marker_keys) == Counter(expected_marker_keys),
        }
    )

    expected_comment_ids = [
        change["raw_events"][0]["w_id"]
        for change in expected["changes"]
        if change["kind"] == "comment"
    ]
    actual_comment_ids = [item["id"] for item in returned["comments"]]
    checks.append(
        {
            "name": "comment_identities_exact_and_unique",
            "expected": expected_comment_ids,
            "actual": actual_comment_ids,
            "pass": Counter(actual_comment_ids) == Counter(expected_comment_ids)
            and len(actual_comment_ids) == len(set(actual_comment_ids))
            and len(expected_comment_ids) == len(set(expected_comment_ids)),
        }
    )
    for anchor_kind, anchor_ids in story["comment_anchors"].items():
        checks.append(
            {
                "name": f"comment_anchor_{anchor_kind}_identities_exact",
                "expected": expected_comment_ids,
                "actual": anchor_ids,
                "pass": Counter(anchor_ids) == Counter(expected_comment_ids),
            }
        )

    comments: dict[str | None, dict[str, Any]] = {}
    for item in returned["comments"]:
        comments.setdefault(item["id"], item)
    anchor_texts = comment_anchor_texts(returned_path)
    for change in expected["changes"]:
        if change["kind"] != "comment":
            continue
        comment_id = change["raw_events"][0]["w_id"]
        actual = comments.get(comment_id)
        actual_anchor = anchor_texts.get(comment_id)
        passed = (
            actual is not None
            and actual["author"] == change["author"]
            and actual["date"] == change["timestamp"]
            and actual["text"] == change["comment"]
            and actual_anchor == change["anchored_text"]
        )
        checks.append(
            {
                "name": change["change_id"],
                "expected": change,
                "actual": {"comment": actual, "anchored_text": actual_anchor},
                "pass": passed,
            }
        )
    return checks


def privacy_scan(
    path: Path,
    allowed_reviewers: set[str],
    allowed_dates: set[str],
    allowed_initials: set[str],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    observed_authors: set[str] = set()
    observed_dates: set[str] = set()
    observed_initials: set[str] = set()
    external_relationships: list[dict[str, str | None]] = []
    hidden_text_elements = 0
    ole_object_elements = 0
    rsid_attributes = 0
    with zipfile.ZipFile(path) as archive:
        parts = set(archive.namelist())
        lower_parts = {name.lower() for name in parts}
        text_chunks: list[str] = []
        for name in sorted(parts):
            if name.endswith((".xml", ".rels")):
                data = archive.read(name)
                text_chunks.append(data.decode("utf-8", errors="ignore"))
                root = xml_root(archive, name)
                for element in root.iter():
                    author = element.get(f"{{{W_NS}}}author")
                    date = element.get(f"{{{W_NS}}}date")
                    initials = element.get(f"{{{W_NS}}}initials")
                    if author:
                        observed_authors.add(author)
                    if date:
                        observed_dates.add(date)
                    if initials:
                        observed_initials.add(initials)
                    if element.tag == f"{{{W_NS}}}vanish":
                        hidden_text_elements += 1
                    if (
                        isinstance(element.tag, str)
                        and etree.QName(element).localname == "OLEObject"
                    ):
                        ole_object_elements += 1
                    for attribute_name in element.attrib:
                        attribute_qname = etree.QName(attribute_name)
                        if (
                            attribute_qname.namespace == W_NS
                            and attribute_qname.localname.lower().startswith("rsid")
                        ):
                            rsid_attributes += 1
                    if (
                        element.tag == f"{{{PKG_REL_NS}}}Relationship"
                        and element.get("TargetMode") == "External"
                    ):
                        external_relationships.append(
                            {"part": name, "id": element.get("Id"), "target": element.get("Target")}
                        )
        all_text = "\n".join(text_chunks)

        core_creator = ""
        last_modified_by = ""
        if "docProps/core.xml" in parts:
            core = xml_root(archive, "docProps/core.xml")
            creator = core.find("dc:creator", namespaces=NS)
            modifier = core.find("cp:lastModifiedBy", namespaces=NS)
            core_creator = "" if creator is None else (creator.text or "")
            last_modified_by = "" if modifier is None else (modifier.text or "")

        company = ""
        manager = ""
        hyperlink_base = ""
        if "docProps/app.xml" in parts:
            app = xml_root(archive, "docProps/app.xml")
            company_node = app.find("ep:Company", namespaces=NS)
            manager_node = app.find("ep:Manager", namespaces=NS)
            hyperlink_base_node = app.find("ep:HyperlinkBase", namespaces=NS)
            company = "" if company_node is None else (company_node.text or "")
            manager = "" if manager_node is None else (manager_node.text or "")
            hyperlink_base = "" if hyperlink_base_node is None else (hyperlink_base_node.text or "")

        checks = {
            "core_creator_empty": core_creator == "",
            "last_modified_by_empty": last_modified_by == "",
            "company_empty": company == "",
            "manager_empty": manager == "",
            "hyperlink_base_empty": hyperlink_base == "",
            "custom_properties_absent": "docProps/custom.xml" not in parts,
            "custom_xml_absent": not any(name.startswith("customXml/") for name in parts),
            "thumbnail_absent": not any("thumbnail" in name.lower() for name in parts),
            "embedded_objects_absent": not any(
                name.startswith("word/embeddings/") for name in parts
            ),
            "ole_objects_absent": ole_object_elements == 0,
            "active_x_absent": not any(name.startswith("word/activex/") for name in lower_parts),
            "macros_absent": not any(
                name.endswith(("vbaproject.bin", "vbadata.xml")) for name in lower_parts
            ),
            "modern_comment_identity_parts_absent": not any(
                name
                in {
                    "word/people.xml",
                    "word/commentsextended.xml",
                    "word/commentsids.xml",
                    "word/commentsextensible.xml",
                }
                for name in lower_parts
            ),
            "hidden_text_absent": hidden_text_elements == 0,
            "rsid_attributes_absent": rsid_attributes == 0,
            "external_relationships_absent": not external_relationships,
            "email_addresses_absent": re.search(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", all_text, re.I
            )
            is None,
            "local_paths_absent": LOCAL_PATH_RE.search(all_text) is None,
            "review_authors_declared": observed_authors.issubset(allowed_reviewers),
            "review_dates_declared": observed_dates.issubset(allowed_dates),
            "review_initials_declared": observed_initials.issubset(allowed_initials),
        }
        for check, passed in checks.items():
            if not passed:
                findings.append({"check": check, "severity": "error"})
        return {
            "file": path.name,
            "sha256": sha256_file(path),
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "findings": findings,
            "intentional_synthetic_review_authors": sorted(observed_authors),
            "intentional_synthetic_review_dates": sorted(observed_dates),
            "intentional_synthetic_review_initials": sorted(observed_initials),
            "external_relationships": external_relationships,
        }


def compare_dict(expected: dict[str, Any], actual: dict[str, Any], name: str) -> dict[str, Any]:
    comparable = {key: actual.get(key) for key in expected}
    return {
        "name": name,
        "expected": expected,
        "actual": comparable,
        "pass": comparable == expected,
    }


def safe_fixture_relative_path(value: str) -> str | None:
    if not value or "\\" in value or "\x00" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts:
        return None
    if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        return None
    normalized = pure.as_posix()
    return normalized if normalized == value else None


def release_check(name: str, expected: Any, actual: Any, passed: bool) -> dict[str, Any]:
    return {"name": name, "expected": expected, "actual": actual, "pass": passed}


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        raise ValueError(f"project root does not exist: {project_root}")
    fixture_candidate = (
        args.fixture_root.resolve()
        if args.fixture_root is not None
        else project_root / "tests" / "fixtures" / "e0-minimal-paper"
    )
    fixture_root = assert_within(
        project_root / "tests" / "fixtures", fixture_candidate, "fixture root"
    )
    output_candidate = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else project_root / "build" / "e0-fixture-qa"
    )
    output_dir = assert_within(project_root / "build", output_candidate, "output directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    expected_structure = load_json(fixture_root / "expected-structure.json")
    expected_changeset = load_json(fixture_root / "expected-changeset.json")
    provenance = load_json(fixture_root / "provenance.json")
    base_path = fixture_root / "base" / "review-base.docx"
    returned_path = fixture_root / "returned" / "returned-reviewed.docx"

    # The bounded raw-package observer runs before any higher-level parser.
    base_observation = run_observer(project_root, base_path)
    returned_observation = run_observer(project_root, returned_path)
    # Opening with python-docx is a package-integrity smoke test; no save occurs.
    Document(str(base_path))
    Document(str(returned_path))
    write_json(output_dir / "observer-base.json", base_observation)
    write_json(output_dir / "observer-returned.json", returned_observation)

    actual_source = source_counts(fixture_root / "source")
    actual_base = docx_structure(base_path, base_observation)
    actual_returned = docx_structure(returned_path, returned_observation)
    structure_checks = [
        compare_dict(expected_structure["source"], actual_source, "source_structure"),
        compare_dict(expected_structure["base_docx"], actual_base, "base_docx_structure"),
        compare_dict(
            expected_structure["returned_docx"], actual_returned, "returned_docx_structure"
        ),
    ]
    structure_report = {
        "schema_version": "e0-fixture-qa-v1",
        "fixture_id": expected_structure["fixture_id"],
        "status": "pass" if all(item["pass"] for item in structure_checks) else "fail",
        "checks": structure_checks,
    }
    write_json(output_dir / "structure-report.json", structure_report)

    oracle_checks = check_changeset_oracle(expected_changeset, returned_observation, returned_path)
    oracle_report = {
        "schema_version": "e0-oracle-qa-v1",
        "fixture_id": expected_changeset["fixture_id"],
        "status": "pass" if all(item["pass"] for item in oracle_checks) else "fail",
        "checks": oracle_checks,
    }
    write_json(output_dir / "changeset-oracle-report.json", oracle_report)

    allowed_reviewers = set(expected_changeset["synthetic_reviewers"])
    allowed_dates = {change["timestamp"] for change in expected_changeset["changes"]}
    allowed_initials = set(expected_changeset.get("synthetic_reviewer_initials", []))
    privacy_results = [
        privacy_scan(base_path, allowed_reviewers, allowed_dates, allowed_initials),
        privacy_scan(returned_path, allowed_reviewers, allowed_dates, allowed_initials),
    ]
    privacy_report = {
        "schema_version": "e0-privacy-scan-v1",
        "fixture_id": expected_changeset["fixture_id"],
        "status": "pass" if all(item["status"] == "pass" for item in privacy_results) else "fail",
        "documents": privacy_results,
    }
    write_json(output_dir / "privacy-report.json", privacy_report)

    manifest = provenance["files"]
    hash_checks: list[dict[str, Any]] = []
    for relative, expected_hash in manifest.items():
        normalized = safe_fixture_relative_path(relative)
        path = (
            assert_within(fixture_root, fixture_root / normalized, "manifest file")
            if normalized is not None
            else None
        )
        actual_hash = sha256_file(path) if path is not None and path.is_file() else None
        hash_checks.append(
            {
                "path": relative,
                "expected": expected_hash,
                "actual": actual_hash,
                "safe_relative_path": normalized is not None,
                "pass": normalized is not None and actual_hash == expected_hash,
            }
        )

    manifest_exclusions = {"README.md", "provenance.json"}
    actual_manifest_files = sorted(
        path.relative_to(fixture_root).as_posix()
        for path in fixture_root.rglob("*")
        if path.is_file() and path.relative_to(fixture_root).as_posix() not in manifest_exclusions
    )
    expected_manifest_files = sorted(manifest)
    integrity_checks: list[dict[str, Any]] = [
        *hash_checks,
        {
            "name": "manifest_file_set_complete",
            "expected": expected_manifest_files,
            "actual": actual_manifest_files,
            "excluded_self_referential_or_documentation_files": sorted(manifest_exclusions),
            "pass": expected_manifest_files == actual_manifest_files,
        },
        {
            "name": "fixture_ids_consistent",
            "expected": provenance["fixture_id"],
            "actual": {
                "expected_structure": expected_structure["fixture_id"],
                "expected_changeset": expected_changeset["fixture_id"],
            },
            "pass": provenance["fixture_id"]
            == expected_structure["fixture_id"]
            == expected_changeset["fixture_id"],
        },
        {
            "name": "oracle_declared_independent",
            "expected": "manually-authored-independent-oracle",
            "actual": expected_changeset.get("source_of_truth"),
            "pass": expected_changeset.get("source_of_truth")
            == "manually-authored-independent-oracle",
        },
    ]

    generator = provenance.get("generator", {})
    generator_script_relative = generator.get("script")
    generator_text = ""
    generator_script_safe = False
    if isinstance(generator_script_relative, str):
        normalized_script = safe_fixture_relative_path(generator_script_relative)
        if normalized_script is not None:
            generator_script_path = assert_within(
                project_root, project_root / normalized_script, "generator script"
            )
            if generator_script_path.is_file():
                generator_script_safe = True
                generator_text = generator_script_path.read_text(encoding="utf-8")
    external_dependency_evidence = [
        token
        for token in ("--documents-skill-root", "documents_skill_root")
        if token in generator_text
    ]
    if generator.get("documents_skill_version"):
        external_dependency_evidence.append("provenance:documents_skill_version")

    license_value = str(provenance.get("license", "")).strip()
    unresolved_license_tokens = {"", "PENDING", "TBD", "NONE", "UNLICENSED", "PROPRIETARY"}
    license_ready = license_value.upper() not in unresolved_license_tokens and not any(
        token in license_value.upper() for token in ("PENDING", "TBD", "TO_BE_CONFIRMED")
    )
    release_status = str(provenance.get("public_release_status", "")).strip()
    release_status_ready = bool(release_status) and not any(
        token in release_status.lower() for token in ("blocked", "pending", "hold")
    )
    third_party_components = provenance.get("third_party_components")
    python_docx_component = None
    if isinstance(third_party_components, list):
        python_docx_component = next(
            (
                item
                for item in third_party_components
                if isinstance(item, dict)
                and item.get("name") == "python-docx default DOCX template"
            ),
            None,
        )
    notice_relative = (
        python_docx_component.get("notice") if isinstance(python_docx_component, dict) else None
    )
    notice_text = ""
    notice_path_safe = False
    if isinstance(notice_relative, str):
        normalized_notice = safe_fixture_relative_path(notice_relative)
        if normalized_notice is not None:
            notice_path = assert_within(
                project_root, project_root / normalized_notice, "third-party notice"
            )
            if notice_path.is_file():
                notice_path_safe = True
                notice_text = notice_path.read_text(encoding="utf-8")
    python_docx_notice_ready = bool(
        isinstance(python_docx_component, dict)
        and python_docx_component.get("version") == "1.2.0"
        and python_docx_component.get("license") == "MIT"
        and notice_path_safe
        and "Copyright (c) 2013 Steve Canny" in notice_text
        and "Permission is hereby granted" in notice_text
    )
    release_checks = [
        release_check(
            "contains_no_personal_or_private_content",
            {"contains_personal_data": False, "contains_private_paper_content": False},
            {
                "contains_personal_data": provenance.get("contains_personal_data"),
                "contains_private_paper_content": provenance.get("contains_private_paper_content"),
            },
            provenance.get("contains_personal_data") is False
            and provenance.get("contains_private_paper_content") is False,
        ),
        release_check(
            "creator_allows_redistribution",
            True,
            provenance.get("redistribution_allowed_by_creator"),
            provenance.get("redistribution_allowed_by_creator") is True,
        ),
        release_check(
            "fixture_license_resolved", "non-placeholder license", license_value, license_ready
        ),
        release_check(
            "python_docx_template_notice_preserved",
            "python-docx 1.2.0 MIT provenance and complete notice",
            {
                "component": python_docx_component,
                "notice_path_safe": notice_path_safe,
            },
            python_docx_notice_ready,
        ),
        release_check(
            "public_release_status_ready",
            "non-blocked release status",
            release_status,
            release_status_ready,
        ),
        release_check(
            "privacy_review_complete",
            "pass",
            privacy_report["status"],
            privacy_report["status"] == "pass",
        ),
        release_check(
            "determinism_review_complete",
            "pass",
            provenance.get("determinism_review", {}).get("status"),
            provenance.get("determinism_review", {}).get("status") == "pass",
        ),
        release_check(
            "visual_review_complete",
            "pass",
            provenance.get("visual_review", {}).get("status"),
            provenance.get("visual_review", {}).get("status") == "pass",
        ),
        release_check(
            "generator_is_repository_self_contained",
            {"script_safe_and_present": True, "external_dependency_evidence": []},
            {
                "script_safe_and_present": generator_script_safe,
                "external_dependency_evidence": external_dependency_evidence,
            },
            generator_script_safe and not external_dependency_evidence,
        ),
    ]
    integrity_passed = all(item["pass"] for item in integrity_checks)
    release_ready = all(item["pass"] for item in release_checks)
    provenance_report = {
        "schema_version": "e0-provenance-qa-v1",
        "status": "pass" if integrity_passed and release_ready else "fail",
        "integrity_status": "pass" if integrity_passed else "fail",
        "release_readiness": "ready" if release_ready else "blocked",
        "checks": integrity_checks,
        "release_checks": release_checks,
    }
    write_json(output_dir / "provenance-report.json", provenance_report)

    reports = [structure_report, oracle_report, privacy_report, provenance_report]
    passed = all(report["status"] == "pass" for report in reports)
    summary = {
        "schema_version": "e0-fixture-qa-summary-v1",
        "fixture_id": expected_changeset["fixture_id"],
        "status": "pass" if passed else "fail",
        "fixture_integrity_status": provenance_report["integrity_status"],
        "release_readiness": provenance_report["release_readiness"],
        "reports": {
            "structure": "structure-report.json",
            "changeset_oracle": "changeset-oracle-report.json",
            "privacy": "privacy-report.json",
            "provenance": "provenance-report.json",
        },
    }
    write_json(output_dir / "qa-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
