"""Analytics & map tools (US4 overview, US5 map)."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..observability import observe_tool
from ..services import listings as listing_svc
from ..services import locations as loc_svc
from ..services import osm as osm_svc


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @observe_tool
    def project_overview(project_id: str) -> dict:
        """Market overview for one project (US4): counts + price/area stats + property-type mix.

        Use when the user asks to analyze or summarize a project ("phan tich tong quan").
        Returns {project, stats:{count, price_vnd:{min,max,avg}, price_per_m2_vnd:{...},
        bedrooms_range, by_property_type, by_price_type, coverage}}.

        Descriptive stats only: NOT valuation or investment advice (out of scope per the PRD).
        Top-level price stats mix all listing price kinds, so prefer stats.by_price_type when
        wording a user answer: "asking" means seller asking price, while "estimate" means a
        source-computed reference price. coverage says how many rows each aggregate used, because
        NULL values are skipped. Raises if the project id is unknown.
        """
        project = loc_svc.get_location(project_id)
        if project is None or project.get("level") != "project":
            raise ToolError(f"'{project_id}' is not a known project id.")
        return {"project": project, "stats": listing_svc.project_price_stats(project_id)}

    @mcp.tool
    @observe_tool
    def map_listings(
        project_id: str | None = None,
        property_type: str | None = None,
        min_price_vnd: int | None = None,
        max_price_vnd: int | None = None,
        bedrooms: int | None = None,
        limit: int = 200, 
        include_amenities: bool = False,
        listing_ids: list[str] | None = None,
        min_bedrooms: int | None = None,
        max_bedrooms: int | None = None
    ) -> dict:
        """Geo points for the map view (US5): listings with lat/lng, and optionally surrounding amenities.

        Use for "xem ban do". Optionally scope to one project.
        Set include_amenities=True ONLY when the user explicitly asks for amenities or POIs nearby.
        Returns {"count": n, "points": [{id, title, property_type, price_vnd, lat, lng}], "amenities": [...]}.
        """
        points = listing_svc.map_points(
            project_id=project_id,
            property_type=property_type,
            min_price_vnd=min_price_vnd,
            max_price_vnd=max_price_vnd,
            bedrooms=bedrooms,
            limit=limit,
            listing_ids=listing_ids,
            min_bedrooms=min_bedrooms,
            max_bedrooms=max_bedrooms
        )
        res = {"count": len(points), "points": points}

        if include_amenities and points:
            center_lat = sum(p["lat"] for p in points) / len(points)
            center_lng = sum(p["lng"] for p in points) / len(points)
            res["amenities"] = osm_svc.get_nearby_amenities(center_lat, center_lng, radius=1000)
        elif include_amenities and project_id:
            proj = loc_svc.get_location(project_id)
            if proj and proj.get("lat") and proj.get("lng"):
                res["amenities"] = osm_svc.get_nearby_amenities(proj["lat"], proj["lng"], radius=1000)
            else:
                res["amenities"] = []

        return res
