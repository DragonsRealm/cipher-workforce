"""Validate diagram specs, theme parity, SVG accessibility, and raster presence."""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "docs-internal" / "diagram-generation"
OUT = ROOT / "docs" / "diagrams" / "v2"
THEMES = ("dark", "light")
NS = {"svg": "http://www.w3.org/2000/svg"}


def png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:24]
    if len(data) == 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return None


def validate_spec(spec: dict) -> list[str]:
    errors: list[str] = []
    node_ids = [n["id"] for n in spec["nodes"]]
    group_ids = [g["id"] for g in spec.get("groups", [])]
    if len(node_ids) != len(set(node_ids)):
        errors.append("duplicate component ids")
    known = set(node_ids + group_ids)
    for rel in spec["relationships"]:
        if rel["from"] not in known or rel["to"] not in known:
            errors.append(f'relationship {rel["id"]} references an unknown endpoint')
        if not rel.get("label"):
            errors.append(f'relationship {rel["id"]} has no label')
    if not spec.get("sourceRefs"):
        errors.append("missing source evidence")
    for ref in spec.get("sourceRefs", []):
        if not (ROOT / ref["path"]).exists():
            errors.append(f'missing source path {ref["path"]}')
    return errors


def validate_svg(spec: dict, theme: str) -> tuple[list[str], str | None]:
    path = OUT / theme / f'{spec["id"]}.svg'
    errors: list[str] = []
    if not path.exists():
        return ["missing SVG"], None
    raw = path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [f"invalid XML: {exc}"], None
    if root.attrib.get("viewBox") != "0 0 1920 1080":
        errors.append("wrong viewBox")
    if root.find("svg:title", NS) is None or root.find("svg:desc", NS) is None:
        errors.append("missing accessible title or description")
    if root.findall(".//svg:image", NS):
        errors.append("embedded raster image found")
    components = {node.attrib.get("data-component-id") for node in root.findall(".//*[@data-component-id]")}
    relationships = {node.attrib.get("data-relationship-id") for node in root.findall(".//*[@data-relationship-id]")}
    if components != {node["id"] for node in spec["nodes"]}:
        errors.append("component ids differ from spec")
    if relationships != {rel["id"] for rel in spec["relationships"]}:
        errors.append("relationship ids differ from spec")
    all_text = " ".join((node.text or "") for node in root.findall(".//svg:text", NS))
    for node in spec["nodes"]:
        for line in node.get("labelLines") or str(node["label"]).split("\n"):
            if line not in all_text:
                errors.append(f'missing label text: {line}')
    for rel in spec["relationships"]:
        if rel["label"] not in all_text:
            errors.append(f'missing relationship label: {rel["label"]}')
    if "font-size" in raw:
        errors.append("inline font-size found; sizes must stay in the audited style block")
    return errors, root.attrib.get("data-geometry-signature")


def main() -> None:
    specs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((WORK / "specs").glob("*.json"))]
    report = {"ok": True, "expectedDiagramCount": 14, "actualDiagramCount": len(specs), "diagrams": []}
    if len(specs) != 14:
        report["ok"] = False
    manifest_path = WORK / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    manifest_by_id = {item["id"]: item for item in manifest.get("diagrams", [])} if manifest else {}
    for spec in specs:
        entry = {"id": spec["id"], "errors": validate_spec(spec), "themes": {}}
        signatures = []
        for theme in THEMES:
            svg_errors, signature = validate_svg(spec, theme)
            draft = WORK / "drafts" / theme / f'{spec["id"]}.png'
            draft_error = None
            dimensions = None
            if not draft.exists():
                draft_error = "missing PNG draft"
            else:
                dimensions = png_size(draft)
                if dimensions is None:
                    draft_error = "draft is not a valid PNG"
            entry["themes"][theme] = {
                "svgErrors": svg_errors, "geometrySignature": signature,
                "draftError": draft_error, "draftDimensions": dimensions,
            }
            entry["errors"].extend(f"{theme}: {err}" for err in svg_errors)
            if draft_error:
                entry["errors"].append(f"{theme}: {draft_error}")
            signatures.append(signature)
        if len(set(signatures)) != 1:
            entry["errors"].append("dark/light geometry signatures differ")
        entry["ok"] = not entry["errors"]
        report["ok"] = report["ok"] and entry["ok"]
        report["diagrams"].append(entry)
        if spec["id"] in manifest_by_id:
            manifest_by_id[spec["id"]]["validationStatus"] = "validated" if entry["ok"] else "failed"
    (WORK / "validation-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if manifest:
        manifest["validationStatus"] = "validated" if report["ok"] else "failed"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if not report["ok"]:
        raise SystemExit("diagram validation failed; see validation-report.json")


if __name__ == "__main__":
    main()
