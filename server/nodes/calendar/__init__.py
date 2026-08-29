"""Google Calendar capability nodes.

Provides read-only calendar access for DCS souls.

Dragon gate (Orion D4): GCP write scopes touching UpStage production are
NOT enabled here. This node uses ``calendar.readonly`` scope only.
Write access (creating/modifying events) requires Dragon's sign-off and
Argus re-proof of the credential surface before enabling.

Credentials: WORKFORCE_GOOGLE_CALENDAR_CREDENTIALS_PATH in
``~/.cipheros/workforce/.env`` — path to a service account JSON file.
If not configured, all operations raise CredentialsNotConfigured (never
crash with an unhandled exception).

Nodes exposed:
- ``calendarListEvents`` — list events in a calendar between two datetimes
- ``calendarGetEvent``   — fetch a single event by id
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue


class CredentialsNotConfigured(NodeUserError):
    """Raised when Google Calendar credentials are not configured."""


def _get_calendar_service():
    """Build the Google Calendar API client from isolated-env credentials.

    Argus-flagged: credential loading surface. Uses
    ``server/services/workforce_env.py`` — never ``os.environ``.
    Raises CredentialsNotConfigured if path is missing or file absent.
    """
    import json
    from pathlib import Path

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    from services.workforce_env import get_workforce_credential

    creds_path_str = get_workforce_credential("WORKFORCE_GOOGLE_CALENDAR_CREDENTIALS_PATH")
    if not creds_path_str:
        raise CredentialsNotConfigured(
            "Google Calendar credentials not configured. "
            "Set WORKFORCE_GOOGLE_CALENDAR_CREDENTIALS_PATH in ~/.cipheros/workforce/.env"
        )

    creds_path = Path(creds_path_str)
    if not creds_path.exists():
        raise CredentialsNotConfigured(
            f"Google Calendar credentials file not found: {creds_path}\n"
            "Verify WORKFORCE_GOOGLE_CALENDAR_CREDENTIALS_PATH in ~/.cipheros/workforce/.env"
        )

    # Read-only scope only — write scope requires Dragon's explicit sign-off (D4 Dragon gate)
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    credentials = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=scopes,
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


class CalendarListEventsParams(BaseModel):
    calendar_id: str = Field(default="primary", description="Calendar ID or 'primary'")
    time_min: str = Field(..., description="Start of range (RFC3339 datetime, e.g. 2026-08-28T00:00:00Z)")
    time_max: str = Field(..., description="End of range (RFC3339 datetime)")
    max_results: int = Field(default=10, ge=1, le=250)

    model_config = ConfigDict(extra="ignore")


class CalendarListEventsOutput(BaseModel):
    events: list[dict]
    count: int
    calendar_id: str

    model_config = ConfigDict(extra="allow")


class CalendarGetEventParams(BaseModel):
    calendar_id: str = Field(default="primary")
    event_id: str = Field(..., description="Google Calendar event ID")

    model_config = ConfigDict(extra="ignore")


class CalendarGetEventOutput(BaseModel):
    event: Optional[dict]

    model_config = ConfigDict(extra="allow")


class CalendarListEventsNode(ActionNode):
    type = "calendarListEvents"
    display_name = "Calendar List Events"
    subtitle = "Google Calendar"
    group = ("calendar", "tool")
    description = "List Google Calendar events between two datetimes (read-only)"
    component_kind = "square"
    tool_name = "calendar_list_events"
    tool_description = (
        "List Google Calendar events between time_min and time_max. "
        "calendar_id defaults to 'primary'. Returns event summaries, times, and IDs. "
        "Read-only — write access is Dragon-gated."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.DEFAULT
    usable_as_tool = True

    Params = CalendarListEventsParams
    Output = CalendarListEventsOutput

    @Operation("list")
    async def list_op(self, ctx: NodeContext, params: CalendarListEventsParams) -> Any:
        import asyncio

        service = _get_calendar_service()

        def _fetch():
            result = (
                service.events()
                .list(
                    calendarId=params.calendar_id,
                    timeMin=params.time_min,
                    timeMax=params.time_max,
                    maxResults=params.max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return result.get("items", [])

        events = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return {"events": events, "count": len(events), "calendar_id": params.calendar_id}


class CalendarGetEventNode(ActionNode):
    type = "calendarGetEvent"
    display_name = "Calendar Get Event"
    subtitle = "Google Calendar"
    group = ("calendar", "tool")
    description = "Fetch a single Google Calendar event by ID (read-only)"
    component_kind = "square"
    tool_name = "calendar_get_event"
    tool_description = "Fetch a Google Calendar event by event_id. Read-only."
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.DEFAULT
    usable_as_tool = True

    Params = CalendarGetEventParams
    Output = CalendarGetEventOutput

    @Operation("get")
    async def get_op(self, ctx: NodeContext, params: CalendarGetEventParams) -> Any:
        import asyncio

        service = _get_calendar_service()

        def _fetch():
            return service.events().get(calendarId=params.calendar_id, eventId=params.event_id).execute()

        event = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return {"event": event}
