"""whatsappBusinessTemplate — send an approved template, or list templates.

Templates are the only way to message a user outside the 24-hour customer
service window, so this is the node error 131047 points at.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import graph_get, graph_post, normalize_recipient, resolve_phone_number_id
from ._credentials import WhatsAppBusinessCredential


class WhatsAppBusinessTemplateParams(BaseModel):
    operation: Literal["send_template", "list_templates"] = Field(
        default="send_template",
        description="Send an approved template, or list what is available.",
    )
    to: str = Field(
        default="",
        description="Recipient phone number in international format.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )
    template_name: str = Field(
        default="",
        description="Exact name of the approved template.",
        json_schema_extra={
            "displayOptions": {"show": {"operation": ["send_template"]}},
            "loadOptionsMethod": "whatsappBusinessTemplates",
        },
    )
    language_code: str = Field(
        default="en_US",
        description="Template language, e.g. en_US, es_MX, hi. Meta does not translate.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )
    body_parameters: List[str] = Field(
        default_factory=list,
        description=(
            "Values for the body placeholders, in order. For a named template "
            "use the Named Parameters field instead."
        ),
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )
    named_parameters: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Values keyed by placeholder name, for templates created with "
            "parameter_format=named."
        ),
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )
    header_media_id: str = Field(
        default="",
        description="Media ID for a template whose header is an image, video or document.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )
    header_media_type: Literal["image", "video", "document"] = Field(
        default="image",
        description="Which media kind the template header expects.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_template"]}}},
    )

    model_config = ConfigDict(extra="ignore")

    @field_validator("body_parameters", mode="before")
    @classmethod
    def _coerce_body_parameters(cls, value: Any) -> Any:
        """Accept a JSON string as well as a list.

        An LLM supplying tool arguments frequently stringifies arrays, and a
        ValidationError there reads as a node bug rather than a retryable
        argument mistake. Malformed input still falls through to Pydantic.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            return parsed if isinstance(parsed, list) else [parsed]
        return value

    @field_validator("named_parameters", mode="before")
    @classmethod
    def _coerce_named_parameters(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return value


class WhatsAppBusinessTemplateOutput(BaseModel):
    message_id: Optional[str] = None
    to: Optional[str] = None
    wa_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    message_status: Optional[str] = None
    templates: List[dict] = Field(default_factory=list)
    count: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessTemplateNode(ActionNode):
    type = "whatsappBusinessTemplate"
    display_name = "WhatsApp Business Template"
    subtitle = "Approved template"
    group = ("whatsapp_business", "tool")
    description = "Send an approved WhatsApp template message, or list available templates"
    component_kind = "square"
    tool_name = "whatsapp_business_template"
    tool_description = (
        "Send a pre-approved WhatsApp template message. This is the ONLY way to "
        "message someone who has not written to the business in the last 24 hours. "
        "Use list_templates first to see what is approved and how many "
        "placeholders each one takes."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (WhatsAppBusinessCredential,)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.MESSAGING
    usable_as_tool = True

    Params = WhatsAppBusinessTemplateParams
    Output = WhatsAppBusinessTemplateOutput

    @Operation("send_template", cost={"service": "whatsapp_business", "action": "send_template", "count": 1})
    async def send_template(
        self, ctx: NodeContext, params: WhatsAppBusinessTemplateParams
    ) -> WhatsAppBusinessTemplateOutput:
        if not params.template_name.strip():
            raise NodeUserError("Choose a template name. Use list_templates to see approved ones.")

        recipient = normalize_recipient(params.to)
        phone_number_id = await resolve_phone_number_id(ctx)

        components: List[Dict[str, Any]] = []

        if params.header_media_id.strip():
            components.append(
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": params.header_media_type,
                            params.header_media_type: {"id": params.header_media_id.strip()},
                        }
                    ],
                }
            )

        body_params = _build_body_parameters(params)
        if body_params:
            components.append({"type": "body", "parameters": body_params})

        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": params.template_name.strip(),
                # An object at send time, a bare string at creation time. The
                # asymmetry is Meta's, not ours.
                "language": {"code": params.language_code.strip() or "en_US"},
            },
        }
        if components:
            payload["template"]["components"] = components

        result = await graph_post(ctx, f"{phone_number_id}/messages", payload)
        messages = result.get("messages") or []
        contacts = result.get("contacts") or []
        first = messages[0] if messages else {}
        return WhatsAppBusinessTemplateOutput(
            message_id=first.get("id"),
            message_status=first.get("message_status"),
            to=recipient,
            wa_id=(contacts[0].get("wa_id") if contacts else None),
            phone_number_id=phone_number_id,
        )

    @Operation("list_templates")
    async def list_templates(
        self, ctx: NodeContext, params: WhatsAppBusinessTemplateParams
    ) -> WhatsAppBusinessTemplateOutput:
        from services.plugin.deps import get_auth_service

        waba_id = await get_auth_service().get_api_key("whatsapp_business_waba_id")
        if not waba_id:
            raise NodeUserError(
                "Add your WhatsApp Business Account ID to the credential to list templates."
            )

        result = await graph_get(
            ctx,
            f"{waba_id}/message_templates",
            {"fields": "name,status,category,language,components", "limit": 100},
        )
        rows = [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "category": item.get("category"),
                "language": item.get("language"),
                "placeholders": _count_placeholders(item),
            }
            for item in (result.get("data") or [])
        ]
        return WhatsAppBusinessTemplateOutput(templates=rows, count=len(rows))


def _build_body_parameters(params: WhatsAppBusinessTemplateParams) -> List[Dict[str, Any]]:
    """Named and positional are mutually exclusive, fixed at template creation.

    Named wins when both are supplied, because a named template rejects
    positional values outright with error 132000 rather than ignoring them.
    """
    if params.named_parameters:
        return [
            {"type": "text", "parameter_name": key, "text": str(value)}
            for key, value in params.named_parameters.items()
        ]
    return [{"type": "text", "text": str(value)} for value in params.body_parameters]


def _count_placeholders(template: Dict[str, Any]) -> Optional[int]:
    """How many body values the template expects.

    Surfaced because a count mismatch is error 132000, the single most common
    template failure, and the number is otherwise invisible until a send fails.
    """
    for component in template.get("components") or []:
        if str(component.get("type", "")).upper() != "BODY":
            continue
        text = component.get("text") or ""
        example = component.get("example") or {}
        named = example.get("body_text_named_params")
        if isinstance(named, list):
            return len(named)
        import re

        return len(set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", text)))
    return None


__all__ = [
    "WhatsAppBusinessTemplateNode",
    "WhatsAppBusinessTemplateOutput",
    "WhatsAppBusinessTemplateParams",
]
