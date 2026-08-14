/**
 * Canvas-wide animation + status styles injected once into <style> by
 * Dashboard. Split into named groups so a new status visual or keyframe
 * can be added without touching Dashboard.tsx.
 *
 *   KEYFRAMES              -- @keyframes definitions for edges
 *   EDGE_STATUS_STYLES     -- .react-flow__edge.{selected,executing,...}
 *   buildCanvasStyles()    -- composes the groups for Dashboard
 *
 * Fully static since the design-handoff edge migration: every color,
 * width, and dash rhythm is a theme token (var(--edge-*) + semantic
 * color tokens, declared in themes/base.css), so this module knows
 * nothing about which theme is active and Dashboard injects it once at
 * module scope. The old CanvasStatusColors parameter object (per-theme
 * hex fed from useAppTheme) is gone -- do not reintroduce it; add a
 * token instead.
 *
 * Resting edges follow the design-handoff contract: one pale neutral
 * stroke (--edge-stroke), dashed (--edge-dash), thin
 * (--edge-stroke-width). Status classes recolor with SEMANTIC tokens so
 * execution feedback survives in every theme. No !important on the
 * resting rule (nothing competes once Dashboard stopped inlining a
 * stroke); status rules keep !important so they beat the resting rule
 * and ConditionalEdge's inline conditional-accent styling during runs.
 * The in-progress connection line (.react-flow__connection-path) is
 * styled here too -- Dashboard passes no connectionLineStyle prop.
 *
 * Node execution glow is owned by `client/src/themes/base.css` -- see
 * the `node-pulse` keyframe + `.react-flow__node.executing .node` /
 * `.sq-node[data-executing] .sq-node-box` rules there.
 */

const KEYFRAMES = `
  @keyframes dashFlow {
    0% { stroke-dashoffset: 24; }
    100% { stroke-dashoffset: 0; }
  }
`;

const EDGE_STATUS_STYLES = `
  .react-flow__edge path,
  .react-flow__connection-path {
    stroke: var(--edge-stroke);
    stroke-width: var(--edge-stroke-width);
    stroke-dasharray: var(--edge-dash);
    stroke-linejoin: round;
  }

  .react-flow__edge.selected path {
    stroke: var(--accent) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
  }

  .react-flow__edge.executing path {
    stroke: var(--node-pulse-color) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
    stroke-dasharray: var(--edge-dash-active);
    animation: dashFlow 0.5s linear infinite;
  }

  .react-flow__edge.completed path {
    stroke: var(--success) !important;
    stroke-width: var(--edge-stroke-width-done) !important;
  }

  .react-flow__edge.error path {
    stroke: var(--destructive) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
  }

  .react-flow__edge.pending path {
    stroke: var(--fg-muted) !important;
    stroke-width: var(--edge-stroke-width-done) !important;
    stroke-dasharray: var(--edge-dash-active);
    animation: dashFlow 0.5s linear infinite;
  }

  .react-flow__edge.memory-active path {
    stroke: var(--node-agent) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
  }

  .react-flow__edge.tool-active path {
    stroke: var(--node-tool) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
  }

  .react-flow__edge.skill-active path {
    stroke: var(--node-skill) !important;
    stroke-width: var(--edge-stroke-width-active) !important;
  }

  @media (prefers-reduced-motion: reduce) {
    .react-flow__edge.executing path,
    .react-flow__edge.pending path {
      animation: none !important;
    }
  }
`;

export function buildCanvasStyles(): string {
  return [EDGE_STATUS_STYLES, KEYFRAMES].join('\n');
}
