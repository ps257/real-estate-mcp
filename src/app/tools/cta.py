"""Action/CTA tools — UI-action payloads (buttons, forms) plus the one write path.

Per the PRD "Action Triggering": the AI responds with concrete UI actions. `start_visit_booking`
and `start_consultation` return the FORM SPEC the UI should render, branching on authenticated
vs not (US2.1/US2.2); `submit_booking` takes the answers back and stores them.

The split matters: an agent that mistakes "here is the form" for "the booking is made" tells the
user they have an appointment nobody recorded. So the form tools carry `persisted: false` and
say so in their descriptions, and only submit_booking returns an id worth confirming.

Requires migrations/003_bookings.sql for the write path.
"""

from __future__ import annotations

from datetime import datetime

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..observability import observe_tool
from ..services import bookings as booking_svc
from ..services import listings as listing_svc
from ..services import locations as loc_svc

# Fields collected when the user is NOT authenticated (need contact info).
_GUEST_FIELDS = [
    {"name": "full_name", "label": "Họ và tên", "type": "text", "required": True},
    {"name": "phone", "label": "Số điện thoại", "type": "tel", "required": True},
    {"name": "email", "label": "Email", "type": "email", "required": False},
    {"name": "preferred_time", "label": "Thời gian mong muốn", "type": "datetime", "required": True},
    {"name": "note", "label": "Ghi chú", "type": "textarea", "required": False},
]

# Fields when the user IS authenticated (contact prefilled from profile).
_AUTHED_FIELDS = [
    {"name": "preferred_time", "label": "Thời gian mong muốn", "type": "datetime", "required": True},
    {"name": "note", "label": "Ghi chú", "type": "textarea", "required": False},
]


_KINDS = ("visit_booking", "consultation")

# Fields that live in the `contact` jsonb rather than in a column of their own.
_CONTACT_FIELDS = ("full_name", "phone", "email")


def _fields_for(is_authenticated: bool) -> list[dict]:
    """The form spec a caller was handed — and the exact rule submit_booking validates against.

    Reading the requirements back out of the same constant is the point: if someone makes
    `email` required for guests, the form and the check move together instead of drifting until
    a booking arrives with a field nobody validated.
    """
    return _AUTHED_FIELDS if is_authenticated else _GUEST_FIELDS


def _validate_payload(payload: dict, is_authenticated: bool) -> None:
    allowed = {f["name"] for f in _fields_for(is_authenticated)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        # Silently dropping a key would lose whatever the user typed into it.
        raise ToolError(
            f"Unknown field(s) {', '.join(unknown)} for this form. "
            f"Accepted: {', '.join(sorted(allowed))}."
        )
    missing = sorted(
        f["name"]
        for f in _fields_for(is_authenticated)
        if f["required"] and not str(payload.get(f["name"]) or "").strip()
    )
    if missing:
        raise ToolError(f"Missing required field(s): {', '.join(missing)}.")

    phone = str(payload.get("phone") or "")
    if phone and sum(c.isdigit() for c in phone) < 8:
        raise ToolError("phone does not look like a phone number.")
    email = str(payload.get("email") or "")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise ToolError("email does not look like an email address.")


def _normalise_time(value: object) -> str | None:
    """ISO-8601 in, ISO-8601 out. Reject anything Postgres would choke on later.

    Letting a bad string reach the insert turns a user mistake into a raw PostgREST error that
    names the column and type; catching it here keeps the message about their input.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).isoformat()  # py3.11+ parses a trailing Z
    except ValueError:
        raise ToolError(
            "preferred_time is not an ISO-8601 datetime. "
            "Expected something like '2026-08-10T14:00:00+07:00'."
        ) from None


def _form_payload(action: str, project_id: str, is_authenticated: bool) -> dict:
    project = loc_svc.get_location(project_id)
    if project is None or project.get("level") != "project":
        raise ToolError(f"'{project_id}' is not a known project id.")
    return {
        "action": action,  # UI switches on this
        "project": {"id": project["id"], "name": project["name"]},
        "authenticated": is_authenticated,
        # Copy: returning the module-level list would hand every caller the same objects, so one
        # in-place edit (a UI tweaking a label) would change the form for every later call.
        "fields": [dict(field) for field in _fields_for(is_authenticated)],
        "submit_tool": "submit_booking",  # how the collected answers actually get stored
        "submit_endpoint": f"/api/{action}",  # frontend alternative to the tool call
        # This call stores nothing; submit_booking does. Stated in the payload as well as the
        # docstrings because an agent that treats *this* as "the booking is made" would tell
        # the user it is confirmed while nothing exists.
        "persisted": False,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @observe_tool
    def start_visit_booking(project_id: str, is_authenticated: bool = False) -> dict:
        """Open the "đặt lịch tham quan" (site-visit) form for a project (US2.1).

        Use when the user picks the "Đặt lịch tham quan" CTA or asks to visit a project. Resolve
        the project first with resolve_project / search_projects.

        This call does NOT create a booking — `persisted` is false. It returns the form for the
        UI to render. Never tell the user their visit is booked off the back of it; the booking
        exists only after their answers go through submit_booking, which returns the id to
        confirm with.

        Returns {"action": "visit_booking", "project": {id, name}, "authenticated": bool,
        "fields": [{name, label, type, required}], "submit_tool", "submit_endpoint",
        "persisted": false}.
        Render `fields` as-is rather than inventing your own; a guest form asks full_name, phone,
        email, preferred_time, note, and a signed-in form asks only preferred_time and note
        because the contact details come from the profile. Raises if `project_id` is not a real
        project.

        Args:
            project_id: a project id from resolve_project / search_projects.
            is_authenticated: whether this user is signed in. This is session state the host
                application knows — do not guess it from the conversation. If you are unsure,
                leave it false: asking a signed-in user for their phone again is a small
                annoyance, while marking a guest as signed in produces a request with no
                contact details at all.
        """
        return _form_payload("visit_booking", project_id, is_authenticated)

    @mcp.tool
    @observe_tool
    def start_consultation(project_id: str, is_authenticated: bool = False) -> dict:
        """Open the "tư vấn mua nhà" (buyer consultation) form for a project (US2.2).

        Use when the user picks the "Tư vấn mua nhà" CTA, or asks to speak to an advisor — for
        example when a policy or legal question falls outside what these tools can answer.
        Resolve the project first with resolve_project / search_projects.

        This call does NOT request a consultation — `persisted` is false. It returns the form
        for the UI to render. Never tell the user an advisor will contact them off the back of
        it; that only follows once their answers go through submit_booking.

        Returns the same shape as start_visit_booking with "action": "consultation" —
        {"action", "project": {id, name}, "authenticated", "fields": [{name, label, type,
        required}], "submit_endpoint", "persisted": false}. Here `preferred_time` is when the
        user wants the advisor to call, and `note` is where their question belongs. Raises if
        `project_id` is not a real project.

        Args:
            project_id: a project id from resolve_project / search_projects.
            is_authenticated: whether this user is signed in — session state from the host
                application, not something to infer. When unsure leave it false; see
                start_visit_booking for why.
        """
        return _form_payload("consultation", project_id, is_authenticated)

    @mcp.tool
    @observe_tool
    def submit_booking(
        kind: str,
        project_id: str,
        payload: dict,
        is_authenticated: bool = False,
    ) -> dict:
        """Store a filled-in visit-booking or consultation form. THIS ONE WRITES.

        Use only after the user has actually supplied the answers that start_visit_booking or
        start_consultation asked for. This is the only tool in the server that records anything,
        and a stored booking is a promise that a real person will be contacted — do not call it
        to "check" something, and do not invent values the user did not give you.

        Returns {"booking_id": str, "kind": str, "project": {id, name}, "preferred_time": str
        |null, "created_at": str, "persisted": true, "duplicate_of_existing": bool}. Once you
        have that object you may tell the user their request is recorded, and give them the
        `booking_id` as a reference. `duplicate_of_existing` true means an identical request
        arrived in the last 10 minutes and this call returned the original instead of making a
        second one — still confirm it, just do not say a new one was created.

        Raises rather than storing anything half-right: unknown or missing fields, a phone that
        is not a phone, a `preferred_time` that is not ISO-8601, or a `project_id` that is not a
        real project. Read the message, fix the input, call again.

        Args:
            kind: "visit_booking" for a site visit (US2.1), "consultation" for buyer advice
                (US2.2). Must match the form the user filled.
            project_id: the project the request is about, from resolve_project.
            payload: the user's answers, keyed exactly as the form's field names. Guests send
                full_name, phone, email, preferred_time, note; signed-in users send only
                preferred_time and note. Anything else is rejected rather than dropped.
            is_authenticated: whether this user is signed in — session state from the host
                application, not something to infer. Guessing true for a guest strips the
                contact fields out of the form and stores a request nobody can answer.
        """
        if kind not in _KINDS:
            raise ToolError(f"Unknown kind '{kind}'. Valid: {', '.join(_KINDS)}.")
        project = loc_svc.get_location(project_id)
        if project is None or project.get("level") != "project":
            raise ToolError(f"'{project_id}' is not a known project id.")
        if not isinstance(payload, dict):
            raise ToolError("payload must be an object of the form's field names.")

        _validate_payload(payload, is_authenticated)
        preferred_time = _normalise_time(payload.get("preferred_time"))
        note = str(payload.get("note") or "").strip() or None
        contact = {
            name: str(payload[name]).strip()
            for name in _CONTACT_FIELDS
            if str(payload.get(name) or "").strip()
        }

        existing = booking_svc.find_recent_duplicate(
            kind=kind,
            project_id=project_id,
            phone=contact.get("phone"),
            preferred_time=preferred_time,
        )
        row = existing or booking_svc.create_booking(
            kind=kind,
            project_id=project_id,
            is_authenticated=is_authenticated,
            contact=contact,
            preferred_time=preferred_time,
            note=note,
        )
        return {
            "booking_id": row["id"],
            "kind": row["kind"],
            "project": {"id": project["id"], "name": project["name"]},
            "preferred_time": row.get("preferred_time"),
            "created_at": row["created_at"],
            "persisted": True,
            "duplicate_of_existing": existing is not None,
        }

    @mcp.tool
    @observe_tool
    def listing_cta_actions(listing_id: str) -> dict:
        """Return the four CTA buttons to show under a listing result (US1).

        Use once you are showing a specific listing to the user, to offer the next steps:
        xem tất cả, đặt lịch tham quan, tư vấn mua nhà, xem bản đồ.

        Returns {"listing_id", "project_id", "ctas": [{action, label, next_tool, args}]}. Each
        cta is directly executable: when the user clicks one, call its `next_tool` with its
        `args` exactly as given. `args` is prefilled with the listing's project_id, which the
        booking and "xem tất cả" tools all require. Raises if the listing does not exist.

        When to show these: the UI rule is 1-3 search results -> render each card with these
        CTAs; more than 3 -> summarise and lead with the "xem tất cả" button instead of
        listing everything.

        Args:
            listing_id: the `id` of a listing you are currently showing.
        """
        listing = listing_svc.get_listing_ref(listing_id)
        if listing is None:
            raise ToolError(f"No listing found with id '{listing_id}'.")
        # Every next_tool below needs the project, not the listing — a CTA carrying only the
        # listing id would leave the agent to guess or re-query before it could act.
        args = {"project_id": listing["project_id"]}
        return {
            "listing_id": listing_id,
            "project_id": listing["project_id"],
            "ctas": [
                {"action": "view_all", "label": "Xem tất cả",
                 "next_tool": "list_project_listings", "args": args},
                {"action": "book_visit", "label": "Đặt lịch tham quan",
                 "next_tool": "start_visit_booking", "args": args},
                {"action": "consult", "label": "Tư vấn mua nhà",
                 "next_tool": "start_consultation", "args": args},
                {"action": "view_map", "label": "Xem bản đồ",
                 "next_tool": "map_listings", "args": args},
            ],
        }
