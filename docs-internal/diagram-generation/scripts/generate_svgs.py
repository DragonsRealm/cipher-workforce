"""Generate the OpenCompany v2 SVG suite, prompt pack, and manifest."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "docs-internal" / "diagram-generation"
SPECS = WORK / "specs"
OUT = ROOT / "docs" / "diagrams" / "v2"
SNAPSHOT_COMMIT = "5f7522250eae7b0d02ddfd8ee3740fdf5be4c25e"

THEMES = {
    "dark": {
        "bg": "#0d0f13", "panel": "#15171c", "card": "#1b1e25",
        "text": "#e8eaed", "muted": "#9aa1ac", "border": "#2b2f37",
        "line": "#e8eaed", "label_bg": "#15171c",
    },
    "light": {
        "bg": "#f5f7fa", "panel": "#fafbfc", "card": "#ffffff",
        "text": "#1a1d21", "muted": "#4b5563", "border": "#d1d5db",
        "line": "#374151", "label_bg": "#ffffff",
    },
}

ROLES = {
    "actor": "#f1fa8c", "system": "#bd93f9", "agent": "#bd93f9",
    "model": "#8be9fd", "interface": "#8be9fd", "tool": "#50fa7b",
    "result": "#50fa7b", "trigger": "#ff79c6", "event": "#ff79c6",
    "workflow": "#ffb86c", "runtime": "#ffb86c", "decision": "#ffb86c",
    "data": "#ff79c6", "security": "#f1fa8c", "neutral": "#9aa1ac",
}

COMMON_AVOID = [
    "photorealism", "people or stock imagery", "3D or isometric rendering",
    "glassmorphism or backdrop blur", "rainbow neon or excessive bloom",
    "full-strength accent backgrounds", "decorative emoji", "sci-fi HUD chrome",
    "tiny or illegible text", "crossing arrows", "spaghetti topology",
    "ambiguous bidirectional arrows", "extra boxes, labels, connectors, or logos",
    "watermarks",
]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def load_specs() -> list[dict[str, Any]]:
    specs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(SPECS.glob("*.json"))]
    ids = [spec["id"] for spec in specs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate diagram ids")
    return specs


def endpoint(box: dict[str, Any], side: str) -> tuple[float, float]:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    return {
        "left": (x, y + h / 2), "right": (x + w, y + h / 2),
        "top": (x + w / 2, y), "bottom": (x + w / 2, y + h),
    }[side]


def geometry_signature(spec: dict[str, Any]) -> str:
    payload = {key: spec[key] for key in ("groups", "nodes", "relationships", "callouts")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def fit_text_attrs(text: str, max_width: float, font_size: float, ratio: float = 0.56) -> str:
    """Keep canonical one-line text inside its allotted box at >=16px."""
    if len(text) * font_size * ratio <= max_width:
        return ""
    return f' textLength="{max_width:.1f}" lengthAdjust="spacingAndGlyphs"'


def relationship_label_size(rel: dict[str, Any]) -> tuple[float, float]:
    label = str(rel["label"])
    protocol = f'via {rel["protocol"]}' if rel.get("protocol") else ""
    longest_line = max(len(label), len(protocol))
    return min(360, max(120, longest_line * 7.6 + 24)), (48 if protocol else 30)


def rect_overlap_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return max(0.0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0.0, min(ay + ah, by + bh) - max(ay, by))


def place_relationship_labels(spec: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Nudge label cards away from components and one another.

    Specs provide the semantic anchor.  The renderer searches nearby free
    space and adds a subtle leader whenever the readable label card moves.
    """
    obstacles: list[tuple[float, float, float, float]] = [
        (node["x"] - 8, node["y"] - 8, node["w"] + 16, node["h"] + 16)
        for node in spec["nodes"]
    ]
    obstacles.extend(
        (item["x"] - 8, item["y"] - 8, item["w"] + 16, item["h"] + 16)
        for item in spec.get("callouts", [])
    )
    # Keep labels out of diagram and group headings, and above the legend.
    obstacles.append((0, 0, 1920, 116))
    obstacles.append((0, 998, 1920, 82))
    obstacles.extend(
        (
            g["x"] + 10,
            g["y"] + 8,
            min(g["w"] - 20, len(str(g["label"])) * 10 + 40),
            36,
        )
        for g in spec.get("groups", [])
    )

    placed: list[tuple[float, float, float, float]] = []
    result: dict[str, dict[str, float]] = {}
    dxs = [0, -90, 90, -180, 180, -270, 270, -360, 360, -450, 450]
    dys = [0, -54, 54, -108, 108, -162, 162, -216, 216, -270, 270]
    offsets = sorted(((dx, dy) for dx in dxs for dy in dys), key=lambda p: abs(p[0]) + 1.2 * abs(p[1]))

    for rel in spec.get("relationships", []):
        width, height = relationship_label_size(rel)
        pref_x, pref_y = rel.get("labelAt", [960, 540])
        best: tuple[float, float, float] | None = None
        for dx, dy in offsets:
            cx = min(1908 - width / 2, max(12 + width / 2, pref_x + dx))
            cy = min(990 - height / 2, max(124 + height / 2, pref_y + dy))
            rect = (cx - width / 2, cy - height / 2, width, height)
            overlap = sum(rect_overlap_area(rect, obstacle) for obstacle in obstacles)
            overlap += 2 * sum(rect_overlap_area(rect, previous) for previous in placed)
            distance = abs(cx - pref_x) + 1.2 * abs(cy - pref_y)
            score = overlap * 1000 + distance
            if best is None or score < best[0]:
                best = (score, cx, cy)
            if overlap == 0:
                break
        assert best is not None
        _, cx, cy = best
        rect = (cx - width / 2, cy - height / 2, width, height)
        placed.append(rect)
        obstacles.append(rect)
        result[rel["id"]] = {
            "x": cx, "y": cy, "w": width, "h": height,
            "anchorX": float(pref_x), "anchorY": float(pref_y),
        }
    return result


def render_group(group: dict[str, Any], theme: dict[str, str]) -> str:
    color = ROLES.get(group.get("role", "neutral"), ROLES["neutral"])
    dash = ' stroke-dasharray="8 7"' if group.get("dash") else ""
    return (
        f'<g data-group-id="{esc(group["id"])}">'
        f'<rect x="{group["x"]}" y="{group["y"]}" width="{group["w"]}" height="{group["h"]}" rx="18" '
        f'fill="{theme["panel"]}" stroke="{color}" stroke-width="2"{dash}/>'
        f'<text x="{group["x"] + 20}" y="{group["y"] + 32}" class="group-label">{esc(group["label"])}</text>'
        '</g>'
    )


def render_node(node: dict[str, Any], theme: dict[str, str]) -> str:
    color = ROLES.get(node.get("role", "neutral"), ROLES["neutral"])
    x, y, w, h = node["x"], node["y"], node["w"], node["h"]
    labels = node.get("labelLines") or str(node["label"]).split("\n")
    detail = node.get("detail", [])
    parts = [
        f'<g data-component-id="{esc(node["id"])}">',
        f'<title>{esc(node["label"])}</title>',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{theme["card"]}" stroke="{color}" stroke-width="2"/>',
        f'<rect x="{x}" y="{y}" width="{w}" height="7" rx="4" fill="{color}"/>',
        f'<text x="{x + 18}" y="{y + 26}" class="type-label"{fit_text_attrs(str(node["type"]).upper(), w - 36, 16, .61)}>{esc(node["type"].upper())}</text>',
    ]
    # The compact cards are intentionally information-dense.  These baselines
    # keep one- and two-line labels plus 16px detail text inside 95–155px cards
    # without changing the shared geometry.
    cursor = y + 52
    for line in labels:
        parts.append(f'<text x="{x + 18}" y="{cursor}" class="node-label"{fit_text_attrs(str(line), w - 36, 20)}>{esc(line)}</text>')
        cursor += 22
    cursor += 4
    for line in detail:
        parts.append(f'<text x="{x + 18}" y="{cursor}" class="node-detail"{fit_text_attrs(str(line), w - 36, 16, .54)}>{esc(line)}</text>')
        cursor += 20
    parts.append('</g>')
    return "".join(parts)


def render_relationship(
    rel: dict[str, Any], boxes: dict[str, dict[str, Any]], theme: dict[str, str],
    placement: dict[str, float],
) -> str:
    start = endpoint(boxes[rel["from"]], rel.get("fromSide", "right"))
    end = endpoint(boxes[rel["to"]], rel.get("toSide", "left"))
    points = [start, *[tuple(p) for p in rel.get("waypoints", [])], end]
    path = " ".join(("M" if idx == 0 else "L") + f" {p[0]} {p[1]}" for idx, p in enumerate(points))
    dash = ' stroke-dasharray="10 8"' if rel.get("style") == "dashed" else ""
    label = rel["label"]
    protocol = f'via {rel["protocol"]}' if rel.get("protocol") else ""
    accessible_label = label + (f' · {protocol}' if protocol else "")
    lx, ly, width, height = placement["x"], placement["y"], placement["w"], placement["h"]
    top = ly - height / 2
    label_fit = fit_text_attrs(label, width - 18, 16)
    protocol_fit = fit_text_attrs(protocol, width - 18, 16, .61)
    text = (
        f'<text x="{lx}" y="{ly - 3}" class="edge-label" text-anchor="middle"{label_fit}>{esc(label)}</text>'
        f'<text x="{lx}" y="{ly + 17}" class="edge-protocol" text-anchor="middle"{protocol_fit}>{esc(protocol)}</text>'
        if protocol else
        f'<text x="{lx}" y="{ly + 3}" class="edge-label" text-anchor="middle"{label_fit}>{esc(label)}</text>'
    )
    moved = abs(lx - placement["anchorX"]) + abs(ly - placement["anchorY"]) > 18
    leader = (
        f'<path d="M {placement["anchorX"]} {placement["anchorY"]} L {lx} {ly}" fill="none" '
        f'stroke="{theme["muted"]}" stroke-width="1" stroke-dasharray="3 4" opacity=".7"/>'
        if moved else ""
    )
    return (
        f'<g data-relationship-id="{esc(rel["id"])}">'
        f'<title>{esc(accessible_label)}</title>'
        f'<path d="{path}" fill="none" stroke="{theme["line"]}" stroke-width="2"{dash} marker-end="url(#arrow)"/>'
        f'{leader}'
        f'<rect x="{lx - width / 2}" y="{top}" width="{width}" height="{height}" rx="7" fill="{theme["label_bg"]}" stroke="{theme["border"]}"/>'
        f'{text}'
        '</g>'
    )


def render_callout(callout: dict[str, Any], theme: dict[str, str]) -> str:
    x, y, w, h = callout["x"], callout["y"], callout["w"], callout["h"]
    parts = [
        f'<g data-callout-id="{esc(callout["id"])}">',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{theme["card"]}" stroke="#f1fa8c" stroke-width="2" stroke-dasharray="7 6"/>',
        f'<text x="{x + 16}" y="{y + 30}" class="callout-title">{esc(callout["title"])}</text>',
    ]
    cursor = y + 56
    for line in callout.get("lines", []):
        parts.append(f'<text x="{x + 16}" y="{cursor}" class="callout-text"{fit_text_attrs(str(line), w - 32, 16, .54)}>{esc(line)}</text>')
        cursor += 22
    parts.append('</g>')
    return "".join(parts)


def render_svg(spec: dict[str, Any], theme_name: str) -> str:
    theme = THEMES[theme_name]
    boxes = {item["id"]: item for item in [*spec.get("groups", []), *spec["nodes"]]}
    signature = geometry_signature(spec)
    label_placements = place_relationship_labels(spec)
    body: list[str] = []
    body.extend(render_group(group, theme) for group in spec.get("groups", []))
    body.extend(render_relationship(rel, boxes, theme, label_placements[rel["id"]]) for rel in spec.get("relationships", []))
    body.extend(render_node(node, theme) for node in spec["nodes"])
    body.extend(render_callout(item, theme) for item in spec.get("callouts", []))
    role_labels = [
        ("agent", "Agents / system"), ("model", "Models / interfaces"),
        ("tool", "Tools / results"), ("trigger", "Triggers / events"),
        ("workflow", "Workflow / runtime"), ("security", "Security note"),
    ]
    legend_x = 72
    legend = []
    for role, label in role_labels:
        color = ROLES[role]
        legend.append(f'<circle cx="{legend_x}" cy="1038" r="7" fill="{color}"/><text x="{legend_x + 14}" y="1044" class="legend">{esc(label)}</text>')
        legend_x += 245
    desc = spec["description"] + " Sources: " + "; ".join(ref["path"] for ref in spec["sourceRefs"])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="100%" role="img" aria-labelledby="title desc" data-theme="{theme_name}" data-geometry-signature="{signature}">
<title id="title">{esc(spec["title"])} — {theme_name} theme</title>
<desc id="desc">{esc(desc)}</desc>
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{theme["line"]}"/></marker>
  <style>
    .title {{ fill:{theme["text"]}; font:700 30px Geist,system-ui,sans-serif; }}
    .subtitle {{ fill:{theme["muted"]}; font:400 18px Geist,system-ui,sans-serif; }}
    .group-label {{ fill:{theme["muted"]}; font:600 16px 'JetBrains Mono',ui-monospace,monospace; letter-spacing:1.2px; }}
    .type-label {{ fill:{theme["muted"]}; font:600 16px 'JetBrains Mono',ui-monospace,monospace; letter-spacing:.8px; }}
    .node-label {{ fill:{theme["text"]}; font:650 20px Geist,system-ui,sans-serif; }}
    .node-detail {{ fill:{theme["muted"]}; font:400 16px Geist,system-ui,sans-serif; }}
    .edge-label {{ fill:{theme["text"]}; font:500 16px Geist,system-ui,sans-serif; }}
    .edge-protocol {{ fill:{theme["muted"]}; font:500 16px 'JetBrains Mono',ui-monospace,monospace; }}
    .callout-title {{ fill:{theme["text"]}; font:650 18px Geist,system-ui,sans-serif; }}
    .callout-text {{ fill:{theme["muted"]}; font:400 16px Geist,system-ui,sans-serif; }}
    .legend {{ fill:{theme["muted"]}; font:500 16px Geist,system-ui,sans-serif; }}
  </style>
</defs>
<rect width="1920" height="1080" fill="{theme["bg"]}"/>
<text x="72" y="58" class="title">{esc(spec["title"])}</text>
<text x="72" y="91" class="subtitle">{esc(spec["subtitle"])}</text>
{''.join(body)}
<g aria-label="Legend">{''.join(legend)}</g>
</svg>
'''


def prompt_for(spec: dict[str, Any]) -> str:
    exact = [spec["title"], *[n["label"] for n in spec["nodes"]], *[r["label"] for r in spec["relationships"]]]
    components = "; ".join(f'{n["label"]} ({n["type"]})' for n in spec["nodes"])
    relationships = "\n".join(f'- {r["from"]} → {r["to"]}: "{r["label"]}"' + (f' via {r["protocol"]}' if r.get("protocol") else "") for r in spec["relationships"])
    constraints = [
        "Mixed product and technical audience; understandable without narration",
        "16:9 landscape technical schematic with generous gutters and a clear legend",
        "Every connector is unidirectional, arrow-headed, and explicitly labelled",
        "Use only the listed components, relationships, and exact text",
        "Flat vector-like artwork; crisp rounded cards; no raster screenshots",
        "Geist-style headings with monospace machine labels",
        "Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text",
        "Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations",
        *spec["prompt"].get("constraints", []),
    ]
    avoid = [*COMMON_AVOID, *spec["prompt"].get("avoid", [])]
    return f'''Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: {spec["prompt"]["primaryRequest"]}
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: {components}
Composition/framing: {spec["prompt"]["composition"]}
Directed relationships:
{relationships}
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): {json.dumps(exact, ensure_ascii=False)}
Constraints:
{chr(10).join(f'- {item}' for item in constraints)}
Avoid:
{chr(10).join(f'- {item}' for item in avoid)}'''


def light_edit_prompt(spec: dict[str, Any]) -> str:
    return f'''Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "{spec["title"]}"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.'''


def repair_prompt(spec: dict[str, Any]) -> str:
    return f'''Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "{spec["title"]}"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.'''


def write_prompt_pack(specs: list[dict[str, Any]]) -> None:
    sections = [
        "# OpenCompany GPT Image prompt pack\n",
        "Generated from the source-backed diagram specifications. Use the built-in GPT Image path one asset at a time. Dark drafts are generations; light drafts are edits of their approved dark counterpart. Generated text is non-authoritative—the SVG redraw is canonical.\n",
    ]
    for spec in specs:
        sections.extend([
            f'## {spec["id"]}: {spec["title"]}\n',
            "### Dark generation prompt\n\n```text\n" + prompt_for(spec) + "\n```\n",
            "### Light-theme edit prompt\n\n```text\n" + light_edit_prompt(spec) + "\n```\n",
            "### Targeted repair prompt\n\n```text\n" + repair_prompt(spec) + "\n```\n",
        ])
    (WORK / "prompt-pack.md").write_text("\n".join(sections), encoding="utf-8")


def write_index(specs: list[dict[str, Any]]) -> None:
    rows = ["# OpenCompany v2 diagrams", "", "Source-backed architecture and product-panel diagrams. Dark and light variants share identical geometry. AI-generated drafts and prompts are retained under `docs-internal/diagram-generation/`.", "", "| Diagram | Dark | Light |", "|---|---|---|"]
    for spec in specs:
        rows.append(f'| {spec["title"]} | [dark](dark/{spec["id"]}.svg) | [light](light/{spec["id"]}.svg) |')
    rows.extend(["", "The six existing public diagrams remain unchanged. These v2 assets are additive until separately approved for public-document replacement.", ""])
    (OUT / "README.md").write_text("\n".join(rows), encoding="utf-8")


def main() -> None:
    specs = load_specs()
    if len(specs) != 14:
        raise SystemExit(f"expected 14 specs, found {len(specs)}")
    for theme_name in THEMES:
        (OUT / theme_name).mkdir(parents=True, exist_ok=True)
    for spec in specs:
        for theme_name in THEMES:
            (OUT / theme_name / f'{spec["id"]}.svg').write_text(render_svg(spec, theme_name), encoding="utf-8")
    write_prompt_pack(specs)
    write_index(specs)
    drafts = WORK / "drafts"
    manifest = {
        "schemaVersion": 1,
        "sourceSnapshot": {"commit": SNAPSHOT_COMMIT, "description": "See source-snapshot.md"},
        "diagramCount": len(specs),
        "themes": list(THEMES),
        "visualReview": {
            "status": "requires-review",
            "evidence": "docs-internal/diagram-generation/visual-review.md",
            "renderSizes": ["1920x1080", "960x540"],
            "themes": list(THEMES),
        },
        "diagrams": [
            {
                "id": spec["id"], "title": spec["title"], "diagramType": spec["diagramType"],
                "sourceReview": spec.get("sourceReview", "pending"),
                "components": [node["id"] for node in spec["nodes"]],
                "relationships": [rel["id"] for rel in spec["relationships"]],
                "sourceRefs": spec["sourceRefs"],
                "artifacts": {
                    theme: {
                        "draft": str((drafts / theme / f'{spec["id"]}.png').relative_to(ROOT)).replace("\\", "/"),
                        "svg": str((OUT / theme / f'{spec["id"]}.svg').relative_to(ROOT)).replace("\\", "/"),
                        "draftPresent": (drafts / theme / f'{spec["id"]}.png').exists(),
                        "svgPresent": (OUT / theme / f'{spec["id"]}.svg').exists(),
                    } for theme in THEMES
                },
                "validationStatus": "pending",
            } for spec in specs
        ],
    }
    (WORK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
