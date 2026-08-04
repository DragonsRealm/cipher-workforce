# SVG visual review

- Reviewed: `2026-08-04`
- Frozen source commit: `5f7522250eae7b0d02ddfd8ee3740fdf5be4c25e`
- Render targets: `1920 × 1080` and `960 × 540`
- Themes: dark and light
- Authoritative artifacts: `docs/diagrams/v2/{dark,light}/*.svg`

Each final SVG was rendered in a Chromium-based browser at native resolution and at README width. The review checked clipping, text and card collisions, connector-label placement, arrow crossings, hierarchy, theme contrast, and dark/light topology parity.

| Diagram | Dark 1920 | Light 1920 | Dark 960 | Light 960 |
|---|---|---|---|---|
| 01 System context | Pass | Pass | Pass | Pass |
| 02 Runtime and trust topology | Pass | Pass | Pass | Pass |
| 03 Workflow execution routing | Pass | Pass | Pass | Pass |
| 04 Durable deployment and events | Pass | Pass | Pass | Pass |
| 05 Plugin, model, agent, and team composition | Pass | Pass | Pass | Pass |
| 06 Persistence and secret plane | Pass | Pass | Pass | Pass |
| 07 Workspace anatomy | Pass | Pass | Pass | Pass |
| 08 Node Configuration anatomy | Pass | Pass | Pass | Pass |
| 09 Credentials architecture | Pass | Pass | Pass | Pass |
| 10 Team operations | Pass | Pass | Pass | Pass |
| 11 Agent Context versus Memory | Pass | Pass | Pass | Pass |
| 12 Master Skill Editor | Pass | Pass | Pass | Pass |
| 13 Workspace files | Pass | Pass | Pass | Pass |
| 14 Runtime observability dock | Pass | Pass | Pass | Pass |

The final pass found no clipped content or unresolved component/label collisions. Dense architecture views intentionally use orthogonal routes, opaque relationship-label plates, and subtle dashed leaders to preserve direction and readability where routes cross.

AI-generated PNGs were inspected as non-authoritative composition drafts. Generation-specific text or connector defects may remain in those drafts; the matching SVGs are the validated source-backed documentation artifacts.

Temporary browser render outputs are excluded from version control.
