"""Listing access: search with filters, detail, list-by-project, and aggregate stats.

Reads go through the `listings_clean` view, not the base table — see LISTINGS below.

Note the data-quality realities of the `listings` table (see docs/SCHEMA.md):
  - area_m2 / bathrooms / floor_num are proper numeric columns in the current DB, but
    shaping.py still coerces them defensively — earlier snapshots stored them as TEXT.
  - the raw `bedrooms` column is unreliable below 2 and is never selected; the view derives
    `bedrooms_norm` from the listing title instead (migrations/002).
  - price_vnd / price_per_m2_vnd are bigint -> safe to filter/sort in SQL.
  - status has one non-null value ('ĐANG BÁN', 1091 rows) and is NULL on the other 1264, so it
    means "listed for sale" or "unknown" — never "sold". normalize_status stays as a guard.
  - NULL rates over all 2355 rows: bathrooms 23%, view 22%, legal_status 12%, building_id 10%,
    bedrooms 6%, area_m2 6%, price_vnd 1%. Filtering on a column silently drops its NULL rows.

Counts here are from the whole table. Don't read them off a `.limit(1000)` sample: PostgREST
returns the first 1000 rows in physical order, which is one crawl block and badly skewed —
floor_band looked 100% NULL that way while it is really only 46% NULL.
"""

from __future__ import annotations

from postgrest.exceptions import APIError

from ..db import get_client
from ..observability import mark_current_observation_error, observe_operation
from ..shaping import (
    LISTING_CARD_COLUMNS,
    LISTING_DETAIL_COLUMNS,
    shape_listing_card,
    shape_listing_detail,
    to_float,
    to_int,
)

# Every read goes through the view, never the base table: it adds `bedrooms_norm` and
# `has_flex_room` and is otherwise `SELECT listings.*`, so there is no reason to mix the two.
# Requires migrations/002_listings_clean.sql — without it PostgREST answers
# "Could not find the table public.listings_clean".
LISTINGS = "listings_clean"


@observe_operation("db.listings.search", as_type="retriever")
def search_listings(
    project_id: str | None,
    project_ids: list[str] | None,
    building_id: str | None,
    property_type: str | None,
    min_price_vnd: int | None,
    max_price_vnd: int | None,
    bedrooms: int | None,
    min_bedrooms: int | None,
    max_bedrooms: int | None,
    min_area_m2: float | None,
    max_area_m2: float | None,
    limit: int,
) -> list[dict]:
    """Filtered listing search, cheapest first. Every filter runs in SQL.

    `bedrooms` used to be filtered in Python over `limit * 3` fetched rows, which silently
    under-returned: Vinhomes Grand Park has 587 listings, and sorted by price the first
    2-bedroom sits at index 144, so `bedrooms=2, limit=10` fetched the cheapest 30 rows,
    matched none of them and answered "no listings" over 251 real matches. Any post-fetch
    filter has this failure mode — over-fetching by a constant factor only moves the cliff.
    Every filter added since then goes straight into the query for the same reason.

    Bedroom filters match `bedrooms_norm` from the view, not the raw column. The raw column
    called 139 studios "1 bedroom" and gave 126 shophouses a placeholder 1; the view reads the
    count out of the listing title instead, which lifted studios from 188 to 379 and left the
    non-residential rows NULL. A NULL is excluded by any bedroom filter, which is the point.

    There is no `province` filter here because `listings` has no province column. Callers that
    want one resolve it to project ids with `locations.project_ids_in_province` and pass them as
    `project_ids` — that is what the search_listings_by_province tool does.
    """
    q = get_client().table(LISTINGS).select(LISTING_CARD_COLUMNS)
    if project_id:
        q = q.eq("project_id", project_id)
    if project_ids is not None:
        # An empty list must not reach PostgREST: `in.()` is not a valid filter and the request
        # would come back unfiltered, turning "no projects in that province" into "every
        # listing we have". Callers should not send one, but the cost of being sure is a line.
        if not project_ids:
            return []
        q = q.in_("project_id", project_ids)
    if building_id:
        q = q.eq("building_id", building_id)
    if property_type:
        q = q.eq("property_type", property_type)
    if bedrooms is not None:
        q = q.eq("bedrooms_norm", bedrooms)
    if min_bedrooms is not None:
        q = q.gte("bedrooms_norm", min_bedrooms)
    if max_bedrooms is not None:
        q = q.lte("bedrooms_norm", max_bedrooms)
    if min_area_m2 is not None:
        q = q.gte("area_m2", min_area_m2)
    if max_area_m2 is not None:
        q = q.lte("area_m2", max_area_m2)
    if min_price_vnd is not None:
        q = q.gte("price_vnd", min_price_vnd)
    if max_price_vnd is not None:
        q = q.lte("price_vnd", max_price_vnd)
    # `id` breaks price ties so repeating a search returns the same cards (see list_by_project).
    rows = q.order("price_vnd", desc=False).order("id", desc=False).limit(limit).execute().data
    cards = [shape_listing_card(r) for r in rows or []]
    return _enrich_location_info(cards)


def _enrich_location_info(items: list[dict]) -> list[dict]:
    """Attach project name, province, and district to shaped listing dicts."""
    project_ids = list({r["project_id"] for r in items if r.get("project_id")})
    if not project_ids:
        return items
    try:
        loc_rows = (
            get_client()
            .table("locations")
            .select("id,name,province,district")
            .in_("id", project_ids)
            .execute()
            .data
            or []
        )
        loc_map = {l["id"]: l for l in loc_rows}
        for item in items:
            loc = loc_map.get(item.get("project_id"))
            if loc:
                item["province"] = loc.get("province")
                item["district"] = loc.get("district")
                item["project_name"] = loc.get("name")
                parts = [loc.get("district"), loc.get("province")]
                addr_parts = [p for p in parts if p]
                if addr_parts:
                    item["address"] = ", ".join(addr_parts)
    except Exception:
        pass
    return items


@observe_operation("db.listings.get", as_type="retriever")
def get_listing(listing_id: str) -> dict | None:
    rows = (
        get_client()
        .table(LISTINGS)
        .select(LISTING_DETAIL_COLUMNS)
        .eq("id", listing_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None
    detail = shape_listing_detail(rows[0])
    return _enrich_location_info([detail])[0]


@observe_operation("db.listings.page", as_type="retriever")
def list_by_project(project_id: str, limit: int, offset: int) -> dict:
    """One page of a project's listings, cheapest first, plus the total that page came from.

    Returns the total because this backs the "xem tất cả" view and the biggest projects hold
    far more than one page — Vinhomes Ocean Park has 685 listings, Grand Park 623, and 9 of
    the 57 projects exceed the default page of 50. Returning a bare list let the caller
    present 50 of 685 as if it were everything.

    `count="exact"` rides along on the same request, so the total costs no extra round trip.
    """
    try:
        res = _project_page(project_id, limit, offset)
    except APIError as exc:
        # PostgREST answers 416 for an offset past the last row, whether the window is set via
        # the Range header or ?offset=. Paging off the end is a normal thing for a caller to do
        # and its error text leaks row counts, so report an empty page instead.
        if exc.code != "PGRST103":
            raise
        return {
            "total": _project_total(project_id),
            "offset": offset,
            "count": 0,
            "has_more": False,
            "listings": [],
        }
    listings = [shape_listing_card(r) for r in res.data or []]
    listings = _enrich_location_info(listings)
    return {
        "total": res.count,
        "offset": offset,
        "count": len(listings),
        "has_more": offset + len(listings) < (res.count or 0),
        "listings": listings,
    }


def _project_total(project_id: str) -> int:
    count = (
        get_client()
        .table(LISTINGS)
        .select("id", count="exact")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
        .count
    )
    return count or 0


def _project_page(project_id: str, limit: int, offset: int):
    return (
        get_client()
        .table(LISTINGS)
        .select(LISTING_CARD_COLUMNS, count="exact")
        .eq("project_id", project_id)
        .order("price_vnd", desc=False)
        # `id` breaks price ties. Without it the sort is not a total order — Ocean Park has ten
        # duplicated prices in its 60 cheapest alone — and Postgres may order a tie group
        # differently per query, so page 2 can repeat or skip rows that page 1 already showed.
        .order("id", desc=False)
        .range(offset, offset + limit - 1)  # inclusive on both ends
        .execute()
    )


@observe_operation("db.listings.get-ref", as_type="retriever")
def get_listing_ref(listing_id: str) -> dict | None:
    """id + project_id only — enough to prove a listing exists and to route its CTAs.

    Deliberately not `get_listing`: that pulls the full detail row including the images array
    (up to 40 URLs) just to read one foreign key.
    """
    rows = (
        get_client()
        .table(LISTINGS)
        .select("id,project_id")
        .eq("id", listing_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


@observe_operation("db.listings.get-many", as_type="retriever")
def get_many(listing_ids: list[str]) -> list[dict]:
    """Fetch several listings by id (used by compare), attaching province for context evaluation."""
    rows = (
        get_client()
        .table(LISTINGS)
        .select(LISTING_DETAIL_COLUMNS)
        .in_("id", listing_ids)
        .order("price_vnd", desc=False)
        .execute()
        .data
        or []
    )
    # Fallback tìm kiếm không phân biệt hoa thường nếu có ID bị lệch case
    if len(rows) < len(listing_ids):
        found_ids = {r["id"].lower() for r in rows if r.get("id")}
        missing_ids = [lid for lid in listing_ids if lid.lower() not in found_ids]
        for mid in missing_ids:
            alt_rows = (
                get_client()
                .table(LISTINGS)
                .select(LISTING_DETAIL_COLUMNS)
                .ilike("id", mid)
                .limit(1)
                .execute()
                .data
                or []
            )
            if alt_rows:
                rows.append(alt_rows[0])

    shaped = [shape_listing_detail(r) for r in rows]
    # Sort shaped listings by price_vnd ascending
    shaped.sort(key=lambda x: (x.get("price_vnd") or 0))
    return _enrich_location_info(shaped)




@observe_operation("db.listings.project-stats", as_type="retriever")
def project_price_stats(project_id: str) -> dict:
    """Aggregate price/area stats for one project (computed in Python over the project's rows).

    Top-level price stats intentionally keep the original US4 contract, but callers should prefer
    `by_price_type` when speaking to users: `asking` is a seller's asking price, while `estimate`
    is a source-computed reference price. `coverage` tells the agent how many rows each aggregate
    is actually based on, because NULL values are omitted from min/max/avg calculations.

    TODO(student, phase 2): move this to a Postgres RPC (avg/percentile over price_vnd) so we
    don't pull every row; keeps latency within the PRD's <3s budget at scale.
    """
    rows = (
        get_client()
        .table(LISTINGS)
        .select("price_vnd,price_per_m2_vnd,price_type,area_m2,property_type,bedrooms_norm")
        .eq("project_id", project_id)
        .execute()
        .data
        or []
    )
    prices = [r["price_vnd"] for r in rows if r.get("price_vnd") is not None]
    ppm2 = [r["price_per_m2_vnd"] for r in rows if r.get("price_per_m2_vnd") is not None]
    areas = [a for a in (to_float(r.get("area_m2")) for r in rows) if a is not None]
    # `bedrooms_norm`, not `bedrooms`: the SELECT above reads the listings_clean view, where the
    # count is derived from the listing title. The raw column called 139 studios "1 bedroom".
    beds = [b for b in (to_int(r.get("bedrooms_norm")) for r in rows) if b is not None]
    ptypes: dict[str, int] = {}
    for r in rows:
        pt = r.get("property_type") or "unknown"
        ptypes[pt] = ptypes.get(pt, 0) + 1

    def _avg(xs: list[int | float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    def _money_stats(xs: list[int]) -> dict:
        return {
            "min": min(xs) if xs else None,
            "max": max(xs) if xs else None,
            "avg": round(_avg(xs)) if xs and _avg(xs) is not None else None,
        }

    def _number_stats(xs: list[int | float]) -> dict:
        return {
            "min": min(xs) if xs else None,
            "max": max(xs) if xs else None,
            "avg": _avg(xs),
        }

    by_price_type: dict[str, dict] = {}
    for price_type in sorted({r.get("price_type") or "unknown" for r in rows}):
        subset = [r for r in rows if (r.get("price_type") or "unknown") == price_type]
        subset_prices = [r["price_vnd"] for r in subset if r.get("price_vnd") is not None]
        subset_ppm2 = [r["price_per_m2_vnd"] for r in subset if r.get("price_per_m2_vnd") is not None]
        by_price_type[price_type] = {
            "count": len(subset),
            "price_vnd": _money_stats(subset_prices),
            "price_per_m2_vnd": _money_stats(subset_ppm2),
            "coverage": {
                "price_vnd_count": len(subset_prices),
                "price_per_m2_vnd_count": len(subset_ppm2),
            },
        }

    return {
        "project_id": project_id,
        "count": len(rows),
        "price_vnd": _money_stats(prices),
        "price_per_m2_vnd": _money_stats(ppm2),
        "area_m2": _number_stats(areas),
        "bedrooms_range": {"min": min(beds) if beds else None, "max": max(beds) if beds else None},
        "by_property_type": ptypes,
        "by_price_type": by_price_type,
        "coverage": {
            "total": len(rows),
            "price_vnd_count": len(prices),
            "price_per_m2_vnd_count": len(ppm2),
            "area_m2_count": len(areas),
            "bedrooms_count": len(beds),
        },
    }


@observe_operation("db.listings.map-points", as_type="retriever")
def map_points(
    project_id: str | None,
    property_type: str | None,
    min_price_vnd: int | None,
    max_price_vnd: int | None,
    bedrooms: int | None,
    limit: int,
    listing_ids: list[str] | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = None
) -> list[dict]:
    """Lightweight lat/lng points for the map view (US5)."""
    base_q = (
        get_client()
        .table(LISTINGS)
        .select("id,title,property_type,price_vnd,lat,lng")
        .not_.is_("lat", "null")
        .not_.is_("lng", "null")
    )
    
    if listing_ids:
        rows = base_q.in_("id", listing_ids).limit(limit).execute().data or []
        if rows:
            return rows
        
        # Fallback to project_id if the specific listings have no lat/lng
        base_q = (
            get_client()
            .table(LISTINGS)
            .select("id,title,property_type,price_vnd,lat,lng")
            .not_.is_("lat", "null")
            .not_.is_("lng", "null")
        )

    q = base_q
    if project_id:
        q = q.eq("project_id", project_id)
    if property_type:
        q = q.eq("property_type", property_type)
    if bedrooms is not None:
        q = q.eq("bedrooms_norm", bedrooms)
    if min_bedrooms is not None:
        q = q.gte("bedrooms_norm", min_bedrooms)
    if max_bedrooms is not None:
        q = q.lte("bedrooms_norm", max_bedrooms)
    if min_price_vnd is not None:
        q = q.gte("price_vnd", min_price_vnd)
    if max_price_vnd is not None:
        q = q.lte("price_vnd", max_price_vnd)
    rows = q.limit(limit).execute().data or []
    return [
        {
            "id": r["id"],
            "title": r.get("title"),
            "property_type": r.get("property_type"),
            "price_vnd": r.get("price_vnd"),
            "lat": r["lat"],
            "lng": r["lng"],
        }
        for r in rows
    ]
