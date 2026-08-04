# OpenCompany GPT Image prompt pack

Generated from the source-backed diagram specifications. Use the built-in GPT Image path one asset at a time. Dark drafts are generations; light drafts are edits of their approved dark counterpart. Generated text is non-authoritative—the SVG redraw is canonical.

## 01-system-context: OpenCompany System Context

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a precise C4 system-context diagram with OpenCompany centered inside a clearly labelled system boundary, two human actor cards on the left, and six external-system cards stacked on the right.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Workflow authors (person); Workflow operators (person); OpenCompany (software system); LLM providers (external system); Productivity services (external system); Messaging channels (external system); Connected devices (external system); Developer services (external system); Search and vector stores (external system)
Composition/framing: Left-to-right context map. Keep OpenCompany visually dominant in the middle. Route the two inbound human relationships into the left edge of OpenCompany and fan six labelled outbound relationships to a clean right-hand stack. Put the system-responsibility note beneath the central card.
Directed relationships:
- workflow-authors → opencompany: "Design and run automations" via Browser UI / CLI
- workflow-operators → opencompany: "Control deployed workflows" via Browser UI / WebSocket
- opencompany → llm-providers: "Requests inference" via Provider SDK / HTTPS
- opencompany → productivity-services: "Reads and updates work" via OAuth / HTTPS
- opencompany → messaging-channels: "Sends and receives messages" via Webhooks / RPC
- opencompany → connected-devices: "Invokes device actions" via RPC / platform APIs
- opencompany → developer-services: "Uses code and dev tools" via MCP / CLI / HTTPS
- opencompany → search-vector-stores: "Indexes and retrieves knowledge" via HTTPS / vector API
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["OpenCompany System Context", "Workflow authors", "Workflow operators", "OpenCompany", "LLM providers", "Productivity services", "Messaging channels", "Connected devices", "Developer services", "Search and vector stores", "Design and run automations", "Control deployed workflows", "Requests inference", "Reads and updates work", "Sends and receives messages", "Invokes device actions", "Uses code and dev tools", "Indexes and retrieves knowledge"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Treat OpenCompany as one software system at this abstraction level
- Show no internal containers or databases
- Keep all external systems outside the OpenCompany boundary
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- vendor logos
- unlisted integrations
- internal microservice detail
- bidirectional arrows
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "OpenCompany System Context"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "OpenCompany System Context"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 02-runtime-trust-topology: Runtime and Trust Topology

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Draw a deployment-aware runtime and trust topology for OpenCompany, showing clients on the left, the application and its security surfaces in the center, and Temporal plus lazy helper runtimes on the right.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Browser SPA (client); OpenCompany CLI (client); Webhook senders (external); Vite development server (container); FastAPI application (container); Protected REST API (interface); /ws/status (websocket); /ws/internal (websocket); /mcp/ide/mcp (MCP endpoint); /webhook/* (HTTP ingress); Production SPA assets (static content); Temporal server and worker (durable runtime); Node.js executor (lazy sidecar); WhatsApp RPC (lazy sidecar)
Composition/framing: Use three vertical trust zones. Place Vite and FastAPI high in the application zone, API and WebSocket surfaces in the middle, MCP/webhook/static surfaces below, and the trust-rules note at the bottom. Clearly distinguish development proxying from production SPA serving.
Directed relationships:
- browser-spa → vite-dev: "Loads SPA in development" via HTTP :5678
- vite-dev → fastapi-app: "Proxies /api /ws /webhook /health /mcp" via HTTP / WS :5679
- opencompany-cli → fastapi-app: "Launches and administers" via Typer / process
- browser-spa → rest-surface: "Calls protected routes" via HTTPS + JWT cookie
- browser-spa → status-socket: "Commands and receives status" via WebSocket + cookie
- opencompany-cli → mcp-surface: "Invokes scoped tools" via MCP over HTTP + Bearer
- webhook-senders → webhook-ingress: "Delivers provider events" via HTTPS webhook
- rest-surface → fastapi-app: "Dispatches request" via ASGI
- status-socket → fastapi-app: "Dispatches authenticated message" via ASGI WebSocket
- temporal-runtime → internal-socket: "Calls legacy node handlers" via WebSocket
- fastapi-app → temporal-runtime: "Starts workflows and polls tasks" via Temporal gRPC :5681
- fastapi-app → nodejs-sidecar: "Starts on demand and executes JavaScript" via HTTP :5682
- fastapi-app → whatsapp-sidecar: "Starts on demand and sends commands" via RPC :5683
- fastapi-app → production-spa: "Serves production fallback" via StaticFiles / HTML
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Runtime and Trust Topology", "Browser SPA", "OpenCompany CLI", "Webhook senders", "Vite development server", "FastAPI application", "Protected REST API", "/ws/status", "/ws/internal", "/mcp/ide/mcp", "/webhook/*", "Production SPA assets", "Temporal server and worker", "Node.js executor", "WhatsApp RPC", "Loads SPA in development", "Proxies /api /ws /webhook /health /mcp", "Launches and administers", "Calls protected routes", "Commands and receives status", "Invokes scoped tools", "Delivers provider events", "Dispatches request", "Dispatches authenticated message", "Calls legacy node handlers", "Starts workflows and polls tasks", "Starts on demand and executes JavaScript", "Starts on demand and sends commands", "Serves production fallback"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Show exact ports 5678, 5679, 5680, 5681, 5682 and 5683 only where specified
- Use a dashed line only for the legacy Temporal-to-/ws/internal path
- Make public, authenticated, bearer-scoped and internal-restricted boundaries visually distinct
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- Docker or Kubernetes assumptions
- microservices not present in the repository
- a public /ws/internal interpretation
- a removed /api/workflow/execute-node route
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Runtime and Trust Topology"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Runtime and Trust Topology"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 03-workflow-execution-routing: Workflow Execution Routing

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a source-accurate dynamic execution-routing diagram showing a direct single-node path, a whole-workflow routing decision with three ordered branches, and a shared six-stage NodeExecutor pipeline.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Single-node request (entry point); Whole-workflow run (entry point); WorkflowService routing (decision); Temporal branch (durable orchestrator); Redis-parallel branch (graph orchestrator); Sequential fallback (graph orchestrator); Prepare parameters (pipeline stage); Resolve templates (pipeline stage); Dispatch plugin operation (pipeline stage); Validated execution result (pipeline stage); Output store (persistence); Status broadcasts (observable result)
Composition/framing: Use three vertical bands: entries, graph orchestration, and NodeExecutor pipeline. Put the direct single-node connector above the graph router. Stack Temporal, Redis-parallel, and sequential branches in priority order. Arrange the pipeline as a compact two-column serpentine flow ending in output storage and status broadcasts.
Directed relationships:
- single-node-request → parameter-preparation: "Executes directly" via WorkflowService.execute_node
- whole-workflow-request → routing-decision: "Submits nodes, edges and identity" via WorkflowService.execute_workflow
- routing-decision → temporal-branch: "Chooses when requested and wired" via Temporal
- routing-decision → redis-parallel-branch: "Else chooses when parallel and Redis enabled" via in-process + Redis
- routing-decision → sequential-branch: "Otherwise falls back" via in-process
- temporal-branch → parameter-preparation: "Schedules node activities" via Temporal task queue
- redis-parallel-branch → parameter-preparation: "Calls node adapter with retries" via async callback
- sequential-branch → parameter-preparation: "Calls each ready node" via await
- parameter-preparation → template-resolution: "Passes validated parameters" via Python dict
- template-resolution → plugin-dispatch: "Builds handler context" via NodeContext
- plugin-dispatch → validated-result: "Returns standardized result" via ExecutionResult
- validated-result → output-store: "Persists successful outputs" via database callback
- output-store → status-broadcast: "Publishes execution state" via WebSocket broadcast
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Workflow Execution Routing", "Single-node request", "Whole-workflow run", "WorkflowService routing", "Temporal branch", "Redis-parallel branch", "Sequential fallback", "Prepare parameters", "Resolve templates", "Dispatch plugin operation", "Validated execution result", "Output store", "Status broadcasts", "Executes directly", "Submits nodes, edges and identity", "Chooses when requested and wired", "Else chooses when parallel and Redis enabled", "Otherwise falls back", "Schedules node activities", "Calls node adapter with retries", "Calls each ready node", "Passes validated parameters", "Builds handler context", "Returns standardized result", "Persists successful outputs", "Publishes execution state"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Preserve the routing precedence Temporal requested+wired, else parallel+Redis, else sequential
- Show that the single-node request bypasses graph routing
- Show all graph branches converging on the same NodeExecutor pipeline
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- claiming Redis is the executor itself
- claiming Temporal is always mandatory
- a removed public execute-node REST route
- parallel arrows without labels
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Workflow Execution Routing"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Workflow Execution Routing"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 04-durable-deployment-events: Durable Deployment and Events

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Build a deployment lifecycle and event-routing diagram that connects operator controls, revision-guarded generation snapshots, a long-lived Temporal controller, push/poll/cron trigger paths, and durable child graph runs.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Start · Pause · Resume · Reset (operator controls); WorkflowControlService (control service); Generation control row (durable data); WorkflowControlWorkflow (long-lived workflow); Local admission state (process projection); Push trigger (trigger definition); Poll trigger (trigger definition); Cron trigger (trigger definition); WorkflowEvent (CloudEvents 1.0); dispatch.emit (event router); Trigger registration (control signal); Child graph workflow (Temporal workflow); Cron action workflow (Temporal workflow); Run status and events (observable result)
Composition/framing: Use three vertical planes: control, trigger/event, and durable execution. Flow lifecycle commands and generation data down the left, push/poll/cron sources across the center, and child/cron workflows plus status on the right. Make the controller the visual hub without crossing connectors.
Directed relationships:
- lifecycle-controls → workflow-control-service: "Requests lifecycle transition" via WebSocket command
- workflow-control-service → generation-snapshot: "Allocates or CAS-updates" via workflow database
- generation-snapshot → controller: "Starts with immutable graph snapshot" via Temporal gRPC
- workflow-control-service → controller: "Updates pause/resume or signals reset" via Temporal Update / Signal
- workflow-control-service → local-runtime-state: "Projects admission state" via in-process
- push-trigger → cloudevent: "Normalizes inbound event" via CloudEvents 1.0
- cloudevent → event-dispatch: "Emits envelope" via async dispatch
- event-dispatch → controller: "Signals matching controller" via Visibility + on_event
- poll-trigger → controller: "Registers durable poll loop" via register_trigger signal
- cron-trigger → cron-action-workflow: "Fires scheduled action" via Temporal Schedule
- trigger-registration → controller: "Registers definitions and pause state" via Temporal Signal
- controller → child-graph-workflow: "Spawns admitted event or poll run" via Temporal child workflow
- cron-action-workflow → child-graph-workflow: "Launches snapshotted graph" via Temporal child workflow
- child-graph-workflow → execution-status: "Persists and broadcasts run state" via database + WebSocket
- cron-action-workflow → execution-status: "Reports scheduled run state" via Temporal + WebSocket
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Durable Deployment and Events", "Start · Pause · Resume · Reset", "WorkflowControlService", "Generation control row", "WorkflowControlWorkflow", "Local admission state", "Push trigger", "Poll trigger", "Cron trigger", "WorkflowEvent", "dispatch.emit", "Trigger registration", "Child graph workflow", "Cron action workflow", "Run status and events", "Requests lifecycle transition", "Allocates or CAS-updates", "Starts with immutable graph snapshot", "Updates pause/resume or signals reset", "Projects admission state", "Normalizes inbound event", "Emits envelope", "Signals matching controller", "Registers durable poll loop", "Fires scheduled action", "Registers definitions and pause state", "Spawns admitted event or poll run", "Launches snapshotted graph", "Persists and broadcasts run state", "Reports scheduled run state"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Show graph_snapshot as immutable per generation
- Show push events as CloudEvents dispatched through Visibility and on_event
- Keep cron as a Temporal Schedule path distinct from controller-owned push/poll paths
- Use dashed styling only for optional or legacy reporting/fallback semantics
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- one workflow per idle trigger
- mutable live canvas as the deployed graph source
- polling through a cron schedule
- unlabelled signals
- Kubernetes or queue infrastructure not present in source
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Durable Deployment and Events"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Durable Deployment and Events"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 05-plugin-agent-team-composition: Plugin → Agent → Team Composition

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a precise three-layer composition diagram that explains how self-registering plugins become chat-model and agent nodes, how an agent assembles context, skills, and tools before LLM dispatch, and how a team lead converts connected teammates into durable delegation state.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: BaseNode plugin contract (plugin contract); Chat-model plugins (model plugin); ChatUnifier (LLM facade); Agent plugins (agent plugin); Context (connected config); Skills (connected capability); Tools (connected capability); Prepared agent call (runtime payload); Orchestrator / AI Employee (team-lead agent); Connected teammate agents (agent set); Task Manager (intrinsic tool); Durable team state (execution data)
Composition/framing: Three framed columns from left to right: Plugin & Model Layer, Agent Composition, and Team Composition. Keep the BaseNode-to-model/agent branching explicit, arrange Context/Skills/Tools as a capability row feeding one prepared agent payload, and show team lead, teammates, intrinsic Task Manager, and durable team state as a clean vertical composition.
Directed relationships:
- base-node → model-plugins: "specializes as model plugin"
- base-node → agent-plugins: "specializes as agent plugin"
- model-plugins → chat-unifier: "dispatches chat" via provider + model
- agent-plugins → prepared-agent-call: "prepares execution payload"
- context-node → prepared-agent-call: "adds context descriptor"
- skill-nodes → prepared-agent-call: "injects enabled skill content"
- tool-nodes → prepared-agent-call: "builds tool surface"
- prepared-agent-call → chat-unifier: "routes selected provider and model"
- teammates → team-lead: "connect via input-teammates"
- team-lead → prepared-agent-call: "executes through shared agent runtime"
- team-lead → task-manager: "auto-binds intrinsic tool"
- teammates → durable-team: "registers execution members"
- task-manager → durable-team: "persists task lifecycle"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Plugin → Agent → Team Composition", "BaseNode plugin contract", "Chat-model plugins", "ChatUnifier", "Agent plugins", "Context", "Skills", "Tools", "Prepared agent call", "Orchestrator / AI Employee", "Connected teammate agents", "Task Manager", "Durable team state", "specializes as model plugin", "specializes as agent plugin", "dispatches chat", "prepares execution payload", "adds context descriptor", "injects enabled skill content", "builds tool surface", "routes selected provider and model", "connect via input-teammates", "executes through shared agent runtime", "auto-binds intrinsic tool", "registers execution members", "persists task lifecycle"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Do not draw standalone chat-model nodes as a required input to ordinary agents; agents select provider and model in their own Params
- Show every capability and teammate connection as a directed labelled edge
- Keep the shared ChatUnifier visible for both standalone model execution and agent execution
- Treat Task Manager as intrinsic to team leads, not a palette-connected tool
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- class-count claims not shown in source
- implying teammate-to-teammate communication is required for composition
- unlabelled containment lines
- bidirectional arrows
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Plugin → Agent → Team Composition"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Plugin → Agent → Team Composition"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 06-persistence-secret-plane: Persistence & Secret Plane

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a two-plane data architecture diagram that makes the separation between workflow.db operational persistence and the encrypted credentials.db secret plane unmistakable, including the real service boundaries and startup-derived encryption key path.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Runtime services (application callers); Database (persistence service); workflow.db (SQLite data store); Plugin credentials (secret consumers); AuthService (secret facade); Process caches (bounded secret cache); CredentialsDatabase (encrypted persistence); API_KEY_ENCRYPTION_KEY (server configuration); EncryptionService (cryptographic boundary); credentials.db (encrypted SQLite)
Composition/framing: Use two large side-by-side framed planes. The left plane is a short Runtime Services → Database → workflow.db flow with a blast-radius note. The right plane flows Plugin Credentials → AuthService → CredentialsDatabase → encrypted credentials.db, with a side cache, a server key feeding EncryptionService, and a refresh-token warning near the cache.
Directed relationships:
- runtime-services → database-service: "persists operational state"
- database-service → workflow-db: "reads and writes SQLModel rows" via sqlite+aiosqlite
- credential-callers → auth-service: "requests secret resolution"
- auth-service → credential-caches: "populates safe cache"
- auth-service → credentials-database: "save · retrieve · delete"
- encryption-password → encryption-service: "derives in-memory Fernet key"
- credentials-database → encryption-service: "calls encrypt / decrypt per field"
- credentials-database → credentials-db: "stores ciphertext rows and salt" via SQLite
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Persistence & Secret Plane", "Runtime services", "Database", "workflow.db", "Plugin credentials", "AuthService", "Process caches", "CredentialsDatabase", "API_KEY_ENCRYPTION_KEY", "EncryptionService", "credentials.db", "persists operational state", "reads and writes SQLModel rows", "requests secret resolution", "populates safe cache", "save · retrieve · delete", "derives in-memory Fernet key", "calls encrypt / decrypt per field", "stores ciphertext rows and salt"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Keep workflow.db and credentials.db visually separate
- Show AuthService as the single service-level credential access surface
- Show the API_KEY_ENCRYPTION_KEY and persisted salt as inputs to the encrypted-store design
- State that OAuth refresh tokens bypass the process cache
- Do not imply plaintext secrets are persisted
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- showing the unused credential backend factory as the active runtime path
- placing API keys in workflow.db
- plaintext secret labels inside credentials.db
- bidirectional arrows
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Persistence & Secret Plane"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Persistence & Secret Plane"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 07-workspace-anatomy: Workspace Anatomy

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a filesystem anatomy diagram showing how an immutable workflow ID resolves through workflow.db to a mutable slug-keyed workspace, what lives inside that workspace, and which HTTP, filesystem/media, and CLI-agent consumers access it through containment controls.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Workflow record (database identity); Workspace locator (path resolver); DATA_DIR / workspaces (filesystem root); workspaces/<workflow.slug>/ (workspace root); Workspace files (runtime artifacts); .claude/skills/<name>/ (materialized skills); <node_id>/wt_<task_id>/ (CLI worktree); Workspace HTTP routes (interface); Filesystem & media nodes (contained I/O); CLI agent session (agent consumer)
Composition/framing: Use three framed columns: Identity & Root Resolution, Per-Workflow Workspace, and Contained Consumers. Show the id→slug resolver as a vertical chain on the left; place the slug directory at the top of the middle frame with files, .claude skills, and per-task CLI worktrees below; stack HTTP routes, filesystem/media nodes, and the CLI agent on the right.
Directed relationships:
- workflow-record → workspace-locator: "lookup by immutable workflow id"
- workspace-locator → workspace-base: "selects mutable slug below root"
- workspace-base → workflow-workspace: "contains one slug directory"
- workflow-workspace → workspace-files: "contains runtime artifacts"
- workflow-workspace → workspace-skills: "contains isolated skill tree"
- workflow-workspace → cli-worktrees: "contains per-node task worktrees"
- workspace-routes → workflow-workspace: "resolves workflow id before file access" via workspace locator
- filesystem-media → workspace-files: "reads and writes contained paths"
- workspace-skills → cli-agent-session: "auto-discovers enabled skills" via --add-dir
- workflow-workspace → cli-agent-session: "passes workspace_dir"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Workspace Anatomy", "Workflow record", "Workspace locator", "DATA_DIR / workspaces", "workspaces/<workflow.slug>/", "Workspace files", ".claude/skills/<name>/", "<node_id>/wt_<task_id>/", "Workspace HTTP routes", "Filesystem & media nodes", "CLI agent session", "lookup by immutable workflow id", "selects mutable slug below root", "contains one slug directory", "contains runtime artifacts", "contains isolated skill tree", "contains per-node task worktrees", "resolves workflow id before file access", "reads and writes contained paths", "auto-discovers enabled skills", "passes workspace_dir"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Label the on-disk directory with workflow.slug, never workflow.id
- State that stable references and routes carry the immutable workflow id
- Show containment for relative paths and symlink/junction escapes
- Keep .claude/skills inside the per-workflow workspace
- Show CLI worktrees under the same workspace root
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- placing shipped example workflows under DATA_DIR
- using a workflow id as the workspace directory name
- showing unrestricted host filesystem access
- bidirectional arrows
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Workspace Anatomy"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Workspace Anatomy"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 08-node-configuration-anatomy: Node Configuration Anatomy

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create an annotated product-panel anatomy of the Node Configuration modal with its real header controls, three proportional columns, and the live client/server data flows that drive configuration, execution, and output.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Node identity (modal header); Save (header action); Run (header action); Cancel / Stop (header action); Close (X) (modal action); Input Data & Variables (left column · 0.7); Parameters (middle column · 1.6); Output (right column · 0.7); NodeSpec metadata (schema source); Parameter query + draft (client state); ExecutionService (per-node runtime); WebSocket + workflow.db (live data plane)
Composition/framing: Use one wide modal frame above a shallow live-data frame. In the modal header place Node identity, Save, Run, Cancel / Stop, and Close. Beneath it, preserve the visibly narrow-wide-narrow 0.7 / 1.6 / 0.7 Input Data & Variables, Parameters, and Output columns. Align NodeSpec, parameter draft/query, ExecutionService, and WebSocket + workflow.db cards below their consumers.
Directed relationships:
- node-spec → node-identity: "supplies icon and display name"
- node-spec → parameters-column: "renders fields and panel variants"
- input-column → parameters-column: "drag template variable"
- parameters-column → parameter-state: "onParameterChange updates local draft"
- save-action → parameter-state: "handleSave commits draft"
- run-action → execution-service: "save if dirty, then execute node"
- execution-service → output-column: "appends correlated ExecutionResult"
- websocket-database → output-column: "broadcasts node status and output" via execution_id
- cancel-action → websocket-database: "cancel wait or close draft"
- parameter-state → websocket-database: "save_node_parameters" via TanStack Query + WS
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Node Configuration Anatomy", "Node identity", "Save", "Run", "Cancel / Stop", "Close (X)", "Input Data & Variables", "Parameters", "Output", "NodeSpec metadata", "Parameter query + draft", "ExecutionService", "WebSocket + workflow.db", "supplies icon and display name", "renders fields and panel variants", "drag template variable", "onParameterChange updates local draft", "handleSave commits draft", "save if dirty, then execute node", "appends correlated ExecutionResult", "broadcasts node status and output", "cancel wait or close draft", "save_node_parameters"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Preserve the 0.7 / 1.6 / 0.7 column hierarchy
- Show Save, Run, Cancel / Stop, and Close as distinct controls
- Represent NodeSpec uiHints as the source of hidden sections and specialized middle panels
- Show the local edit buffer separately from persisted node_parameters
- Show output correlation by execution_id
- Keep all relationships directed and labelled
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- invented toolbar controls
- a generic settings form unrelated to the real panel
- equal-width content columns
- unlabelled arrows
- bidirectional arrows
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Node Configuration Anatomy"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Node Configuration Anatomy"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 09-credentials-architecture: Credentials Architecture

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Show the current OpenCompany credential architecture as a security-first flow from the Credentials Modal through WebSocket handlers and declarative credential registries to AuthService, encrypted SQLite, and client-invalidating status broadcasts.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Credentials Modal (UI); WebSocket + Query State (CLIENT); Credential WS Handlers (ROUTER); Credential Registries (REGISTRY); Status Broadcaster (EVENTS); AuthService (SECURITY SERVICE); Encryption Service (CRYPTO); credentials.db (ENCRYPTED SQLITE)
Composition/framing: Two clearly bounded lanes: a compact client control plane on the left and a larger server security plane on the right. Read primarily left-to-right, then down through AuthService to encrypted storage, with one dashed broadcast return path across the top. Keep the API-key versus OAuth-token split visible as a security callout.
Directed relationships:
- credentials-modal → client-state: "panel actions + cached status" via React Query
- client-state → ws-handlers: "catalogue + credential RPC" via WebSocket
- ws-handlers → credential-registries: "dispatch validation"
- ws-handlers → auth-service: "get · save · delete"
- credential-registries → auth-service: "store valid probe result"
- credential-registries → status-broadcaster: "publish validation status"
- auth-service → status-broadcaster: "catalogue version + mutation event"
- auth-service → credentials-db: "canonical encrypted CRUD"
- credentials-db → encryption-service: "encrypt / decrypt payloads" via Fernet
- status-broadcaster → client-state: "push status + invalidate catalogue" via WebSocket
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Credentials Architecture", "Credentials Modal", "WebSocket + Query State", "Credential WS Handlers", "Credential Registries", "Status Broadcaster", "AuthService", "Encryption Service", "credentials.db", "panel actions + cached status", "catalogue + credential RPC", "dispatch validation", "get · save · delete", "store valid probe result", "publish validation status", "catalogue version + mutation event", "canonical encrypted CRUD", "encrypt / decrypt payloads", "push status + invalidate catalogue"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- AuthService must be the only plaintext credential access boundary
- Show API keys and OAuth tokens as distinct non-interchangeable records
- Show CredentialsDatabase calling Fernet encryption with PBKDF2-SHA256-derived process key
- Show status broadcasts invalidating the client catalogue without exposing secret values
- Never render example API keys, access tokens, refresh tokens, or plaintext secrets
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- implying the browser stores plaintext secrets persistently
- merging API keys and OAuth tokens into one cache or record
- showing routers accessing CredentialsDatabase directly
- depicting refresh tokens inside the in-memory cache
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Credentials Architecture"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Credentials Architecture"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 10-team-operations: Team Operations

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Explain OpenCompany team operations as one integrated schematic: the team lead's intrinsic Task Manager and human panels feed an execution-scoped AgentTeamService, then a durable task state machine makes worker submission, lead acceptance, retries, reassignment, cancellation, and final team completion explicit.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Team Lead (AGENT); Task Manager (INTRINSIC TOOL); AgentTeamService (DURABLE SERVICE); Task Manager + Team Monitor (HUMAN UI); Blocked (TASK STATE); Queued (TASK STATE); Running (TASK STATE); Submitted (TASK STATE); Accepted (TASK STATE); Failed (TASK STATE); Cancelled (TASK STATE); Team Completed (EXECUTION STATE)
Composition/framing: A narrow top control band contains Team Lead, Task Manager, AgentTeamService, and the human Task Manager plus Team Monitor. A large lower band contains a left-to-right lifecycle from Blocked through Queued, Running, Submitted, and Accepted, with Failed and Cancelled recovery states below and Team Completed at the lower right. Route recovery arrows around the main path and keep every arrow label readable.
Directed relationships:
- team-lead → task-manager: "call public operations"
- task-manager → agent-team-service: "scoped durable mutation"
- human-views → agent-team-service: "inspect + permitted review actions" via WebSocket
- agent-team-service → human-views: "snapshots + lifecycle events"
- agent-team-service → queued: "assign_task persists ready work"
- blocked → queued: "dependencies accepted"
- queued → running: "worker claims task"
- running → submitted: "worker succeeds"
- submitted → accepted: "accept_task"
- running → failed: "worker errors"
- failed → queued: "retry_task / reassign_task"
- submitted → queued: "retry_task / reassign_task"
- cancelled → queued: "retry_task / reassign_task"
- accepted → team-completed: "finish_team when all resolved"
- cancelled → team-completed: "cancelled also counts resolved"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Team Operations", "Team Lead", "Task Manager", "AgentTeamService", "Task Manager + Team Monitor", "Blocked", "Queued", "Running", "Submitted", "Accepted", "Failed", "Cancelled", "Team Completed", "call public operations", "scoped durable mutation", "inspect + permitted review actions", "snapshots + lifecycle events", "assign_task persists ready work", "dependencies accepted", "worker claims task", "worker succeeds", "accept_task", "worker errors", "retry_task / reassign_task", "retry_task / reassign_task", "retry_task / reassign_task", "finish_team when all resolved", "cancelled also counts resolved"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Submitted must be visibly distinct from Accepted and must not be labelled Done
- Only accepted and cancelled tasks satisfy finish_team
- Show retry and reassign returning failed, submitted, or cancelled work to Queued
- Show optimistic revision-checked mutations at AgentTeamService
- Team Monitor must be described as read-only even though Task Manager UI can mutate
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- equating worker completion with lead acceptance
- showing arbitrary non-connected agents as valid assignees
- a single undifferentiated completed state
- implying one sibling failure cancels every sibling
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Team Operations"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Team Operations"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 11-agent-context-memory: Agent Context vs. Simple Memory

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Create a precise side-by-side comparison showing that Agent Context and Simple Memory are separate systems around the same Agent Runtime: Context automatically journals and replays exact execution state, while Simple Memory is an explicit tool for versioned durable facts chosen by the model or a human.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Context Node (POLICY); AgentContextStore (EXACT JOURNAL); Replay Checkpoint (CONTEXT STATE); Agent Runtime (SHARED CONSUMER); Simple Memory (LOCKED TOOL); MemoryToolStore (DURABLE DATA); Facts & Decisions (MEMORY ITEMS)
Composition/framing: Mirror two wide bounded lanes around one central Agent Runtime. The left Context lane flows from policy to exact journal to replay checkpoint and back into the runtime. The right Memory lane flows from the explicit tool to a scoped durable store to recalled facts and back into the runtime. Use symmetric geometry so the conceptual contrast is immediate.
Directed relationships:
- context-node → agent-runtime: "bind policy at input-context"
- agent-runtime → context-journal: "append exact observable events"
- context-journal → context-checkpoint: "compact at context pressure"
- context-checkpoint → agent-runtime: "replay checkpoint + exact tail"
- memory-node → agent-runtime: "register tool at input-tools"
- agent-runtime → memory-store: "remember / recall / update / forget"
- memory-store → memory-items: "persist isolated versioned items"
- memory-items → agent-runtime: "return selected recalled items"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Agent Context vs. Simple Memory", "Context Node", "AgentContextStore", "Replay Checkpoint", "Agent Runtime", "Simple Memory", "MemoryToolStore", "Facts & Decisions", "bind policy at input-context", "append exact observable events", "compact at context pressure", "replay checkpoint + exact tail", "register tool at input-tools", "remember / recall / update / forget", "persist isolated versioned items", "return selected recalled items"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Label Context connection as output-context to input-context and Memory connection as output-tool to input-tools
- Context must be automatic, exact, provider-bound, and system managed
- Simple Memory must expose remember, recall, list, get, update, and forget as explicit operations
- Memory items must never appear as automatically injected transcript replay
- Clearing either system must not imply clearing the other
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- calling Simple Memory conversation history
- showing Context as a model-invoked tool
- merging Context journal records with Memory items
- implying manual or generation-zero runs always create durable Context
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Agent Context vs. Simple Memory"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Agent Context vs. Simple Memory"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 12-master-skill-editor: Master Skill Editor

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Draw a source-backed product-panel schematic of the Master Skill Editor, showing its split skill catalogue and instructions editor, the built-in and user skill sources, the persisted skills_config, expansion into the connected Agent Skill runtime, and the safe runtime-status feedback loop.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Skill WebSocket handlers (service); Built-in SKILL.md (source); User skill store (database); TanStack skill caches (client state); Skills catalogue pane (interface); Instructions editor pane (interface); Catalogue projection (view model); Master Skill skills_config (node params); Master Skill expander (runtime); Agent Skill runtime (tool runtime); Exact-node status projection (observability)
Composition/framing: Use three clearly bounded vertical zones matching the supplied coordinates: source handlers on the left, the split editor and its view/config state in the center, and the connected agent runtime on the right. Keep the bottom status-feedback route outside the cards and reserve the final band for the invariant callout.
Directed relationships:
- skill-ws-handlers → builtin-skill-files: "scan and load metadata + instructions" via filesystem
- skill-ws-handlers → user-skill-store: "list and mutate custom skills" via database
- skill-ws-handlers → tanstack-skill-cache: "query results and lifecycle invalidation" via WebSocket
- tanstack-skill-cache → query-projection: "cached folder, user, and content records"
- query-projection → skill-catalogue-pane: "render and filter rows"
- skill-catalogue-pane → instructions-editor-pane: "select skill"
- skill-catalogue-pane → skills-config: "toggle enabled / keep required"
- instructions-editor-pane → skills-config: "write instructions + customized flag"
- instructions-editor-pane → skill-ws-handlers: "create / update / delete user skill" via WebSocket
- skills-config → master-skill-expander: "expand enabled entries"
- master-skill-expander → agent-skill-runtime: "descriptors + Skill tool catalogue"
- agent-skill-runtime → skill-status-projection: "loading / loaded / used / failed" via node status + CloudEvents
- skill-status-projection → skill-catalogue-pane: "project safe state onto matching row"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Master Skill Editor", "Skill WebSocket handlers", "Built-in SKILL.md", "User skill store", "TanStack skill caches", "Skills catalogue pane", "Instructions editor pane", "Catalogue projection", "Master Skill skills_config", "Master Skill expander", "Agent Skill runtime", "Exact-node status projection", "scan and load metadata + instructions", "list and mutate custom skills", "query results and lifecycle invalidation", "cached folder, user, and content records", "render and filter rows", "select skill", "toggle enabled / keep required", "write instructions + customized flag", "create / update / delete user skill", "expand enabled entries", "descriptors + Skill tool catalogue", "loading / loaded / used / failed", "project safe state onto matching row"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Show the Skills catalogue pane and Instructions editor pane as equal peers in one editor surface
- Keep built-in SKILL.md content distinct from database-backed user skills
- Show skills_config as the authoritative enabled/instructions configuration, not a runtime log
- Show the runtime feedback arrow returning to the matching catalogue row
- Use the exact handler/cache/config/runtime labels supplied in the spec
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- generic prompt-engineering imagery
- implying every skill body is eagerly injected into the system prompt
- showing runtime events that contain instruction text or secrets
- merging built-in files and user skills into one storage box
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Master Skill Editor"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Master Skill Editor"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 13-workspace-files: Workspace Files

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Draw a source-backed product-panel schematic of Workspace Files, showing the Gallery panel controls, the WebSocket listing/search path, the HTTP upload/content path, the contained workflow workspace, and the Gallery node’s downstream FileRef output.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Workspace Files header (interface); WorkspaceBrowser (interface); File actions (interface); Node params: path + selection (node params); list_workspace_files (WebSocket); Workspace HTTP routes (HTTP); Workspace listing service (service); Contained workflow workspace (filesystem); Gallery node execution (runtime); Downstream FileRef output (result)
Composition/framing: Use a large operator panel on the left, a two-by-two transport/storage block on the upper right, and a compact workflow execution lane on the lower right. Preserve the visible browser/action split and route listing versus content arrows separately.
Directed relationships:
- workspace-panel-header → workspace-file-actions: "Upload action"
- workspace-browser → workspace-file-actions: "activate, pin, or drag file"
- workspace-browser → list-workspace-files-handler: "browse path or send debounced search" via WebSocket
- list-workspace-files-handler → workspace-listing-service: "directory list or recursive search"
- workspace-listing-service → contained-workspace-root: "contained ls_info / glob_info" via async filesystem
- workspace-file-actions → workspace-http-routes: "upload, preview, or download" via HTTP
- workspace-http-routes → contained-workspace-root: "write upload or stream content"
- workspace-browser → gallery-node-parameters: "navigation writes path"
- workspace-file-actions → gallery-node-parameters: "pinning writes selection"
- gallery-node-parameters → gallery-node-runtime: "run with path / selection"
- gallery-node-runtime → workspace-listing-service: "reuse listing and FileRef conversion"
- gallery-node-runtime → gallery-file-ref-output: "emit selected or listed files"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Workspace Files", "Workspace Files header", "WorkspaceBrowser", "File actions", "Node params: path + selection", "list_workspace_files", "Workspace HTTP routes", "Workspace listing service", "Contained workflow workspace", "Gallery node execution", "Downstream FileRef output", "Upload action", "activate, pin, or drag file", "browse path or send debounced search", "directory list or recursive search", "contained ls_info / glob_info", "upload, preview, or download", "write upload or stream content", "navigation writes path", "pinning writes selection", "run with path / selection", "reuse listing and FileRef conversion", "emit selected or listed files"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Keep WebSocket listing/search separate from HTTP upload/content
- Show path and selection as controlled Gallery node parameters
- Show whole-workspace search, grid/list browsing, polling, preview, pinning, and FileRef drag behavior
- Show the Gallery execution precedence selection > pattern > directory
- Use workspace-relative paths and a single contained workflow workspace
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- generic cloud-drive branding
- implying directories are draggable FileRefs
- showing HTTP as the listing channel
- showing file delete, rename, or move controls that are not implemented
- uncontained host filesystem paths
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Workspace Files"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Workspace Files"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```

## 14-runtime-observability-dock: Runtime Observability Dock

### Dark generation prompt

```text
Use case: infographic-diagram
Asset type: OpenCompany technical documentation diagram; dark-theme structural draft for later SVG redraw
Audience: mixed product and technical readers
Primary request: Draw a source-backed runtime observability schematic of the collapsible Chat / Console dock, showing the three distinct streams—workflow chat, Console-node records, and Terminal server/process logs—from producers and retained state through WebSocketContext into the chat pane and Console/Terminal tabs.
Scene/backdrop: flat near-black technical canvas; no environment or decorative scene
Subject/components: Console node output (workflow); Server / process / CLI logs (runtime); chatTrigger consumers (trigger); StatusBroadcaster (event bus); Dock WebSocket handlers (service); Workflow history DB (database); Terminal ring buffer (memory); WebSocketContext state (client state); Chat pane (interface); Console tab (interface); Terminal tab (interface); Persisted dock preferences (local state)
Composition/framing: Use three vertical zones: runtime producers on the left, transport and retained state in the center, and the actual dock on the right. Inside the dock, show a tall Chat pane beside stacked Console and Terminal tab cards, plus a narrow persisted-preferences bar along the bottom.
Directed relationships:
- console-node-producer → status-broadcaster: "broadcast_console_log"
- terminal-log-producers → status-broadcaster: "broadcast_terminal_log"
- status-broadcaster → workflow-history-db: "persist Console records"
- status-broadcaster → terminal-ring-buffer: "retain latest Terminal records"
- dock-ws-handlers → workflow-history-db: "get / clear scoped history"
- dock-ws-handlers → terminal-ring-buffer: "get / clear terminal history"
- dock-ws-handlers → chat-trigger-consumers: "dispatch chat_message_received" via workflow-scoped CloudEvent
- status-broadcaster → websocket-context-state: "live console_log / terminal_log" via WebSocket broadcast
- dock-ws-handlers → websocket-context-state: "hydrate on connect or workflow switch"
- chat-pane → dock-ws-handlers: "send_chat_message / clear chat" via WebSocket request
- websocket-context-state → chat-pane: "chat messages"
- websocket-context-state → console-tab: "Console records"
- websocket-context-state → terminal-tab: "Terminal records"
- dock-preferences → chat-pane: "height + split width + font"
- dock-preferences → terminal-tab: "selected Console / Terminal tab"
Style/medium: dark technical editorial schematic; flat vector-like infographic; quiet professional workspace wearing restrained neon accents
Text (verbatim): ["Runtime Observability Dock", "Console node output", "Server / process / CLI logs", "chatTrigger consumers", "StatusBroadcaster", "Dock WebSocket handlers", "Workflow history DB", "Terminal ring buffer", "WebSocketContext state", "Chat pane", "Console tab", "Terminal tab", "Persisted dock preferences", "broadcast_console_log", "broadcast_terminal_log", "persist Console records", "retain latest Terminal records", "get / clear scoped history", "get / clear terminal history", "dispatch chat_message_received", "live console_log / terminal_log", "hydrate on connect or workflow switch", "send_chat_message / clear chat", "chat messages", "Console records", "Terminal records", "height + split width + font", "selected Console / Terminal tab"]
Constraints:
- Mixed product and technical audience; understandable without narration
- 16:9 landscape technical schematic with generous gutters and a clear legend
- Every connector is unidirectional, arrow-headed, and explicitly labelled
- Use only the listed components, relationships, and exact text
- Flat vector-like artwork; crisp rounded cards; no raster screenshots
- Geist-style headings with monospace machine labels
- Dark OpenCompany palette: #0d0f13 background, #15171c panels, #1b1e25 cards, #e8eaed text
- Semantic accents only: purple agents, cyan models/interfaces, green tools/results, pink triggers/events, orange workflows/runtime, yellow annotations
- Keep workflow Chat, Console-node output, and Terminal logs visibly separate
- Show chat and Console history as workflow-scoped and database-persisted
- Show Terminal history as a global in-memory latest-200 buffer
- Show live broadcasts and reconnect/workflow-switch hydration as separate labelled flows
- Show the actual chat target, Console-node filter, Terminal-level filter, clear controls, and resizable persisted preferences
Avoid:
- photorealism
- people or stock imagery
- 3D or isometric rendering
- glassmorphism or backdrop blur
- rainbow neon or excessive bloom
- full-strength accent backgrounds
- decorative emoji
- sci-fi HUD chrome
- tiny or illegible text
- crossing arrows
- spaghetti topology
- ambiguous bidirectional arrows
- extra boxes, labels, connectors, or logos
- watermarks
- merging Console and Terminal into one undifferentiated log stream
- implying Terminal history is workflow-scoped or database-persisted
- showing a generic monitoring dashboard with charts
- extra tabs beyond Chat, Console, and Terminal
- unlabelled bidirectional connectors
```

### Light-theme edit prompt

```text
Use case: precise-object-edit
Asset type: light-theme variant of the OpenCompany diagram "Runtime Observability Dock"
Input images: Image 1: edit target, the approved dark-theme structural draft
Primary request: Change only the theme from canonical OpenCompany dark to canonical OpenCompany light.
Color palette: #f5f7fa canvas, #fafbfc panels, #ffffff cards, #1a1d21 primary text, #4b5563 secondary text, #d1d5db neutral borders. Preserve the semantic accent assignments exactly.
Constraints: preserve every component, label, arrow, arrow direction, connector route, legend item, spacing, alignment, card size, and overall 16:9 composition; change only colors and contrast; no new or removed elements; no watermark.
```

### Targeted repair prompt

```text
Use case: precise-object-edit
Asset type: structural repair for the OpenCompany diagram "Runtime Observability Dock"
Input images: Image 1: edit target, the latest generated draft
Primary request: Correct only the specifically identified missing, extra, or misconnected structural element from the review note.
Constraints: preserve all approved geometry, components, labels, colors, typography, spacing, and connector routes; do not redesign the diagram; do not add unrequested text; no watermark.
```
