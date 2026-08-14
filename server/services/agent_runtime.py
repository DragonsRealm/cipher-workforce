"""Provider-neutral agent execution primitives.

This module is the shared boundary between orchestration (the in-process
``AIService`` loop and Temporal activities) and the native provider SDK layer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Type,
)

from pydantic import BaseModel, ValidationError

from core.logging import get_logger
from services.llm.media import (
    hydrate_image_blocks,
    image_blocks_from_tool_result,
)
from services.llm.messages import filter_empty_messages
from services.llm.protocol import (
    LLMError,
    LLMResponse,
    Message,
    ThinkingConfig,
    ToolCall,
    ToolDef,
    Usage,
    message_to_wire,
    messages_to_wire,
)
from services.tool_identity import DuplicateToolNameError

logger = get_logger(__name__)


class AgentContextTransitionSink(Protocol):
    """Bound, epoch-fenced sink for one Context thread.

    Store implementations decide whether payloads are inlined or replaced by
    hash-addressed blob references.  The runtime only emits committed
    transitions in provider-observable order.
    """

    async def append_transition(
        self,
        *,
        event_type: str,
        operation_id: str,
        provider: str,
        message_wire: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any: ...


@dataclass
class AgentToolSpec:
    """LLM-visible definition plus local execution/validation metadata."""

    definition: ToolDef
    args_schema: Optional[Type[BaseModel]] = None
    execution: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def parameters(self) -> Dict[str, Any]:
        return self.definition.parameters


def _tool_definition(tool: ToolDef | AgentToolSpec) -> ToolDef:
    return tool.definition if isinstance(tool, AgentToolSpec) else tool


def _tool_specs_by_name(
    tools: Sequence[ToolDef | AgentToolSpec],
) -> Dict[str, AgentToolSpec]:
    return {
        tool.name: tool
        for tool in tools
        if isinstance(tool, AgentToolSpec)
    }


def add_usage(total: Usage, current: Optional[Usage]) -> Usage:
    """Accumulate normalized provider usage without mutating either input."""

    if current is None:
        return total
    return Usage(
        input_tokens=total.input_tokens + current.input_tokens,
        output_tokens=total.output_tokens + current.output_tokens,
        total_tokens=total.total_tokens + current.total_tokens,
        cache_creation_tokens=(
            total.cache_creation_tokens + current.cache_creation_tokens
        ),
        cache_read_tokens=total.cache_read_tokens + current.cache_read_tokens,
        reasoning_tokens=total.reasoning_tokens + current.reasoning_tokens,
    )


async def run_native_llm_step(
    chat_unifier,
    *,
    provider: str,
    api_key: str,
    messages: Sequence[Message],
    model: str,
    temperature: float,
    max_tokens: int,
    thinking: Optional[ThinkingConfig] = None,
    tools: Optional[Sequence[ToolDef | AgentToolSpec]] = None,
    context_management: Optional[Dict[str, Any]] = None,
    sdk_max_retries: int = 0,
    explicit_max_retries: int = 2,
    translate_errors: bool = True,
) -> LLMResponse:
    """Execute one native SDK turn and return its lossless response envelope.

    Agent retries are deliberately outside the provider SDK so only failures
    normalized as retryable :class:`LLMError` values are repeated. Temporal
    passes ``explicit_max_retries=0`` and owns activity-level retry itself.
    """

    if chat_unifier is None:
        raise RuntimeError("ChatUnifier is required for native agent execution")

    definitions = [_tool_definition(tool) for tool in (tools or ())]
    # Hydrate once, outside the retry loop, on throwaway copies — refs stay
    # the durable form; bytes exist only for this provider call.
    prepared = await hydrate_image_blocks(
        filter_empty_messages(messages), provider=provider, model=model
    )
    attempts = max(0, int(explicit_max_retries)) + 1
    for attempt in range(attempts):
        try:
            return await chat_unifier.chat(
                provider=provider,
                api_key=api_key,
                messages=prepared,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                tools=definitions or None,
                context_management=context_management,
                sdk_max_retries=max(0, int(sdk_max_retries)),
                # Preserve structured metadata until this agent-step boundary.
                translate_errors=False,
            )
        except LLMError as error:
            if not error.retryable or attempt + 1 >= attempts:
                if not translate_errors:
                    raise
                from services.plugin import NodeUserError

                raise NodeUserError(error.user_message) from error
            delay = (
                error.retry_after
                if error.retry_after is not None
                else 0.25 * (2**attempt)
            )
            await asyncio.sleep(max(0.0, min(float(delay), 5.0)))

    raise AssertionError("native LLM retry loop exhausted unexpectedly")


def _validated_tool_args(
    call: ToolCall,
    specs: Dict[str, AgentToolSpec],
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if call.parse_error:
        return None, call.parse_error

    spec = specs.get(call.name)
    if spec is None or spec.args_schema is None:
        return dict(call.args or {}), None

    try:
        value = spec.args_schema.model_validate(call.args or {})
        # Only fields the model actually supplied participate in
        # ToolNode.execute_as_tool's ``{**node_params, **tool_args}`` merge.
        # Materializing Pydantic defaults would let schema metadata
        # silently override operator node configuration: a node the user
        # set to method=POST would be reset to the schema's GET default
        # merely because the model omitted the field. Defaults are schema
        # metadata, not model-authored arguments.
        return value.model_dump(mode="json", exclude_unset=True), None
    except ValidationError as exc:
        return None, str(exc)


async def run_native_agent_loop(
    chat_unifier,
    *,
    provider: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    initial_messages: Sequence[Message],
    thinking: Optional[ThinkingConfig] = None,
    tools: Optional[Sequence[AgentToolSpec]] = None,
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]] = None,
    max_iterations: int = 500,
    progress_callback: Optional[Callable[[int], Awaitable[Any]]] = None,
    rebind_from_operations: Optional[
        Callable[[List[Dict[str, Any]]], Awaitable[List[AgentToolSpec]]]
    ] = None,
    context_management: Optional[Dict[str, Any]] = None,
    compaction_pause_callback: Optional[
        Callable[
            [int, LLMResponse, List[Message]],
            Awaitable[Optional[List[Message]]],
        ]
    ] = None,
    context_transition_sink: Optional[AgentContextTransitionSink] = None,
    context_operation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the shared buffered native tool-agent loop.

    The complete assistant message returned by a provider is appended verbatim
    before tool execution.  This is essential for Gemini thought signatures,
    Anthropic signed thinking blocks, and OpenAI reasoning continuation state.
    """

    current_tools: List[AgentToolSpec] = list(tools or ())
    messages: List[Message] = list(initial_messages)
    usage = Usage()
    thinking_parts: List[str] = []
    last_response: Optional[LLMResponse] = None
    iteration = 0
    if context_transition_sink is not None and not context_operation_id:
        raise ValueError(
            "context_operation_id is required when a Context transition sink is used"
        )

    async def persist_transition(
        event_type: str,
        *,
        operation_id: str,
        message: Optional[Message] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if context_transition_sink is None:
            return
        await context_transition_sink.append_transition(
            event_type=event_type,
            operation_id=f"{context_operation_id}:{operation_id}",
            provider=provider,
            message_wire=message_to_wire(message) if message else None,
            payload=payload,
        )

    for iteration in range(1, max_iterations + 1):
        if progress_callback is not None:
            try:
                await progress_callback(iteration)
            except Exception as exc:  # progress is observational
                logger.debug("[Agent loop] progress callback failed: %s", exc)

        definitions = [_tool_definition(tool) for tool in current_tools]
        await persist_transition(
            "request.snapshot",
            operation_id=f"iteration:{iteration}:request",
            payload={
                "iteration": iteration,
                "provider": provider,
                "model": model,
                "settings": {
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "thinking": asdict(thinking) if thinking is not None else None,
                },
                "messages": messages_to_wire(messages),
                "tools": [
                    {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": definition.parameters,
                    }
                    for definition in definitions
                ],
                "dynamic_tool_count": len(current_tools),
            },
        )
        try:
            response = await run_native_llm_step(
                chat_unifier,
                provider=provider,
                api_key=api_key,
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
                tools=current_tools,
                context_management=context_management,
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await persist_transition(
                "provider.outcome_ambiguous",
                operation_id=f"iteration:{iteration}:provider-error",
                payload={
                    "iteration": iteration,
                    "error_type": type(exc).__name__,
                    "ambiguous": True,
                },
            )
            raise
        last_response = response
        usage = add_usage(usage, response.billing_usage or response.usage)

        assistant = response.assistant_message
        if assistant is None:
            assistant = Message(
                role="assistant",
                content=response.content,
                tool_calls=list(response.tool_calls),
            )
        messages.append(assistant)
        # This commit is intentionally before any requested tool executes.
        await persist_transition(
            "message.assistant",
            operation_id=f"iteration:{iteration}:assistant",
            message=assistant,
            payload={
                "iteration": iteration,
                "finish_reason": response.finish_reason,
                "model": response.model or model,
                "usage": asdict(response.usage),
                "billing_usage": asdict(
                    response.billing_usage or response.usage
                ),
            },
        )

        if response.thinking:
            thinking_parts.append(
                (
                    response.thinking
                    if not thinking_parts
                    else f"--- Iteration {iteration} ---\n{response.thinking}"
                )
            )

        if str(response.finish_reason).upper() == "SAFETY":
            logger.warning("[Agent loop] provider response blocked by safety filters")

        calls = list(response.tool_calls or assistant.tool_calls or ())
        if (
            str(response.finish_reason).strip().lower() == "compaction"
            and not calls
        ):
            # Anthropic's durability pause returns only a compaction block.
            # It has already been journaled above; continue from that exact
            # block instead of exposing it as the user's final response.
            await persist_transition(
                "provider.compaction_paused",
                operation_id=f"iteration:{iteration}:compaction-pause",
                payload={
                    "iteration": iteration,
                    "finish_reason": response.finish_reason,
                },
            )
            if compaction_pause_callback is not None:
                replacement = await compaction_pause_callback(
                    iteration,
                    response,
                    list(messages),
                )
                if replacement is not None:
                    messages = list(replacement)
            continue
        if not calls:
            await persist_transition(
                "response.final",
                operation_id=f"iteration:{iteration}:final",
                payload={
                    "iteration": iteration,
                    "usage": asdict(response.usage),
                    "lifetime_usage": asdict(usage),
                    "active_input_tokens": response.usage.input_tokens,
                    "finish_reason": response.finish_reason,
                },
            )
            return {
                "messages": messages,
                "iteration": iteration,
                "thinking_content": "\n\n".join(thinking_parts) or None,
                "truncated": False,
                "usage": usage,
                "response": response,
            }

        if tool_executor is None:
            logger.warning(
                "[Agent loop] model emitted %d tool call(s) without an executor",
                len(calls),
            )
            return {
                "messages": messages,
                "iteration": iteration,
                "thinking_content": "\n\n".join(thinking_parts) or None,
                "truncated": False,
                "usage": usage,
                "response": response,
            }

        specs = _tool_specs_by_name(current_tools)
        iteration_new_tools: List[AgentToolSpec] = []
        for call_index, call in enumerate(calls, start=1):
            if call.name not in specs:
                result: Any = {
                    "error": "Unknown tool",
                    "details": (
                        f"Tool {call.name!r} is not connected to this agent."
                    ),
                }
            else:
                args, validation_error = _validated_tool_args(call, specs)
                if validation_error:
                    result = {
                        "error": "Invalid tool arguments",
                        "details": validation_error,
                    }
                    if call.raw_arguments is not None:
                        result["raw_arguments"] = call.raw_arguments
                else:
                    try:
                        # AgentToolSpec.execution is the trusted server-side
                        # config consumed by execute_tool. Attach the provider
                        # call identity there, never to model-controlled args,
                        # so stateful tools can make mutations idempotent.
                        spec = specs[call.name]
                        spec.execution["tool_call_id"] = call.id
                        spec.execution["operation_id"] = (
                            f"{context_operation_id}:iteration:{iteration}:"
                            f"tool:{call_index}:{call.id or call.name}"
                            if context_operation_id
                            else f"{provider}:tool:{call.id or call.name}"
                        )
                        result = await tool_executor(call.name, args or {})
                    except Exception as exc:
                        # Tool failures are fed back to the model.
                        logger.error(
                            "[Agent loop] tool %r failed: %s",
                            call.name,
                            exc,
                        )
                        result = {"error": str(exc)}

            if (
                rebind_from_operations is not None
                and isinstance(result, dict)
                and result.get("operations")
            ):
                try:
                    added = await rebind_from_operations(result["operations"])
                    if added:
                        iteration_new_tools.extend(added)
                except DuplicateToolNameError as exc:
                    logger.warning(
                        "[Agent loop] rejected ambiguous tool rebind: %s", exc
                    )
                    result = dict(result)
                    result["rebind_error"] = exc.as_dict()
                except Exception as exc:
                    logger.warning(
                        "[Agent loop] tool rebind failed: %s", exc, exc_info=True
                    )

            tool_message = Message(
                role="tool",
                content=json.dumps(result, default=str),
                tool_call_id=call.id,
                name=call.name,
            )
            # Tools opt into vision via `llm_media` refs; blocks stay ~450 B
            # in durable state (hydration happens per provider call).
            tool_message.blocks.extend(image_blocks_from_tool_result(result))
            messages.append(tool_message)
            await persist_transition(
                "message.tool_result",
                operation_id=(
                    f"iteration:{iteration}:tool:{call_index}:{call.id or call.name}"
                ),
                message=tool_message,
                payload={
                    "iteration": iteration,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "outcome": (
                        "error"
                        if isinstance(result, dict) and "error" in result
                        else "success"
                    ),
                },
            )

        if iteration_new_tools:
            current_tools.extend(iteration_new_tools)
            logger.info(
                "[Agent loop] rebound %d tool(s) (total=%d)",
                len(iteration_new_tools),
                len(current_tools),
            )

    terminal = Message(
        role="assistant",
        content=(
            f"[Recursion limit reached: {max_iterations} iterations. "
            "Adjust agent.recursion_limit in llm_defaults.json or simplify the task.]"
        ),
    )
    messages.append(terminal)
    await persist_transition(
        "response.truncated",
        operation_id=f"iteration:{iteration}:truncated",
        message=terminal,
        payload={
            "iteration": iteration,
            "lifetime_usage": asdict(usage),
            "reason": "recursion_limit",
        },
    )
    return {
        "messages": messages,
        "iteration": iteration,
        "thinking_content": "\n\n".join(thinking_parts) or None,
        "truncated": True,
        "usage": usage,
        "response": last_response,
    }


__all__ = [
    "AgentContextTransitionSink",
    "AgentToolSpec",
    "add_usage",
    "run_native_llm_step",
    "run_native_agent_loop",
]
