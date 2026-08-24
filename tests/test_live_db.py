"""Live-DB integration tests. AUTO-SKIPPED unless SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set.

Run them once you've put credentials in .env:
    .venv/Scripts/python.exe -m pytest tests/test_live_db.py -v

These call the real tools end-to-end through the MCP server, proving your service/db layer works
against the actual Supabase data. They use the real sample ids from conftest.py.

`mcp.call_tool(name, args)` returns a ToolResult; use `tool_data(res)` from conftest to read the
tool's return value out of it (see that helper for why `.structured_content` needs unwrapping).
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from .conftest import needs_db, tool_data


@needs_db
async def test_search_projects_finds_vinhomes(mcp_server, sample_project_name):
    res = await mcp_server.call_tool("search_projects", {"query": sample_project_name})
    projects = tool_data(res)
    assert isinstance(projects, list) and projects, "expected at least one Vinhomes project"
    assert all(p["level"] == "project" for p in projects)


@needs_db
async def test_search_projects_ignores_accents(mcp_server):
    """US1 acceptance: an unaccented query must find the same projects as the accented one.

    Matching runs against `locations.name_norm`, which is stored accent-folded. Guards the
    checklist example "chung cu" (which returned nothing while we only searched `name`).
    """
    for accented, plain in [("chung cư", "chung cu"), ("Phương Đông", "phuong dong")]:
        with_marks = tool_data(await mcp_server.call_tool("search_projects", {"query": accented}))
        without = tool_data(await mcp_server.call_tool("search_projects", {"query": plain}))
        assert without, f"unaccented query {plain!r} found nothing"
        assert {p["id"] for p in with_marks} == {p["id"] for p in without}


@needs_db
async def test_search_projects_order_is_deterministic(mcp_server):
    """`limit` must cut a stable prefix, not an arbitrary subset.

    Without ORDER BY, Postgres may return any 5 of the 57 projects, and a different 5 next
    time. Don't compare against Python's `sorted()` — Postgres orders by its own locale
    collation, which ranks e.g. "Happy Home" before "HH Linh Đàm"; the property that matters
    to callers is that the order is reproducible.
    """
    first = [p["id"] for p in tool_data(await mcp_server.call_tool("search_projects", {"limit": 5}))]
    wider = [p["id"] for p in tool_data(await mcp_server.call_tool("search_projects", {"limit": 20}))]
    assert len(first) == 5
    assert first == wider[:5], "limit returned a different slice on a wider query"


@needs_db
async def test_search_projects_survives_filter_metacharacters(mcp_server):
    """Characters with meaning in PostgREST's filter syntax must not break the request.

    A comma previously produced a "failed to parse logic tree" error rather than a result set.
    """
    for probe in ["a,b", "%", '"', "(x)"]:
        out = tool_data(await mcp_server.call_tool("search_projects", {"query": probe}))
        assert isinstance(out, list)


@needs_db
async def test_search_projects_tolerates_typos(mcp_server):
    """pg_trgm branch: a misspelt query still reaches the project (US1 "harden" item).

    Requires migrations/001_search_projects_fuzzy.sql. A "Could not find the function
    public.search_projects_fuzzy" error here means the migration has not been applied.
    """
    for typo in ["vinhoms", "vinhomse", "vin homes"]:
        out = tool_data(await mcp_server.call_tool("search_projects", {"query": typo}))
        assert any("Vinhomes" in p["name"] for p in out), f"{typo!r} found nothing"


@needs_db
async def test_search_projects_rejects_unrelated_queries(mcp_server):
    """The other half of the similarity threshold: fuzziness must not become "matches anything".

    Pairs with test_search_projects_tolerates_typos — lowering min_score far enough to pass that
    test while failing this one means the threshold is too loose.
    """
    for nonsense in ["xyzabc", "qqqqqq", "zzzzzzzz"]:
        out = tool_data(await mcp_server.call_tool("search_projects", {"query": nonsense}))
        assert out == [], f"{nonsense!r} matched {[p['name'] for p in out]}"


@needs_db
async def test_search_projects_ranks_best_match_first(mcp_server):
    """Results come back ordered by trigram score, so the exact name outranks its longer siblings."""
    out = tool_data(await mcp_server.call_tool("search_projects", {"query": "Vinhomes Ocean Park"}))
    assert out, "expected at least one Vinhomes Ocean Park project"
    assert out[0]["name"] == "Vinhomes Ocean Park"


@needs_db
async def test_search_projects_filters_province_in_sql(mcp_server):
    """`province` must narrow the query itself, not a page of results.

    If the filter ran in Python after LIMIT, a name+province search would return fewer rows than
    the same search capped at that many — silently dropping matches.
    """
    args = {"query": "Vinhomes", "province": "Hà Nội"}
    out = tool_data(await mcp_server.call_tool("search_projects", {**args, "limit": 100}))
    assert out, "expected Vinhomes projects in Hà Nội"
    assert all(p["province"] == "Hà Nội" for p in out)
    capped = tool_data(await mcp_server.call_tool("search_projects", {**args, "limit": 3}))
    assert len(capped) == min(3, len(out))


@needs_db
async def test_resolve_project(mcp_server, sample_project_name):
    res = await mcp_server.call_tool("resolve_project", {"text": sample_project_name})
    out = tool_data(res)
    assert set(out) == {"matched", "project", "candidates"}


@needs_db
async def test_resolve_project_three_outcomes(mcp_server):
    """The contract from docs/TOOLS_TODO.md: resolved / ambiguous / not-a-project."""
    resolved = tool_data(await mcp_server.call_tool("resolve_project", {"text": "Amber Riverside"}))
    assert resolved["matched"] and resolved["project"]["name"] == "Amber Riverside"
    assert resolved["candidates"] == [], "a resolved match must not also offer candidates"

    ambiguous = tool_data(await mcp_server.call_tool("resolve_project", {"text": "Vinhomes"}))
    assert not ambiguous["matched"] and ambiguous["project"] is None
    assert len(ambiguous["candidates"]) > 1, "ambiguous results must list what to choose between"

    unknown = tool_data(await mcp_server.call_tool("resolve_project", {"text": "khong ton tai xyz"}))
    assert not unknown["matched"] and unknown["candidates"] == []


@needs_db
async def test_resolve_project_prefers_exact_name(mcp_server):
    """A project's own full name resolves even when longer siblings also match.

    "Vinhomes Ocean Park" also matches "... 2" and "... 3"; counting candidates alone would make
    the user disambiguate a name they already typed in full.
    """
    for text in ["Vinhomes Ocean Park", "vinhomes ocean park", "  Vinhomes   Ocean  Park  "]:
        out = tool_data(await mcp_server.call_tool("resolve_project", {"text": text}))
        assert out["matched"], f"{text!r} did not resolve"
        assert out["project"]["name"] == "Vinhomes Ocean Park"


@needs_db
async def test_list_project_buildings_reaches_buildings_behind_clusters(mcp_server):
    """The tool must not stop at the cluster layer.

    Vinhomes Ocean Park's 53 buildings all hang off its 13 clusters, so a `parent_id` query
    returns clusters only and the user can never pick a tower. Matching on `project_id`
    returns the whole subtree.
    """
    out = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 200}
    ))
    levels = {n["level"] for n in out}
    assert "building" in levels, "no buildings returned; the query stopped at the cluster layer"
    assert "cluster" in levels, "clusters are choices too and must still be offered"
    assert all(n["project_id"] == "vhm:vinhomes-ocean-park" for n in out)


@needs_db
async def test_list_project_buildings_orders_clusters_before_buildings(mcp_server):
    """Parents come before their children, so `limit` cuts a usable prefix, not a random slice."""
    out = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 200}
    ))
    levels = [n["level"] for n in out]
    assert levels == sorted(levels, key=lambda x: x != "cluster"), "levels are interleaved"
    # Don't compare names against Python's sorted() — Postgres sorts by its own locale
    # collation. What callers need is that the order repeats, so `limit` is a stable prefix.
    few = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 5}
    ))
    assert [n["id"] for n in few] == [n["id"] for n in out[:5]]


@needs_db
async def test_list_project_buildings_level_filter(mcp_server):
    """`level` narrows to one layer; the two layers together are the unfiltered result."""
    args = {"project_id": "vhm:vinhomes-ocean-park", "limit": 200}
    every = tool_data(await mcp_server.call_tool("list_project_buildings", args))
    clusters = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {**args, "level": "cluster"}
    ))
    buildings = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {**args, "level": "building"}
    ))
    assert all(n["level"] == "cluster" for n in clusters) and clusters
    assert all(n["level"] == "building" for n in buildings) and buildings
    assert len(clusters) + len(buildings) == len(every)


@needs_db
async def test_list_project_buildings_rejects_non_project_ids(mcp_server):
    """A cluster/building id or a typo must raise, not silently return an empty list."""
    child = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 1}
    ))[0]
    for bad in ["khong-ton-tai-xyz", child["id"]]:
        with pytest.raises(ToolError):
            await mcp_server.call_tool("list_project_buildings", {"project_id": bad})

    with pytest.raises(ToolError):
        await mcp_server.call_tool(
            "list_project_buildings",
            {"project_id": "vhm:vinhomes-ocean-park", "level": "tower"},
        )


@needs_db
async def test_list_provinces_is_clean(mcp_server):
    """The checklist contract: non-empty strings, no duplicates, nothing blank or null."""
    out = tool_data(await mcp_server.call_tool("list_provinces", {}))
    assert out, "expected at least one province"
    assert all(isinstance(p, str) and p == p.strip() and p for p in out)
    assert len(out) == len(set(out)), f"duplicates in {out}"


@needs_db
async def test_list_provinces_sorted_for_vietnamese_readers(mcp_server):
    """Order must follow the Vietnamese alphabet, not Unicode codepoints.

    Plain sorted() ranks by codepoint, where ư (U+01B0) < ả (U+1EA3) < ồ (U+1ED3), so it
    returns Hà Nội, Hưng Yên, Hải Phòng, Hồ Chí Minh — 'Hưng Yên' jumps three places up a
    list the user is reading. Sorting on the accent-folded name fixes it.
    """
    out = tool_data(await mcp_server.call_tool("list_provinces", {}))
    for earlier, later in [("Hà Nội", "Hải Phòng"), ("Hải Phòng", "Hưng Yên")]:
        if earlier in out and later in out:
            assert out.index(earlier) < out.index(later), f"{earlier!r} must precede {later!r}"


@needs_db
async def test_list_provinces_each_has_a_project(mcp_server):
    """"...that have at least one project" — every entry must survive the round trip."""
    out = tool_data(await mcp_server.call_tool("list_provinces", {}))
    for province in out:
        hits = tool_data(await mcp_server.call_tool(
            "search_projects", {"province": province, "limit": 1}
        ))
        assert hits, f"{province!r} is listed but search_projects finds no project there"


@needs_db
async def test_search_listings_in_project(mcp_server, sample_project_id):
    res = await mcp_server.call_tool(
        "search_listings", {"project_id": sample_project_id, "limit": 5}
    )
    cards = tool_data(res)
    assert isinstance(cards, list) and cards
    assert len(cards) <= 5
    # types were coerced by shaping (price is int, area is float-or-none)
    assert all(isinstance(c["price_vnd"], (int, type(None))) for c in cards)


@needs_db
async def test_listing_cards_carry_price_type(mcp_server):
    """Every card showing `price_vnd` must also say what kind of price it is.

    1264 of 2355 rows are priced by `estimate` (a figure the source computed) rather than
    `asking`. A card without `price_type` invites the agent to quote an estimate as the
    seller's price — the tool cannot do valuation, so it must not imply one.
    """
    for tool, args, unwrap in [
        ("search_listings", {"limit": 25}, lambda d: d),
        ("list_project_listings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 25},
         lambda d: d["listings"]),
    ]:
        cards = unwrap(tool_data(await mcp_server.call_tool(tool, args)))
        assert cards, f"{tool} returned nothing"
        for card in cards:
            assert "price_type" in card, f"{tool} card is missing price_type"
            assert card["price_type"] in ("asking", "estimate", None)


@needs_db
async def test_search_listings_bedrooms_filter_is_not_truncated(mcp_server):
    """Regression: the bedrooms filter must run in SQL, not over a fetched page.

    Grand Park has 587 listings; sorted by price the first 2-bedroom is at index 144. The old
    code fetched `limit * 3` rows and filtered in Python, so bedrooms=2 with limit=10 looked at
    the cheapest 30, matched none, and answered "no listings" over 251 real matches.
    """
    for beds in (1, 2, 3):
        out = tool_data(await mcp_server.call_tool(
            "search_listings",
            {"project_id": "vhm:vinhomes-grand-park", "bedrooms": beds, "limit": 10},
        ))
        assert len(out) == 10, f"bedrooms={beds} returned {len(out)}/10"
        assert all(c["bedrooms"] == beds for c in out)


@needs_db
async def test_bedrooms_agree_with_the_listing_title(mcp_server):
    """`bedrooms` is derived from the title by the listings_clean view (migrations/002).

    The raw column called 139 studios "1 bedroom". If this fails with "Could not find the
    table public.listings_clean", the migration has not been applied.
    """
    studios = tool_data(await mcp_server.call_tool(
        "search_listings", {"bedrooms": 0, "limit": 100}
    ))
    assert studios, "expected studio listings"
    assert all("studio" in c["title"].lower() for c in studios), (
        "a unit counted as 0 bedrooms must be advertised as a studio"
    )

    ones = tool_data(await mcp_server.call_tool(
        "search_listings", {"bedrooms": 1, "limit": 100}
    ))
    assert not any("studio" in c["title"].lower() for c in ones), (
        "studios must no longer leak into the 1-bedroom bucket"
    )


@needs_db
async def test_bedroom_filter_excludes_non_residential(mcp_server):
    """Shophouses carried a placeholder bedrooms=1; the view leaves them null instead.

    Without this, "căn 1 phòng ngủ" returned commercial units.
    """
    for pt in ("shophouse", "thuong_mai_dich_vu"):
        with_beds = tool_data(await mcp_server.call_tool(
            "search_listings", {"property_type": pt, "min_bedrooms": 0, "limit": 50}
        ))
        assert with_beds == [], f"{pt} should have no bedroom count at all, got {len(with_beds)}"
        # …but the units themselves are still findable without a bedroom filter.
        plain = tool_data(await mcp_server.call_tool(
            "search_listings", {"property_type": pt, "limit": 5}
        ))
        assert plain, f"{pt} listings disappeared entirely"
        assert all(c["bedrooms"] is None for c in plain)


@needs_db
async def test_has_flex_room_marks_the_plus_one(mcp_server):
    """"2 PN + 1" is 2 bedrooms plus a multi-purpose room, not 3 bedrooms."""
    cards = tool_data(await mcp_server.call_tool(
        "search_listings", {"bedrooms": 2, "limit": 100}
    ))
    assert cards
    for card in cards:
        titled_plus = "+ 1" in card["title"] or "+1" in card["title"] or "2PN+" in card["title"] or "1PN+" in card["title"] or "3PN+" in card["title"]
        assert card["has_flex_room"] == titled_plus, f"mismatch on {card['title']!r}"


@needs_db
async def test_search_listings_area_range_runs_in_sql(mcp_server):
    """`area_m2` bounds must narrow the query, not a page of results.

    Same failure mode the bedrooms filter had: a post-fetch filter would cap the cheapest
    `limit` rows first and then discard most of them, under-returning without any error.
    """
    lo, hi = 50.0, 70.0
    out = tool_data(await mcp_server.call_tool(
        "search_listings", {"min_area_m2": lo, "max_area_m2": hi, "limit": 50}
    ))
    assert len(out) == 50, "a 50-70 m2 window holds far more than 50 listings"
    assert all(lo <= c["area_m2"] <= hi for c in out)

    # Narrowing the window must not return rows the wider one already excluded.
    narrow = tool_data(await mcp_server.call_tool(
        "search_listings", {"min_area_m2": 60.0, "max_area_m2": hi, "limit": 50}
    ))
    assert all(60.0 <= c["area_m2"] <= hi for c in narrow)


@needs_db
async def test_search_listings_bedroom_range(mcp_server):
    """min/max bedrooms express "từ N phòng trở lên", which exact match cannot."""
    two_plus = tool_data(await mcp_server.call_tool(
        "search_listings", {"min_bedrooms": 2, "limit": 50}
    ))
    assert two_plus and all(c["bedrooms"] >= 2 for c in two_plus)

    exactly_two = tool_data(await mcp_server.call_tool(
        "search_listings", {"bedrooms": 2, "limit": 50}
    ))
    assert all(c["bedrooms"] == 2 for c in exactly_two)

    window = tool_data(await mcp_server.call_tool(
        "search_listings", {"min_bedrooms": 2, "max_bedrooms": 3, "limit": 50}
    ))
    assert all(2 <= c["bedrooms"] <= 3 for c in window)


@needs_db
async def test_search_listings_rejects_inverted_ranges(mcp_server):
    """An impossible window must say so, not answer "no listings match"."""
    for bad in [
        {"min_price_vnd": 5_000_000_000, "max_price_vnd": 1_000_000_000},
        {"min_bedrooms": 3, "max_bedrooms": 1},
        {"min_area_m2": 90.0, "max_area_m2": 40.0},
    ]:
        with pytest.raises(ToolError):
            await mcp_server.call_tool("search_listings", bad)


@needs_db
async def test_search_listings_price_bounds_run_in_sql(mcp_server):
    """Price bounds must narrow the query itself, so `limit` caps matches and not the page."""
    lo, hi = 2_000_000_000, 4_000_000_000
    out = tool_data(await mcp_server.call_tool(
        "search_listings",
        {"project_id": "vhm:vinhomes-grand-park", "min_price_vnd": lo,
         "max_price_vnd": hi, "limit": 50},
    ))
    assert out, "expected listings in the 2-4 tỷ band"
    assert all(lo <= c["price_vnd"] <= hi for c in out)
    prices = [c["price_vnd"] for c in out]
    assert prices == sorted(prices), "results must come back cheapest first"


@needs_db
async def test_search_listings_by_building(mcp_server):
    """`building_id` closes the loop with list_project_buildings (US1 narrow-to-a-tower)."""
    buildings = tool_data(await mcp_server.call_tool(
        "list_project_buildings",
        {"project_id": "vhm:vinhomes-grand-park", "level": "building", "limit": 200},
    ))
    assert buildings, "fixture project should have buildings"
    for b in buildings:
        out = tool_data(await mcp_server.call_tool(
            "search_listings", {"building_id": b["id"], "limit": 5}
        ))
        if out:  # not every tower has listings on file
            assert all(c["building_id"] == b["id"] for c in out)
            return
    raise AssertionError("no building in the project had any listing")


@needs_db
async def test_search_listings_rejects_unknown_property_type(mcp_server):
    """A bad property_type raises and names the valid values, instead of returning nothing."""
    from app.tools.listings import PROPERTY_TYPES

    with pytest.raises(ToolError) as err:
        await mcp_server.call_tool("search_listings", {"property_type": "chung_cu"})
    message = str(err.value)
    assert all(valid in message for valid in PROPERTY_TYPES), "error must list every valid value"

    # Every advertised type must actually be accepted, or the error message lies.
    for valid in PROPERTY_TYPES:
        out = tool_data(await mcp_server.call_tool(
            "search_listings", {"property_type": valid, "limit": 1}
        ))
        assert isinstance(out, list)


@needs_db
async def test_search_by_province_covers_every_project_there(mcp_server):
    """The two-step resolve must not lose projects on the way from province to listings."""
    out = tool_data(await mcp_server.call_tool(
        "search_listings_by_province", {"province": "Hà Nội", "limit": 200}
    ))
    assert out, "expected listings in Hà Nội"

    in_province = {
        p["id"] for p in tool_data(await mcp_server.call_tool(
            "search_projects", {"province": "Hà Nội", "limit": 100}
        ))
    }
    assert {c["project_id"] for c in out} <= in_province, (
        "a card came back from a project that is not in the province"
    )
    prices = [c["price_vnd"] for c in out]
    assert prices == sorted(prices), "results must be cheapest-first across projects"


@needs_db
async def test_search_by_province_spans_more_than_one_project(mcp_server):
    """The point of the tool: it is not just search_listings with extra steps.

    A province search has to reach every project there, not the first one it finds.
    """
    out = tool_data(await mcp_server.call_tool(
        "search_listings_by_province", {"province": "Hà Nội", "limit": 200}
    ))
    assert len({c["project_id"] for c in out}) > 1


@needs_db
async def test_search_by_province_applies_the_other_filters(mcp_server):
    """Filters must still run in SQL once the project list is resolved."""
    out = tool_data(await mcp_server.call_tool(
        "search_listings_by_province",
        {"province": "Hà Nội", "bedrooms": 2, "max_price_vnd": 4_000_000_000, "limit": 100},
    ))
    assert out
    assert all(c["bedrooms"] == 2 for c in out)
    assert all(c["price_vnd"] <= 4_000_000_000 for c in out)


@needs_db
async def test_search_by_province_rejects_unknown_province(mcp_server):
    """An unknown province must raise, never fall through to every listing we have.

    `.in_("project_id", [])` is not a valid PostgREST filter; if the empty list reached the
    query the request would come back unfiltered.
    """
    with pytest.raises(ToolError) as err:
        await mcp_server.call_tool(
            "search_listings_by_province", {"province": "Đà Nẵng"}
        )
    assert "Hà Nội" in str(err.value), "the error should name the provinces we do cover"


@needs_db
async def test_search_by_province_matches_case_insensitively(mcp_server):
    """list_provinces returns accented names; typing them in lower case must still work."""
    exact = tool_data(await mcp_server.call_tool(
        "search_listings_by_province", {"province": "Hà Nội", "limit": 20}
    ))
    lowered = tool_data(await mcp_server.call_tool(
        "search_listings_by_province", {"province": "hà nội", "limit": 20}
    ))
    assert [c["id"] for c in exact] == [c["id"] for c in lowered]


@needs_db
async def test_get_listing_detail(mcp_server, sample_listing_ids):
    res = await mcp_server.call_tool("get_listing", {"listing_id": sample_listing_ids[0]})
    listing = tool_data(res)
    assert listing["id"] == sample_listing_ids[0]
    assert "images" in listing  # detail view


@needs_db
async def test_get_listing_returns_every_documented_field(mcp_server, sample_listing_ids):
    """The docstring promises a fixed key set; a missing value must be null, not an absent key.

    shape_listing_detail builds the dict from `row.get(...)`, so a column dropped from
    LISTING_DETAIL_COLUMNS would silently shrink the payload instead of failing.
    """
    detail = tool_data(await mcp_server.call_tool(
        "get_listing", {"listing_id": sample_listing_ids[0]}
    ))
    expected = {
        "id", "title", "url", "source", "project_id", "building_id", "property_type",
        "area_m2", "bedrooms", "bedrooms_plus", "has_flex_room", "bathrooms", "price_vnd", "price_per_m2_vnd",
        "status",
        "lat", "lng", "thumbnail", "floor_num", "floor_band", "direction_balcony", "view",
        "legal_status", "furnishing", "usage_status", "price_type", "area_type",
        "image_count", "images", "first_seen", "last_seen", "crawled_at",
        "project_name", "province", "district", "address",
    }
    assert set(detail) == expected, f"payload drifted: {set(detail) ^ expected}"
    assert isinstance(detail["images"], list), "images must be a list even when empty"


@needs_db
async def test_get_listing_raises_on_unknown_id(mcp_server):
    """"raise nếu không tìm thấy" — and the message must not leak internals."""
    for bad in ["khong-ton-tai-xyz", ""]:
        with pytest.raises(ToolError) as err:
            await mcp_server.call_tool("get_listing", {"listing_id": bad})
        message = str(err.value)
        assert "not found" in message.lower() or "no listing" in message.lower()
        assert "supabase" not in message.lower() and "http" not in message.lower()


@needs_db
async def test_get_listing_matches_the_card_it_came_from(mcp_server):
    """A detail view must agree with the search card, or the UI contradicts itself."""
    cards = tool_data(await mcp_server.call_tool(
        "search_listings", {"project_id": "vhm:vinhomes-grand-park", "limit": 3}
    ))
    assert cards
    for card in cards:
        detail = tool_data(await mcp_server.call_tool(
            "get_listing", {"listing_id": card["id"]}
        ))
        for field in ("id", "title", "price_vnd", "area_m2", "bedrooms", "property_type"):
            assert detail[field] == card[field], f"{field} differs between card and detail"


@needs_db
async def test_get_listing_image_count_is_not_the_gallery_length(mcp_server):
    """Guards the docstring claim that `images` is capped while `image_count` is the source count.

    If a future load fixed the cap, image_count would equal len(images) everywhere and the
    warning we give the agent would be stale advice worth deleting.
    """
    cards = tool_data(await mcp_server.call_tool("search_listings", {"limit": 50}))
    details = [
        tool_data(await mcp_server.call_tool("get_listing", {"listing_id": c["id"]}))
        for c in cards[:15]
    ]
    assert all(len(d["images"]) <= 40 for d in details), "images cap is no longer 40"
    assert all(d["image_count"] >= len(d["images"]) for d in details), (
        "image_count should never undercount the gallery we hold"
    )


@needs_db
async def test_compare_listings(mcp_server, sample_listing_ids):
    res = await mcp_server.call_tool(
        "compare_listings", {"listing_ids": sample_listing_ids[:2]}
    )
    out = tool_data(res)
    assert len(out["listings"]) == 2
    assert "fields" in out
    assert "context" in out
    assert "deltas" in out
    assert "highlights" in out
    assert isinstance(out["context"]["same_project"], bool)
    assert isinstance(out["deltas"]["price_vnd"], dict)



@needs_db
async def test_list_project_listings_reports_the_real_total(mcp_server):
    """"Xem tất cả" must not present one page as the whole project.

    Vinhomes Ocean Park has 685 listings; the default page is 50. Returning a bare list let the
    agent say "here are all the listings" over 7% of them.
    """
    out = tool_data(await mcp_server.call_tool(
        "list_project_listings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 50}
    ))
    assert set(out) == {"total", "offset", "count", "has_more", "listings"}
    assert out["total"] > out["count"], "fixture project should exceed one page"
    assert out["count"] == len(out["listings"]) == 50
    assert out["has_more"] is True


@needs_db
async def test_list_project_listings_pages_without_gaps_or_repeats(mcp_server):
    """Consecutive pages must tile the result set: no listing skipped, none served twice."""
    args = {"project_id": "vhm:vinhomes-ocean-park", "limit": 20}
    first = tool_data(await mcp_server.call_tool("list_project_listings", args))
    second = tool_data(await mcp_server.call_tool(
        "list_project_listings", {**args, "offset": first["count"]}
    ))
    ids_a = [c["id"] for c in first["listings"]]
    ids_b = [c["id"] for c in second["listings"]]
    assert not set(ids_a) & set(ids_b), "pages overlap"
    assert second["offset"] == 20 and second["total"] == first["total"]

    wide = tool_data(await mcp_server.call_tool("list_project_listings", {**args, "limit": 40}))
    assert [c["id"] for c in wide["listings"]] == ids_a + ids_b, "pages do not tile the order"

    # Past the end is an empty page, not an error.
    tail = tool_data(await mcp_server.call_tool(
        "list_project_listings", {**args, "offset": first["total"] + 10}
    ))
    assert tail["listings"] == [] and tail["has_more"] is False


@needs_db
async def test_list_project_listings_rejects_bad_input(mcp_server):
    for bad in [{"project_id": "khong-ton-tai-xyz"},
                {"project_id": "vhm:vinhomes-ocean-park", "limit": 0},
                {"project_id": "vhm:vinhomes-ocean-park", "offset": -1}]:
        with pytest.raises(ToolError):
            await mcp_server.call_tool("list_project_listings", bad)


@needs_db
async def test_listing_cta_actions_are_executable(mcp_server):
    """Each CTA must name a tool that exists and carry the args that tool needs.

    `next_tool` is a string the agent will act on; nothing else checks it, so a renamed tool
    would send the agent to call something that is not there.
    """
    registered = {t.name for t in await mcp_server.list_tools()}
    card = tool_data(await mcp_server.call_tool(
        "search_listings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 1}
    ))[0]
    out = tool_data(await mcp_server.call_tool(
        "listing_cta_actions", {"listing_id": card["id"]}
    ))

    assert out["listing_id"] == card["id"]
    assert out["project_id"] == card["project_id"]
    assert [c["action"] for c in out["ctas"]] == [
        "view_all", "book_visit", "consult", "view_map"
    ]
    for cta in out["ctas"]:
        assert cta["next_tool"] in registered, f"{cta['next_tool']} is not a registered tool"
        assert cta["args"]["project_id"] == card["project_id"]
        # The prefilled args must actually satisfy the tool they point at.
        await mcp_server.call_tool(cta["next_tool"], cta["args"])


@needs_db
async def test_listing_cta_actions_rejects_unknown_listing(mcp_server):
    """A CTA block for a listing that does not exist would offer buttons that go nowhere."""
    with pytest.raises(ToolError):
        await mcp_server.call_tool("listing_cta_actions", {"listing_id": "khong-ton-tai-xyz"})


@needs_db
async def test_project_overview_stats(mcp_server, sample_project_id):
    res = await mcp_server.call_tool("project_overview", {"project_id": sample_project_id})
    out = tool_data(res)
    assert out["project"]["id"] == sample_project_id
    assert out["stats"]["count"] > 0
    assert "price_vnd" in out["stats"]
    assert "by_price_type" in out["stats"]
    assert "coverage" in out["stats"]
    assert out["stats"]["coverage"]["total"] == out["stats"]["count"]
    assert out["stats"]["coverage"]["price_vnd_count"] <= out["stats"]["count"]
    assert set(out["stats"]["by_price_type"]).issubset({"asking", "estimate", "unknown"})


@needs_db
async def test_map_listings_have_coords(mcp_server, sample_project_id):
    res = await mcp_server.call_tool(
        "map_listings", {"project_id": sample_project_id, "limit": 10}
    )
    out = tool_data(res)
    assert out["count"] == len(out["points"])
    assert all(p["lat"] is not None and p["lng"] is not None for p in out["points"])


@needs_db
async def test_booking_form_authed_vs_guest(mcp_server, sample_project_id):
    guest = tool_data(await mcp_server.call_tool(
        "start_visit_booking", {"project_id": sample_project_id, "is_authenticated": False}
    ))
    authed = tool_data(await mcp_server.call_tool(
        "start_visit_booking", {"project_id": sample_project_id, "is_authenticated": True}
    ))
    guest_fields = {f["name"] for f in guest["fields"]}
    authed_fields = {f["name"] for f in authed["fields"]}
    assert "phone" in guest_fields  # guest must give contact
    assert "phone" not in authed_fields  # authed is prefilled


@needs_db
@pytest.mark.parametrize(
    ("tool", "action"),
    [("start_visit_booking", "visit_booking"), ("start_consultation", "consultation")],
)
async def test_form_spec_matches_the_user_story(mcp_server, sample_project_id, tool, action):
    """US2.1/US2.2: guest asks tên/điện thoại/email/thời gian/ghi chú, signed-in only time/note."""
    guest = tool_data(await mcp_server.call_tool(
        tool, {"project_id": sample_project_id, "is_authenticated": False}
    ))
    authed = tool_data(await mcp_server.call_tool(
        tool, {"project_id": sample_project_id, "is_authenticated": True}
    ))

    assert set(guest) == {
        "action", "project", "authenticated", "fields",
        "submit_tool", "submit_endpoint", "persisted",
    }
    assert guest["submit_tool"] == "submit_booking", "the form must name the tool that stores it"
    assert guest["action"] == action
    assert guest["submit_endpoint"].endswith(action)
    assert guest["project"]["id"] == sample_project_id and guest["project"]["name"]
    assert guest["authenticated"] is False and authed["authenticated"] is True

    assert [f["name"] for f in guest["fields"]] == [
        "full_name", "phone", "email", "preferred_time", "note"
    ]
    assert [f["name"] for f in authed["fields"]] == ["preferred_time", "note"]

    required = {f["name"] for f in guest["fields"] if f["required"]}
    assert required == {"full_name", "phone", "preferred_time"}, (
        "a booking with no name, phone or time cannot be acted on"
    )
    assert all({"name", "label", "type", "required"} <= set(f) for f in guest["fields"])


@needs_db
@pytest.mark.parametrize("tool", ["start_visit_booking", "start_consultation"])
async def test_form_says_nothing_was_saved(mcp_server, sample_project_id, tool):
    """These tools return a form spec only; no `bookings` table exists yet.

    If this ever flips to true, the docstrings telling the agent never to confirm a booking
    have to be rewritten in the same change.
    """
    out = tool_data(await mcp_server.call_tool(
        tool, {"project_id": sample_project_id}
    ))
    assert out["persisted"] is False


@needs_db
@pytest.mark.parametrize("tool", ["start_visit_booking", "start_consultation"])
async def test_form_rejects_non_project_ids(mcp_server, tool):
    """A form must hang off a real project — a cluster or building id is not one."""
    children = tool_data(await mcp_server.call_tool(
        "list_project_buildings", {"project_id": "vhm:vinhomes-ocean-park", "limit": 200}
    ))
    a_cluster = next(c["id"] for c in children if c["level"] == "cluster")
    a_building = next(c["id"] for c in children if c["level"] == "building")
    for bad in ["khong-ton-tai-xyz", "", a_cluster, a_building]:
        with pytest.raises(ToolError):
            await mcp_server.call_tool(tool, {"project_id": bad})


@pytest.fixture
def booking_cleanup():
    """Delete whatever a test wrote to `bookings`, even if the test fails.

    These are the only tests in the suite that write, and they write to the one table holding
    personal data. Leaving rows behind would also poison the dedupe window for the next run.
    """
    import sys

    sys.path.insert(0, "src")
    from app.db import get_client

    phones: list[str] = []
    yield phones
    if phones:
        get_client().table("bookings").delete().in_("contact->>phone", phones).execute()


@needs_db
async def test_submit_booking_stores_and_returns_an_id(mcp_server, sample_project_id, booking_cleanup):
    """The write path end to end: a filled form comes back with an id worth confirming."""
    phone = "0900000001"
    booking_cleanup.append(phone)
    out = tool_data(await mcp_server.call_tool("submit_booking", {
        "kind": "visit_booking",
        "project_id": sample_project_id,
        "payload": {
            "full_name": "Nguyễn Văn Test",
            "phone": phone,
            "preferred_time": "2026-09-01T14:00:00+07:00",
            "note": "test",
        },
    }))
    assert set(out) == {
        "booking_id", "kind", "project", "preferred_time", "created_at",
        "persisted", "duplicate_of_existing",
    }
    assert out["persisted"] is True, "the whole point is that this one does write"
    assert out["booking_id"] and out["project"]["id"] == sample_project_id
    assert out["duplicate_of_existing"] is False


@needs_db
async def test_submit_booking_is_idempotent_within_the_window(mcp_server, sample_project_id, booking_cleanup):
    """A retry must not book the same person twice.

    Agents retry on timeouts and users double-click. Two rows means the sales team calls the
    same person about the same unit twice and the user cannot cancel the extra one.
    """
    phone = "0900000002"
    booking_cleanup.append(phone)
    args = {
        "kind": "visit_booking",
        "project_id": sample_project_id,
        "payload": {"full_name": "Trần Thị Test", "phone": phone,
                    "preferred_time": "2026-09-02T09:00:00+07:00"},
    }
    first = tool_data(await mcp_server.call_tool("submit_booking", args))
    second = tool_data(await mcp_server.call_tool("submit_booking", args))
    assert second["booking_id"] == first["booking_id"]
    assert second["duplicate_of_existing"] is True


@needs_db
async def test_submit_booking_validates_against_the_form_it_handed_out(
    mcp_server, sample_project_id
):
    """Whatever start_visit_booking marked `required` is exactly what submit_booking demands."""
    form = tool_data(await mcp_server.call_tool(
        "start_visit_booking", {"project_id": sample_project_id, "is_authenticated": False}
    ))
    required = [f["name"] for f in form["fields"] if f["required"]]
    assert required, "the guest form should require something"

    full = {"full_name": "A", "phone": "0900000003", "preferred_time": "2026-09-03T09:00:00+07:00"}
    for name in required:
        with pytest.raises(ToolError) as err:
            await mcp_server.call_tool("submit_booking", {
                "kind": "visit_booking",
                "project_id": sample_project_id,
                "payload": {k: v for k, v in full.items() if k != name},
            })
        assert name in str(err.value)


@needs_db
async def test_submit_booking_rejects_bad_input_without_storing(mcp_server, sample_project_id):
    """Every rejection path. None of these may leave a row behind."""
    good = {"full_name": "A", "phone": "0900000004", "preferred_time": "2026-09-04T09:00:00+07:00"}
    cases = [
        ({"kind": "khong_ton_tai"}, "unknown kind"),
        ({"project_id": "khong-ton-tai-xyz"}, "unknown project"),
        ({"payload": {**good, "budget": "3 tỷ"}}, "field the form never offered"),
        ({"payload": {**good, "phone": "abc"}}, "not a phone number"),
        ({"payload": {**good, "email": "nope"}}, "not an email"),
        ({"payload": {**good, "preferred_time": "chiều mai"}}, "not ISO-8601"),
    ]
    for override, why in cases:
        args = {"kind": "visit_booking", "project_id": sample_project_id, "payload": good}
        with pytest.raises(ToolError):
            await mcp_server.call_tool("submit_booking", {**args, **override})

    import sys

    sys.path.insert(0, "src")
    from app.db import get_client

    left = (
        get_client().table("bookings").select("id", count="exact")
        .eq("contact->>phone", good["phone"]).limit(1).execute().count
    )
    assert left == 0, f"a rejected booking was stored anyway ({why})"


@needs_db
async def test_submit_booking_authenticated_needs_no_contact(mcp_server, sample_project_id):
    """Signed-in users send only time and note; contact comes from their profile."""
    with pytest.raises(ToolError) as err:
        await mcp_server.call_tool("submit_booking", {
            "kind": "consultation",
            "project_id": sample_project_id,
            "is_authenticated": True,
            "payload": {"phone": "0900000005", "preferred_time": "2026-09-05T09:00:00+07:00"},
        })
    assert "phone" in str(err.value), "the authed form never asked for a phone"


@needs_db
async def test_form_fields_are_not_shared_between_calls(mcp_server, sample_project_id):
    """The field list is built from a module-level template; callers must get their own copy."""
    from app.tools import cta

    first = cta._form_payload("visit_booking", sample_project_id, is_authenticated=False)
    first["fields"][0]["label"] = "MUTATED"
    second = cta._form_payload("visit_booking", sample_project_id, is_authenticated=False)
    assert second["fields"][0]["label"] == "Họ và tên"
