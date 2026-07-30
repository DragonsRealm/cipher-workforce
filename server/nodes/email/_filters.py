"""Email event-trigger filter builder (Wave 11.I, milestone K).

Moved from ``services/event_waiter.build_email_filter``, then extended to
honour ``filter_query`` -- which was declared on ``EmailReceiveParams`` and
rendered in the node panel, but read by nothing.
"""

from __future__ import annotations

from typing import Callable, Dict

# Fields a free-text filter searches, in the shape `_format_message` emits.
_SEARCHABLE_FIELDS = ("subject", "from", "to", "body")


def build_filter(params: Dict) -> Callable[[Dict], bool]:
    """Build filter for email events (Himalaya IMAP polling).

    ``folder`` matches exactly, with ``"all"`` as a wildcard. ``filter_query``
    is a case-insensitive substring match across subject / from / to / body --
    deliberately not IMAP search syntax, which belongs server-side in the
    ``search`` operation rather than in a post-fetch predicate.
    """
    folder_filter = params.get("folder") or "INBOX"
    query = (params.get("filter_query") or "").strip().lower()

    def matches(data: Dict) -> bool:
        if folder_filter and folder_filter != "all":
            if data.get("folder", "") != folder_filter:
                return False

        if query:
            haystack = " ".join(
                str(data.get(field) or "") for field in _SEARCHABLE_FIELDS
            ).lower()
            if query not in haystack:
                return False

        return True

    return matches
