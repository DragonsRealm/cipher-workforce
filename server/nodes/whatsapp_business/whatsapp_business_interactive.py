"""whatsappBusinessInteractive — reply buttons, list menus and CTA-URL.

Replies arrive back on ``whatsappBusinessReceive`` as ``interactive_reply``,
with the ``id`` you set here, so the id is the join between what was offered
and what was chosen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import graph_post, normalize_recipient, resolve_phone_number_id
from ._credentials import WhatsAppBusinessCredential

# Meta's documented ceilings. Enforced locally because the API rejects the
# whole message rather than trimming, and a 400 mid-conversation is worse
# than a clear error before sending.
_MAX_BUTTONS = 3
_MAX_BUTTON_TITLE = 20
_MAX_ROWS_TOTAL = 10
_MAX_ROW_TITLE = 24
_MAX_ROW_DESCRIPTION = 72
_MAX_LIST_BUTTON = 20
# The OpenAPI spec says 1024 and the interactive-list docs page says 4096.
# Using the smaller of Meta's two numbers: too-short fails loudly at authoring
# time, too-long fails at send time in front of a customer.
_MAX_BODY = 1024
_MAX_FOOTER = 60
_MAX_HEADER = 60


class WhatsAppBusinessInteractiveParams(BaseModel):
    operation: Literal["send_buttons", "send_list", "send_cta_url"] = Field(
        default="send_buttons",
        description="Which interactive message to send.",
    )
    to: str = Field(default="", description="Recipient phone number in international format.")
    body: str = Field(
        default="",
        description="Main message text.",
        json_schema_extra={"rows": 3},
    )
    header: str = Field(default="", description="Optional bold header text.")
    footer: str = Field(default="", description="Optional small footer text.")

    buttons: List[Dict[str, str]] = Field(
        default_factory=list,
        description='Up to 3 reply buttons: [{"id": "yes", "title": "Yes"}]',
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_buttons"]}}},
    )

    list_button_text: str = Field(
        default="Choose",
        description="Label on the button that opens the list.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_list"]}}},
    )
    sections: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            'Sections of rows: [{"title": "Plans", "rows": '
            '[{"id": "p1", "title": "Basic", "description": "..."}]}]. '
            "10 rows maximum across all sections."
        ),
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_list"]}}},
    )

    cta_display_text: str = Field(
        default="",
        description="Button label for the CTA URL.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_cta_url"]}}},
    )
    cta_url: str = Field(
        default="",
        description="URL the button opens.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_cta_url"]}}},
    )

    model_config = ConfigDict(extra="ignore")

    @field_validator("buttons", "sections", mode="before")
    @classmethod
    def _coerce_json(cls, value: Any) -> Any:
        """LLM tool arguments routinely arrive as stringified JSON."""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                return []
            return parsed if isinstance(parsed, list) else [parsed]
        return value


class WhatsAppBusinessInteractiveOutput(BaseModel):
    message_id: Optional[str] = None
    to: Optional[str] = None
    wa_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    message_status: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessInteractiveNode(ActionNode):
    type = "whatsappBusinessInteractive"
    display_name = "WhatsApp Business Interactive"
    subtitle = "Buttons / list / CTA"
    group = ("whatsapp_business", "tool")
    description = "Send reply buttons, a list menu, or a call-to-action URL button"
    component_kind = "square"
    tool_name = "whatsapp_business_interactive"
    tool_description = (
        "Send an interactive WhatsApp message: up to 3 reply buttons, a list menu "
        "of up to 10 rows, or a single call-to-action URL button. The user's "
        "choice comes back on the WhatsApp Business Receive trigger carrying the "
        "id you set here."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (WhatsAppBusinessCredential,)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.MESSAGING
    usable_as_tool = True

    Params = WhatsAppBusinessInteractiveParams
    Output = WhatsAppBusinessInteractiveOutput

    @Operation("send_buttons", cost={"service": "whatsapp_business", "action": "send_interactive", "count": 1})
    async def send_buttons(
        self, ctx: NodeContext, params: WhatsAppBusinessInteractiveParams
    ) -> WhatsAppBusinessInteractiveOutput:
        if not params.buttons:
            raise NodeUserError('Add at least one button, e.g. [{"id": "yes", "title": "Yes"}].')
        if len(params.buttons) > _MAX_BUTTONS:
            raise NodeUserError(
                f"WhatsApp allows at most {_MAX_BUTTONS} reply buttons; got {len(params.buttons)}. "
                "Use a list message for more options."
            )

        buttons = []
        for index, button in enumerate(params.buttons):
            title = str(button.get("title") or "").strip()
            if not title:
                raise NodeUserError(f"Button {index + 1} has no title.")
            if len(title) > _MAX_BUTTON_TITLE:
                raise NodeUserError(
                    f"Button title {title!r} is {len(title)} characters; the limit is {_MAX_BUTTON_TITLE}."
                )
            buttons.append(
                {
                    "type": "reply",
                    "reply": {"id": str(button.get("id") or f"btn_{index + 1}"), "title": title},
                }
            )

        return await self._send(ctx, params, {"type": "button", "action": {"buttons": buttons}})

    @Operation("send_list", cost={"service": "whatsapp_business", "action": "send_interactive", "count": 1})
    async def send_list(
        self, ctx: NodeContext, params: WhatsAppBusinessInteractiveParams
    ) -> WhatsAppBusinessInteractiveOutput:
        if not params.sections:
            raise NodeUserError("Add at least one section with rows.")

        total_rows = sum(len(section.get("rows") or []) for section in params.sections)
        if total_rows == 0:
            raise NodeUserError("The list has no rows.")
        if total_rows > _MAX_ROWS_TOTAL:
            # The cap is across ALL sections, not per section -- a common
            # misreading, so the message says so.
            raise NodeUserError(
                f"A list allows {_MAX_ROWS_TOTAL} rows in total across every section; got {total_rows}."
            )

        button_text = params.list_button_text.strip() or "Choose"
        if len(button_text) > _MAX_LIST_BUTTON:
            raise NodeUserError(
                f"List button text is {len(button_text)} characters; the limit is {_MAX_LIST_BUTTON}."
            )

        sections = []
        for section in params.sections:
            rows = []
            for index, row in enumerate(section.get("rows") or []):
                title = str(row.get("title") or "").strip()
                if not title:
                    raise NodeUserError(f"Row {index + 1} has no title.")
                if len(title) > _MAX_ROW_TITLE:
                    raise NodeUserError(
                        f"Row title {title!r} is {len(title)} characters; the limit is {_MAX_ROW_TITLE}."
                    )
                entry = {"id": str(row.get("id") or f"row_{index + 1}"), "title": title}
                description = str(row.get("description") or "").strip()
                if description:
                    if len(description) > _MAX_ROW_DESCRIPTION:
                        raise NodeUserError(
                            f"Row description for {title!r} is {len(description)} characters; "
                            f"the limit is {_MAX_ROW_DESCRIPTION}."
                        )
                    entry["description"] = description
                rows.append(entry)
            sections.append({"title": str(section.get("title") or "")[:24], "rows": rows})

        return await self._send(
            ctx,
            params,
            {"type": "list", "action": {"button": button_text, "sections": sections}},
            # A list header is text-only; media there is rejected.
            header_text_only=True,
        )

    @Operation("send_cta_url", cost={"service": "whatsapp_business", "action": "send_interactive", "count": 1})
    async def send_cta_url(
        self, ctx: NodeContext, params: WhatsAppBusinessInteractiveParams
    ) -> WhatsAppBusinessInteractiveOutput:
        display_text = params.cta_display_text.strip()
        url = params.cta_url.strip()
        if not display_text or not url:
            raise NodeUserError("A CTA button needs both display text and a URL.")

        return await self._send(
            ctx,
            params,
            {
                "type": "cta_url",
                "action": {
                    "name": "cta_url",
                    "parameters": {"display_text": display_text, "url": url},
                },
            },
        )

    async def _send(
        self,
        ctx: NodeContext,
        params: WhatsAppBusinessInteractiveParams,
        interactive: Dict[str, Any],
        *,
        header_text_only: bool = False,
    ) -> WhatsAppBusinessInteractiveOutput:
        body = params.body.strip()
        if not body:
            raise NodeUserError("An interactive message needs body text.")
        if len(body) > _MAX_BODY:
            raise NodeUserError(
                f"Body is {len(body)} characters; the limit is {_MAX_BODY}."
            )

        interactive = dict(interactive)
        interactive["body"] = {"text": body}

        header = params.header.strip()
        if header:
            if len(header) > _MAX_HEADER:
                raise NodeUserError(f"Header is {len(header)} characters; the limit is {_MAX_HEADER}.")
            interactive["header"] = {"type": "text", "text": header}
        elif header_text_only:
            pass

        footer = params.footer.strip()
        if footer:
            if len(footer) > _MAX_FOOTER:
                raise NodeUserError(f"Footer is {len(footer)} characters; the limit is {_MAX_FOOTER}.")
            interactive["footer"] = {"text": footer}

        recipient = normalize_recipient(params.to)
        phone_number_id = await resolve_phone_number_id(ctx)
        result = await graph_post(
            ctx,
            f"{phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "interactive",
                "interactive": interactive,
            },
        )

        messages = result.get("messages") or []
        contacts = result.get("contacts") or []
        first = messages[0] if messages else {}
        return WhatsAppBusinessInteractiveOutput(
            message_id=first.get("id"),
            message_status=first.get("message_status"),
            to=recipient,
            wa_id=(contacts[0].get("wa_id") if contacts else None),
            phone_number_id=phone_number_id,
        )


__all__ = [
    "WhatsAppBusinessInteractiveNode",
    "WhatsAppBusinessInteractiveOutput",
    "WhatsAppBusinessInteractiveParams",
]
