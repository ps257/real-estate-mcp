"""Booking writes (US2.1 / US2.2) — the only module in the project that inserts rows.

The `bookings` table holds personal data (name, phone, email of real people) and is locked down
accordingly: RLS on with no policies, plus GRANTs revoked from anon/authenticated. Only the
service-role key the MCP server uses can reach it. See migrations/003_bookings.sql.

Everything here goes through PostgREST like the read paths; there is no raw SQL and no string
interpolation of user input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..db import get_client
from ..observability import observe_operation

BOOKINGS = "bookings"

# A booking that reaches the DB twice is worse than one that fails: a sales team calls the same
# person about the same unit twice, and the user has no way to cancel the extra one. Agents
# retry on timeouts and users double-click, so treat an identical request inside this window as
# the same request and hand back the original id.
DEDUPE_WINDOW = timedelta(minutes=10)

# Columns worth returning as the confirmation receipt.
BOOKING_COLUMNS = "id,kind,project_id,is_authenticated,contact,preferred_time,note,created_at"


@observe_operation("db.bookings.find-duplicate", as_type="retriever")
def find_recent_duplicate(
    kind: str,
    project_id: str,
    phone: str | None,
    preferred_time: str | None,
) -> dict | None:
    """The same request already recorded a moment ago, if there is one.

    Matches on phone where we have one, because that is what identifies a person across two
    calls. Signed-in users send no phone (it lives in their profile), so those fall back to the
    requested time — weaker, but it still catches a double submit of the same form.
    """
    since = (datetime.now(UTC) - DEDUPE_WINDOW).isoformat()
    q = (
        get_client()
        .table(BOOKINGS)
        .select(BOOKING_COLUMNS)
        .eq("kind", kind)
        .eq("project_id", project_id)
        .gte("created_at", since)
    )
    if phone:
        q = q.eq("contact->>phone", phone)
    elif preferred_time:
        q = q.eq("preferred_time", preferred_time)
    else:
        # Nothing distinguishing to match on; let the insert through rather than guessing.
        return None
    rows = q.order("created_at", desc=True).limit(1).execute().data or []
    return rows[0] if rows else None


@observe_operation("db.bookings.insert", as_type="span")
def create_booking(
    kind: str,
    project_id: str,
    is_authenticated: bool,
    contact: dict,
    preferred_time: str | None,
    note: str | None,
) -> dict:
    """Insert one booking and return the stored row, including the id the DB generated."""
    rows = (
        get_client()
        .table(BOOKINGS)
        .insert(
            {
                "kind": kind,
                "project_id": project_id,
                "is_authenticated": is_authenticated,
                "contact": contact,
                "preferred_time": preferred_time,
                "note": note,
            }
        )
        .execute()
        .data
        or []
    )
    if not rows:
        raise RuntimeError("insert into bookings returned no row")
    return rows[0]
