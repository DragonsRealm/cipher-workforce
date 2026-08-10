# WhatsApp Business (Meta Cloud API)

Official Meta WhatsApp Business Platform integration, living entirely in
[`server/nodes/whatsapp_business/`](../server/nodes/whatsapp_business/).

**This is not the same integration as `nodes/whatsapp/`.** That one drives a
*personal* account through an unofficial Go bridge (`edgymeow`) and QR pairing.
Both ship. They share no node type, no credential id, no WebSocket key and no
palette group, because the auth models, capabilities and failure modes have
nothing in common. The Cloud API group is visible in the default palette; the
personal one is `dev`-only.

## Node surface (4 nodes)

| Node | Kind | Operations |
|---|---|---|
| `whatsappBusinessSend` | action, agent tool | `send_text`, `send_media`, `send_template`, `send_buttons`, `send_list`, `send_cta_url`, `send_reaction`, `send_location`, `send_contacts`, `list_templates` |
| `whatsappBusinessMedia` | action, agent tool | `upload`, `get_url`, `download`, `delete` |
| `whatsappBusinessReceive` | trigger | inbound messages |
| `whatsappBusinessStatus` | trigger | delivery receipts |

### Why the node split is where it is

Meta models message type as a **field on one endpoint**
(`POST /{phone-number-id}/messages`; `type` ∈ text, image, video, audio,
document, sticker, location, contacts, reaction, interactive, template), not as
separate endpoints. Splitting by message type would make the canvas imply an API
shape that does not exist, so Send carries them all as operations. Earlier
`whatsappBusinessTemplate` and `whatsappBusinessInteractive` nodes were folded
in for exactly this reason.

Media stays separate because it genuinely is a different endpoint family
(`POST /{phone-number-id}/media`, `GET`/`DELETE /{media-id}`).

The two triggers stay separate for a structural reason: a canary trigger
registers exactly **one** CloudEvents type — `canary_registry` is a `str -> str`
map and the listener carries a single `EventType` keyword Search Attribute — so
one node cannot subscribe to both. Their payloads also share almost no fields,
so merging would mean a union output schema and weaker `{{trigger.field}}`
resolution.

`list_templates` is the one WABA-scoped call kept on Send, because it exists to
answer "what can I send".

## Credential

`WhatsAppBusinessCredential` (`ApiKeyCredential`, `id="whatsapp_business"`,
bearer). One secret plus four operator-metadata fields:

| Field | Required | Purpose |
|---|---|---|
| `apiKey` | yes | System User access token |
| `whatsapp_business_waba_id` | yes | WhatsApp Business Account id — template listing |
| `whatsapp_business_phone_number_id` | yes | The number messages are sent **from** |
| `whatsapp_business_app_secret` | no* | HMAC key for `X-Hub-Signature-256`; *required for inbound* |
| `whatsapp_business_verify_token` | no* | Echoed in the GET subscription handshake |

The probe calls `GET /{waba_id}/phone_numbers` — authenticated, free, and it
validates the WABA id at the same time, returning discovered numbers with their
`quality_rating`.

### The sending number is credential-only, by design

There is deliberately **no `phone_number_id` parameter** on any node. It selects
which business identity a message is sent *from*, which is exactly what a prompt
injection in an inbound message would want to set. On a dual-purpose
`ActionNode` a declared field cannot be protected: the split-schema machinery
(`ToolInput` / `server_controlled_fields`) is a `ToolNode` extension, and
`BaseNode.execute_as_tool` sends `{**node_params, **tool_args}` for everything
else — model arguments win, and `ctx.raw["_raw_parameters"]` is that same merged
dict. Sourcing it from the credential puts it somewhere model arguments cannot
reach. Locked by `TestModelCannotChooseTheSendingNumber`, which drives the real
tool path with a hostile `phone_number_id`.

## Webhook

Claimed via `register_webhook_source(get_webhook_source())` — an *instance*, not
the class. There is no plugin router; the shared catch-all in
[`routers/webhook.py`](../server/routers/webhook.py) dispatches by path, and a
claimed path never reaches the legacy generic handler (which would otherwise
fire every deployed `webhookTrigger`).

| Route | Purpose |
|---|---|
| `GET /webhook/whatsapp-business` | Subscription handshake — compares `hub.verify_token` with `hmac.compare_digest`, echoes `hub.challenge` as **bare `text/plain`** (Meta rejects the router's JSON envelope). 403 on mismatch or unconfigured token. |
| `POST /webhook/whatsapp-business` | Deliveries — `MetaHubVerifier` (SHA-256 HMAC over the **raw body**, `X-Hub-Signature-256`, `sha256=` prefix). Fails **closed**: 503 + `Retry-After` when the secret is unavailable, 400 on mismatch. |

`iter_events` walks all three nesting levels
(`entry[] -> changes[] -> value.messages[] / statuses[] / errors[]`) and emits
one CloudEvent per item. Account-level `errors[]` are logged only.

Both delivery paths are driven: `dispatch.emit` for deployed Temporal listeners,
and `event_waiter.dispatch` for the canvas-Run path.

**Replay dedup ids.** Meta retries for 7 days. Messages use the bare `wamid`;
statuses use a composite `f"{wamid}:{status}"`, because `sent -> delivered ->
read` all carry the same message id and a bare wamid would collapse them into
one event.

**Known risk, documented in code**: Meta's *Messenger* docs claim the signature
is computed over an escaped-unicode rendering rather than raw bytes. This
implementation uses raw bytes and logs a diagnostic when a verification failure
coincides with non-ASCII bytes in the body.

## Error classification

`classify_error` splits Meta's numeric codes by retryability, which matters
because `NodeUserError` is on the framework's non-retryable list:

| Codes | Mapped to | Effect |
|---|---|---|
| `0`, `190` | `PermissionError` with `.provider` / `.reason` / `.auth` | Reconnect chip in the UI + `credential.*.runtime_failed` event |
| throttle / transient | `RuntimeError` | Retryable — preserves backoff |
| permission / policy / template / account | `NodeUserError` | One WARN line, no traceback |
| `131047` | `NodeUserError` | Bespoke message pointing at `send_template` — the 24-hour window has closed |

## Icons

All four node icons are **library references declared in the plugin's own
`meta.json`**, not vendored SVGs:

```json
{"color": "#128C7E",
 "icons": {"whatsappBusinessSend": "lucide:Send",
           "whatsappBusinessMedia": "lucide:Paperclip",
           "whatsappBusinessReceive": "lucide:Inbox",
           "whatsappBusinessStatus": "lucide:CheckCheck"}}
```

`CheckCheck` is WhatsApp's own delivery tick. `whatsapp_business.svg` remains as
the *credential* brand mark — it resolves through `Credential.get_icon_path`,
a different chain, and no library has an equivalent. See
[plugin_system.md](./plugin_system.md) for the resolution order.

## Gotchas worth knowing before editing

- **`operation` is load-bearing on Send.** `_pick_operation` reads it off the
  raw parameter dict *before* Pydantic validation, so the model default never
  applies at dispatch; raw-dict callers must pass it explicitly.
- **The kind selector is `media_type`, not `message_type`.**
  `ParameterRenderer.getFileAcceptType` hardcodes a switch on a sibling named
  `message_type` and would override the backend's declared `accept`.
- **Uploads post bytes, not a file handle** — `Connection.request` replays the
  same kwargs on its one auth retry, and a consumed handle replays empty.
- **Media travels as a reference, never bytes** — see
  [media_transport.md](./media_transport.md).
- **`shape_output` is overridden on both triggers** to return `event.data` flat,
  matching the deployed path. The base default dumps the whole CloudEvents
  envelope, which made `{{trigger.text}}` resolve when deployed and break on
  Run.

## Unverified

`cta_url` matches Meta's documented CTA payload shape, but the messages
reference schema does not enumerate it among `interactive.type` values. Not yet
confirmed against a live send.

## Related

- Per-node contracts: [`node-logic-flows/whatsapp_business/`](./node-logic-flows/whatsapp_business/)
- [Event Framework](./event_framework.md) · [Media Transport](./media_transport.md) · [Plugin System](./plugin_system.md)
- Meta docs: <https://developers.facebook.com/docs/whatsapp/cloud-api>
