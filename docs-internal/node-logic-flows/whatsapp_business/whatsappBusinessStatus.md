# WhatsApp Business Status (`whatsappBusinessStatus`)

| Field | Value |
|------|-------|
| **Category** | whatsapp_business / trigger |
| **Backend handler** | [`server/nodes/whatsapp_business/whatsapp_business_receive.py`](../../../server/nodes/whatsapp_business/whatsapp_business_receive.py) (`WhatsAppBusinessStatusNode`, on the shared `_WhatsAppBusinessTrigger` base); webhook plumbing in [`_source.py`](../../../server/nodes/whatsapp_business/_source.py) |
| **Tests** | [`server/tests/nodes/test_whatsapp_business.py`](../../../server/tests/nodes/test_whatsapp_business.py) |
| **Skill (if any)** | none |
| **Dual-purpose tool** | no (trigger) |

## Purpose

Fire on delivery receipts for messages the business **sent** — `sent`,
`delivered`, `read`, `failed`. Shares the webhook, credential and verification
path with [`whatsappBusinessReceive`](./whatsappBusinessReceive.md) but carries a
different CloudEvents type (`...whatsapp_business.status.updated`) and a
different payload.

> **Note on volume.** WhatsApp emits a status callback per outbound message for
> each state transition, so this trigger typically fires 2-4x per message you
> send. Deploy it only when you actually consume receipts.

## Inputs (handles)

None — this is a trigger.

## Parameters

`BaseTriggerParams` only, whose `event_type_filter` is prefixed with
`com.opencompany.whatsapp_business.status.` by `WebhookTriggerNode`.

## Outputs (handles)

| Handle | Shape | Description |
|--------|-------|-------------|
| `output-main` | object | The flat shaped status (`WhatsAppBusinessStatusOutput`) |

### Output payload (TypeScript shape)

```ts
{
  message_id: string;               // wamid of the message you sent
  status: string;                   // sent | delivered | read | failed
  recipient_id?: string;
  timestamp?: string;
  conversation_id?: string;
  conversation_origin?: string;
  conversation_expires_at?: string; // when the 24-hour window closes
  billable?: boolean;
  pricing_model?: string;
  pricing_category?: string;
  error_code?: number;              // failed only
  error_title?: string;
  error_detail?: string;
  biz_opaque_callback_data?: string;
}
```

## Logic Flow

Identical intake to `whatsappBusinessReceive` — same route, same
`MetaHubVerifier`, same three-level fan-out — diverging at `iter_events`, which
walks `value.statuses[]` instead of `value.messages[]` and calls
`shape_status` / `emit_status_updated`.

## Decision Logic

- **Dedup id is composite**: `f"{wamid}:{status}"`. A bare wamid would make
  `sent -> delivered -> read` collapse into a single event, since all three
  carry the same message id.
- **`shape_output` returns `event.data` flat**, inherited from the shared base
  so Run and deploy cannot disagree about the output shape.
- **`_check_precondition`** is the shared one: fails fast when the credential
  or `whatsapp_business_app_secret` is missing.

## Side Effects

- **Broadcasts**: CloudEvents on wire key `whatsapp_business_status_updated`.
- **Spawns**: one child `MachinaWorkflow` per admitted event when deployed.

## External Dependencies

Same as `whatsappBusinessReceive`.

## Edge cases & known limits

- `conversation_expires_at` is the practical signal for whether a free-form
  reply is still allowed; past it, only an approved template gets through
  (error `131047`).
- `error_code` is populated only on `failed`.
- Meta's own subscription settings control whether status callbacks are
  delivered at all. If the WABA is not subscribed to the `message_status`
  field, this trigger never fires and nothing in OpenCompany reports that.

## Related

- **Architecture docs**: [WhatsApp Business Service](../../whatsapp_business_service.md), [Event Framework](../../event_framework.md)
- **Sibling**: [`whatsappBusinessReceive`](./whatsappBusinessReceive.md)
- **Template sending** (for a closed window): [`whatsappBusinessSend`](./whatsappBusinessSend.md) `send_template`
