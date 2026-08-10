# WhatsApp Business Receive (`whatsappBusinessReceive`)

| Field | Value |
|------|-------|
| **Category** | whatsapp_business / trigger |
| **Backend handler** | [`server/nodes/whatsapp_business/whatsapp_business_receive.py`](../../../server/nodes/whatsapp_business/whatsapp_business_receive.py) (`WhatsAppBusinessReceiveNode`, on the shared `_WhatsAppBusinessTrigger` base); webhook plumbing in [`_source.py`](../../../server/nodes/whatsapp_business/_source.py) |
| **Tests** | [`server/tests/nodes/test_whatsapp_business.py`](../../../server/tests/nodes/test_whatsapp_business.py) |
| **Skill (if any)** | none |
| **Dual-purpose tool** | no (trigger) |

## Purpose

Fire when a WhatsApp user messages the business number. Inbound deliveries
arrive on the shared webhook `POST /webhook/whatsapp-business`, are
signature-verified, fanned out per event, and emitted as CloudEvents of type
`com.opencompany.whatsapp_business.message.received`.

## Inputs (handles)

None — this is a trigger; it is the head of a run.

## Parameters

`BaseTriggerParams` only, whose `event_type_filter` is prefixed with
`com.opencompany.whatsapp_business.message.` by `WebhookTriggerNode`.

## Outputs (handles)

| Handle | Shape | Description |
|--------|-------|-------------|
| `output-main` | object | The flat shaped message (`WhatsAppBusinessReceiveOutput`) |

### Output payload (TypeScript shape)

```ts
{
  message_id: string;          // wamid - the dedup key
  from: string;                // sender wa_id (field is `from_` in Python)
  timestamp: string;
  type: string;                // text | image | interactive | ...
  text?: string;
  contact_name?: string;
  phone_number_id?: string;
  media?: { id, mime_type, sha256, filename, caption, voice };
  interactive_reply?: { id, title, type };
  context_message_id?: string; // set when the user replied to a message
}
```

## Logic Flow

```mermaid
flowchart TD
  A[POST /webhook/whatsapp-business] --> B[MetaHubVerifier: X-Hub-Signature-256]
  B -- mismatch --> R400[400]
  B -- secret unavailable --> R503[503 + Retry-After]
  B -- ok --> C[iter_events: entry[] -> changes[] -> value.messages[]]
  C --> D[shape_message] --> E[emit_message_received via dispatch.emit]
  E --> F[Temporal listener signalled by EventType SA]
  F --> G[node filter gates the spawn] --> H[child MachinaWorkflow run]
  C --> I[event_waiter.dispatch] --> J[canvas Run path]
```

## Decision Logic

- **Signature verification fails closed**: no secret configured → `503` with
  `Retry-After`, mismatch → `400`.
- **`_check_precondition`** fails fast on the canvas when the credential is
  missing or `whatsapp_business_app_secret` is unset, instead of waiting out
  the 24-hour trigger timeout.
- **`shape_output` returns `event.data` flat**, matching what the deployed
  path hands downstream. The base default would dump the whole CloudEvents
  envelope, which made `{{trigger.text}}` resolve when deployed and break on
  Run.
- **Account-level `value.errors[]` are logged only**, never emitted.

## Side Effects

- **Broadcasts**: CloudEvents on wire key `whatsapp_business_message_received`.
- **Spawns**: one child `MachinaWorkflow` per admitted event when deployed.

## External Dependencies

- **Credentials**: `WhatsAppBusinessCredential` — `whatsapp_business_app_secret`
  is required for inbound (HMAC key), `whatsapp_business_verify_token` for the
  GET subscription handshake.

## Edge cases & known limits

- **The trigger never downloads media.** It carries a `media.id`; wire
  [`whatsappBusinessMedia`](./whatsappBusinessMedia.md) downstream to fetch.
  Downloading inside the webhook would delay Meta's expected prompt 200 and is
  unreachable on the deployed path anyway.
- **Dedup ids**: messages use the bare `wamid`, so Meta's 7-day retries
  collapse to one run.
- **Two nodes, not one with a mode switch.** A canary trigger registers exactly
  one CloudEvents type (`canary_registry` is a `str -> str` map and the listener
  carries a single `EventType` keyword Search Attribute), and the message and
  status payloads share almost no fields.
- The GET handshake returns a **bare `text/plain` body** — Meta rejects the
  router's JSON envelope.
- Meta's Messenger docs claim the signature is computed over an escaped-unicode
  rendering; this implementation uses raw bytes and logs a diagnostic when a
  failure coincides with non-ASCII input.

## Related

- **Architecture docs**: [WhatsApp Business Service](../../whatsapp_business_service.md), [Event Framework](../../event_framework.md)
- **Sibling**: [`whatsappBusinessStatus`](./whatsappBusinessStatus.md)
