"""Listing tools (US1 results, US6 compare, listing detail)."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..observability import observe_tool
from ..services import listings as svc
from ..services import locations as loc_svc
from ..shaping import compute_comparison_insights

# Known property types in the data (US1 filtering). Vietnamese slugs as stored.
PROPERTY_TYPES = (
    "can_ho",  # apartment (dominant)
    "lien_ke",  # townhouse
    "nha_pho",  # street house
    "shophouse",
    "thuong_mai_dich_vu",  # commercial/service
    "biet_thu_don_lap",  # detached villa
    "biet_thu_song_lap",  # semi-detached villa
    "biet_thu_tu_lap",  # quad villa
)


def _check_property_type(value: str | None) -> None:
    if value and value not in PROPERTY_TYPES:
        raise ToolError(f"Unknown property_type '{value}'. Valid: {', '.join(PROPERTY_TYPES)}.")


def _check_ranges(bounds: tuple[tuple[str, float | None, float | None], ...]) -> None:
    """An inverted range matches nothing in SQL, which reads to the agent as "no such unit
    exists" rather than "you asked for an impossible window". Say which bound it was.
    """
    for name, low, high in bounds:
        if low is not None and high is not None and low > high:
            raise ToolError(f"min_{name} ({low}) is greater than max_{name} ({high}).")


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @observe_tool
    def search_listings(
        project_id: str | None = None,
        building_id: str | None = None,
        property_type: str | None = None,
        min_price_vnd: int | None = None,
        max_price_vnd: int | None = None,
        bedrooms: int | None = None,
        min_bedrooms: int | None = None,
        max_bedrooms: int | None = None,
        min_area_m2: float | None = None,
        max_area_m2: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search property LISTINGS with filters. Use after the project is known (US1 results).

        Returns a list of listing cards {id, title, url, source, project_id, building_id,
        property_type, area_m2, bedrooms, has_flex_room, bathrooms, price_vnd,
        price_per_m2_vnd, price_type, status, lat, lng, thumbnail}, cheapest first. An empty
        list means nothing matched the filters, not that the search failed — offer to relax a
        filter rather than reporting an error.

        Read `price_type` on every card before quoting its `price_vnd`: "asking" is a price the
        seller is asking, "estimate" (1264 of 2355 listings) is a figure the source computed and
        nobody has asked for. Say which one you are quoting. Note the price filters below apply
        to both kinds, so a price range mixes asked prices with estimated ones.

        How to present the result: 1-3 cards -> show them with the CTA buttons from
        listing_cta_actions; more than 3 -> summarise and offer a "xem tất cả" button backed by
        list_project_listings.

        Filters combine with AND and all run in the database, so `limit` caps the cheapest
        matches rather than hiding some. Note that `bedrooms` is missing on 6% of listings and
        `area_m2` on 6%, so filtering on them drops listings that simply lack the field rather
        than ones that fail the test.

        `bedrooms` is read out of each listing's title, so it agrees with what the seller
        advertised: 0 means studio, and a unit whose title never states a count (mostly
        shophouses and townhouses) has `bedrooms: null` and is excluded by any bedroom filter.
        `has_flex_room` marks the "+1" in "2 PN + 1" — a multi-purpose room, not a bedroom, so
        those units still count as 2.

        Args:
            project_id: restrict to one project, e.g. "oh:amber-riverside". Strongly recommended;
                without it the search spans every project.
            building_id: restrict to one tower, using an id from list_project_buildings. Only
                "building"-level ids match; a cluster id returns nothing.
            property_type: one of can_ho, lien_ke, nha_pho, shophouse, thuong_mai_dich_vu,
                biet_thu_don_lap, biet_thu_song_lap, biet_thu_tu_lap. Anything else raises.
            min_price_vnd: lowest acceptable total price in VND (e.g. 3000000000 for 3 tỷ).
            max_price_vnd: highest acceptable total price in VND.
            bedrooms: exact bedroom count, 0-4 (0 = studio). Use this OR the min/max pair,
                not both — they combine with AND, so bedrooms=2 with min_bedrooms=3 matches
                nothing.
            min_bedrooms: lowest acceptable bedroom count, for "từ N phòng ngủ trở lên".
            max_bedrooms: highest acceptable bedroom count.
            min_area_m2: smallest acceptable floor area. The stock runs 24.5-162 m2; the
                middle half sits between 43 and 64 m2, so 50 m2 is an ordinary floor here,
                not a large one.
            max_area_m2: largest acceptable floor area.
            limit: max cards to return (default 10).
        """
        _check_property_type(property_type)
        _check_ranges((
            ("price_vnd", min_price_vnd, max_price_vnd),
            ("bedrooms", min_bedrooms, max_bedrooms),
            ("area_m2", min_area_m2, max_area_m2),
        ))
        return svc.search_listings(
            project_id=project_id,
            project_ids=None,
            building_id=building_id,
            property_type=property_type,
            min_price_vnd=min_price_vnd,
            max_price_vnd=max_price_vnd,
            bedrooms=bedrooms,
            min_bedrooms=min_bedrooms,
            max_bedrooms=max_bedrooms,
            min_area_m2=min_area_m2,
            max_area_m2=max_area_m2,
            limit=limit,
        )

    @mcp.tool
    @observe_tool
    def search_listings_by_province(
        province: str,
        property_type: str | None = None,
        min_price_vnd: int | None = None,
        max_price_vnd: int | None = None,
        bedrooms: int | None = None,
        min_bedrooms: int | None = None,
        max_bedrooms: int | None = None,
        min_area_m2: float | None = None,
        max_area_m2: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search LISTINGS across a whole province instead of one project.

        Use when the user names a place rather than a project — "chung cư ở Hà Nội dưới 3 tỷ",
        "căn hộ TP.HCM 2 phòng ngủ". If they have already settled on a project, use
        search_listings, which is one query instead of two.

        Returns the same listing cards as search_listings, cheapest first across every project
        in that province, so read `price_type` before quoting any price and apply the same
        1-3-cards rule when presenting them. Cards carry `project_id`; group by it when the
        results span several projects, because "rẻ nhất trong tỉnh" spread over five projects
        is rarely what the user wants to see as a flat list.

        Listings hold no province of their own, so this resolves the province to its projects
        first and then filters on those. Raises if no project sits in that province — use
        list_provinces to see which ones do, and pass the name back accented and spelled as
        that tool returned it.

        Args:
            province: province name, accented, e.g. "Hà Nội", "Hồ Chí Minh". Case-insensitive
                and partial names match, but unaccented text does not.
            property_type: one of can_ho, lien_ke, nha_pho, shophouse, thuong_mai_dich_vu,
                biet_thu_don_lap, biet_thu_song_lap, biet_thu_tu_lap. Anything else raises.
            min_price_vnd: lowest acceptable total price in VND (e.g. 3000000000 for 3 tỷ).
            max_price_vnd: highest acceptable total price in VND.
            bedrooms: exact bedroom count, 0-4 (0 = studio).
            min_bedrooms: lowest acceptable bedroom count.
            max_bedrooms: highest acceptable bedroom count.
            min_area_m2: smallest acceptable floor area in m2.
            max_area_m2: largest acceptable floor area in m2.
            limit: max cards to return (default 10).
        """
        _check_property_type(property_type)
        _check_ranges((
            ("price_vnd", min_price_vnd, max_price_vnd),
            ("bedrooms", min_bedrooms, max_bedrooms),
            ("area_m2", min_area_m2, max_area_m2),
        ))
        project_ids = loc_svc.project_ids_in_province(province)
        if not project_ids:
            # Distinguish "we have nothing in that province" from "no unit matched your
            # filters" — the agent should offer a different province, not a looser price.
            raise ToolError(
                f"No project found in province '{province}'. "
                f"Known provinces: {', '.join(loc_svc.list_provinces())}."
            )
        return svc.search_listings(
            project_id=None,
            project_ids=project_ids,
            building_id=None,
            property_type=property_type,
            min_price_vnd=min_price_vnd,
            max_price_vnd=max_price_vnd,
            bedrooms=bedrooms,
            min_bedrooms=min_bedrooms,
            max_bedrooms=max_bedrooms,
            min_area_m2=min_area_m2,
            max_area_m2=max_area_m2,
            limit=limit,
        )

    @mcp.tool
    @observe_tool
    def get_listing(listing_id: str) -> dict:
        """Get the full detail of one listing — the detail page in US1.

        Use when the user asks about one specific unit they picked from a search result, or
        before answering a question a search card cannot answer (floor, view, legal status,
        furnishing, photos).

        Returns one object with every card field (id, title, url, source, project_id,
        building_id, property_type, area_m2, bedrooms, has_flex_room, bathrooms, price_vnd,
        price_per_m2_vnd, price_type, status, lat, lng, thumbnail) plus the detail-only fields:
        floor_num, floor_band, direction_balcony, view, legal_status, furnishing, usage_status,
        area_type, image_count, images, first_seen, last_seen, crawled_at. Raises if the id
        does not exist, so a returned object is always a real listing.

        Reading the result honestly:
        - CHECK `price_type` BEFORE QUOTING `price_vnd`. It is "asking" on 1091 listings (a real
          price the seller is asking) but "estimate" on 1264 (a figure the source computed, that
          nobody has asked for). Always say which one you are quoting — "giá chào bán" for
          asking, "giá tham khảo do nguồn ước tính" for estimate. Presenting an estimate as
          an asking price misstates the cost, and this tool does not do valuation.
        - The data is two catalogues stacked, and `source` tells you which row you are reading:
          "vinhomes-market" carries status and floor_num but often lacks bathrooms/view;
          "onehousing" carries bathrooms/view/floor_band but never status, and all of its prices
          are estimates. So a null usually means "this source does not publish that field",
          not "the unit lacks it" — say "chưa có thông tin" rather than guessing.
        - `status` is either "ĐANG BÁN" or null; null means unknown, never "đã bán". Every
          onehousing row is null here, so absence of status says nothing about availability.
        - `images` is capped at 40 URLs while `image_count` is the count at the source, so
          image_count > len(images) on about a third of listings. Cite len(images) for what you
          can actually show, and `url` for the full gallery.
        - `area_type` is "thong_thuy" (carpet area) or "unknown"; every onehousing row is
          "unknown", so price-per-m2 is not strictly comparable across the two sources.

        Args:
            listing_id: the `id` from a search result card, e.g. one returned by search_listings.
        """
        row = svc.get_listing(listing_id)
        if row is None:
            raise ToolError(f"No listing found with id '{listing_id}'.")
        return row

    @mcp.tool
    @observe_tool
    def list_project_listings(project_id: str, limit: int = 50, offset: int = 0) -> dict:
        """Page through every listing in a project — the "xem tất cả" view.

        Use when a search returned more than 3 results, or the user asks to see everything in a
        project. For a filtered subset (price, bedrooms, tower) use search_listings instead.

        Returns {"total": int, "offset": int, "count": int, "has_more": bool, "listings": [...]}
        where `listings` holds the same card objects as search_listings, cheapest first, and
        `total` is how many the project has in all. Tell the user the total, not the page size:
        the largest projects hold 685 and 623 listings, so a first page of 50 is a small slice.
        When `has_more` is true, fetch the next page by calling again with
        offset = offset + count. Raises if `project_id` is not a real project.

        A page mixes both price kinds — check each card's `price_type` ("asking" vs "estimate")
        before quoting it, and do not compare the two as if they were the same measurement.

        Args:
            project_id: a project id from search_projects / resolve_project.
            limit: page size, max cards per call (default 50).
            offset: how many listings to skip; 0 is the first page.
        """
        project = loc_svc.get_location(project_id)
        if project is None or project.get("level") != "project":
            raise ToolError(f"'{project_id}' is not a known project id.")
        if limit < 1 or offset < 0:
            raise ToolError("limit must be >= 1 and offset >= 0.")
        return svc.list_by_project(project_id=project_id, limit=limit, offset=offset)

    @mcp.tool
    @observe_tool
    def compare_listings(listing_ids: list[str]) -> dict:
        """Compare 2-4 listings side by side with calculated insights (US6).

        Use when the user wants to compare specific units. Pass their ids.
        Returns:
            - `listings`: full details of the compared units.
            - `fields`: key comparison attribute names.
            - `context`: `{same_project, same_province, projects, provinces}` context evaluation.
            - `deltas`: computed price, unit price, and area differences.
            - `highlights`: map of listing_id -> badges (e.g. "cheapest_price", "largest_area").
        Raises:
            ToolError if fewer than 2 or more than 4 distinct listing ids are passed,
            or if any specified listing id does not exist.
        """
        ids = list(dict.fromkeys(listing_ids))  # dedupe, keep order
        if not 2 <= len(ids) <= 4:
            raise ToolError("compare_listings needs between 2 and 4 distinct listing ids.")
        rows = svc.get_many(ids)
        found = {r["id"] for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise ToolError(f"Listing id(s) not found: {', '.join(missing)}.")
        ordered = sorted(rows, key=lambda r: ids.index(r["id"]))
        insights = compute_comparison_insights(ordered)
        return {
            "listings": ordered,
            "fields": [
                "price_vnd",
                "price_per_m2_vnd",
                "area_m2",
                "bedrooms",
                "bedrooms_plus",
                "bathrooms",
                "floor_num",
                "floor_band",
                "property_type",
                "direction_balcony",
                "view",
                "legal_status",
                "furnishing",
                "usage_status",
            ],
            "context": insights["context"],
            "deltas": insights["deltas"],
            "highlights": insights["highlights"],
        }
