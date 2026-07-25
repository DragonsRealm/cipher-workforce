from typing import Literal, Optional

from pydantic import Field

from .._base import ChatModelBase, ChatModelParams

from .._credentials import SarvamCredential


class SarvamChatModelParams(ChatModelParams):
    frequency_penalty: Optional[float] = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        json_schema_extra={"numberStepSize": 0.1},
    )
    presence_penalty: Optional[float] = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        json_schema_extra={"numberStepSize": 0.1},
    )
    response_format: Optional[Literal["text", "json_object"]] = Field(default="text")
    # Sarvam runs reasoning ON by default at effort "medium"; this toggle only
    # decides whether we override that effort. There is no documented way to
    # turn reasoning off, which is why the provider block omits
    # ``thinking_default_on`` — that key emits Moonshot's proprietary
    # ``extra_body.thinking = {"type": "disabled"}``.
    thinking_enabled: bool = Field(default=False)
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = Field(
        default="medium",
        json_schema_extra={"displayOptions": {"show": {"thinking_enabled": [True]}}},
    )


class SarvamChatModelNode(ChatModelBase):
    type = "sarvamChatModel"
    display_name = "Sarvam AI"
    subtitle = "Chat Model"
    group = ("model",)
    description = (
        "Sarvam AI Indic-first models (sarvam-105b 128K, sarvam-30b 64K) with "
        "always-on reasoning across 10 Indian languages plus English"
    )

    credentials = (SarvamCredential,)
    Params = SarvamChatModelParams
