"""JavaScript Executor — Wave 11.C migration."""

from __future__ import annotations

from typing import Any

from services.plugin import NodeContext, NodeUserError, Operation

from .._base import CodeExecutorBase, CodeExecutorParams


class JavaScriptExecutorNode(CodeExecutorBase):
    type = "javascriptExecutor"
    display_name = "JavaScript Executor"
    subtitle = "Run JS"
    description = "Execute JavaScript code via persistent Node.js server"
    tool_name = "javascript_code"
    tool_description = "Execute JavaScript code for calculations, data processing, and JSON manipulation. Set output variable with result."

    @Operation("execute")
    async def execute_op(self, ctx: NodeContext, params: CodeExecutorParams) -> Any:
        """Inlined from handlers/code.py (Wave 11.D.2). Dispatches to the
        backend-supervised Node.js sidecar; ``acquire_client`` spawns it
        on demand (first JS/TS execution) in every run mode."""
        from aiohttp import ClientConnectorError

        from .._nodejs import acquire_client, executor_base_url

        if not params.code.strip():
            raise NodeUserError("No code provided")
        input_data = dict(ctx.raw.get("connected_outputs") or {})
        input_data["workspace_dir"] = ctx.workspace_dir or ""

        try:
            client = await acquire_client()
            result = await client.execute(
                code=params.code,
                input_data=input_data,
                timeout=params.timeout * 1000,
                language="javascript",
            )
        except (ClientConnectorError, RuntimeError) as exc:
            # Spawn or connect failed -- bare errors are opaque to the
            # LLM. Tell it what's actually wrong and which tool can
            # substitute.
            raise NodeUserError(
                "JavaScript executor is unavailable (Node.js sidecar at "
                f"{executor_base_url()}). Fall back to python_executor "
                f"for similar logic. Underlying: {exc}"
            ) from exc

        if not result.get("success"):
            raise NodeUserError(result.get("error") or "JavaScript executor failed")
        return {
            "output": result.get("output"),
            "console_output": result.get("console_output", ""),
        }
