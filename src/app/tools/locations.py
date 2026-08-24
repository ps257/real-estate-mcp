"""Project / location tools (US1, and the 'clarify project' step across US1/2.1/2.2/3)."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..observability import observe_tool
from ..services import locations as svc


def register(mcp: FastMCP) -> None:
    @mcp.tool
    @observe_tool
    def search_projects(
        query: str | None = None,
        province: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search real-estate PROJECTS by name and/or province.

        Use when the user names or hints at a project ("Vinhomes", "Amber Riverside") or asks for
        projects in a place ("chung cư ở Hà Nội"). This is the entry point for US1 and for the
        "clarify which project" step in booking/consulting/policy flows.

        Returns a list of project nodes sorted by name, each {id, level, name, province, district,
        parent_id, project_id, lat, lng}. On project rows parent_id/project_id are always null,
        and province/lat/lng are null for a few projects. An empty list means no project matched,
        not that the search failed. Show up to 3 as quick-pick buttons; if more, offer
        "xem tất cả".

        Name matching is forgiving: it ignores accents ("chung cu" finds "Chung cư 25 Lạc
        Trung") and tolerates typos ("vinhoms" finds "Vinhomes ..."), and results come back
        best-match-first. `province` is stricter — pass it accented ("Hà Nội", not "ha noi").

        Args:
            query: free-text project name fragment (Vietnamese ok), e.g. "vinhomes". Optional.
            province: province filter, accented, e.g. "Hà Nội". Optional.
            limit: max projects to return (default 10).
        """
        return svc.search_projects(query=query, province=province, limit=limit)

    @mcp.tool
    @observe_tool
    def resolve_project(text: str) -> dict:
        """Decide whether a user's free text refers to a known project, for slot-filling.

        Use to check if what the user typed/clicked is actually a project name before proceeding.
        Returns {"matched": bool, "project": {...}|None, "candidates": [...]}:
        - Resolved -> matched=true, `project` set, `candidates` empty. Proceed with that project.
        - Ambiguous -> matched=false, `candidates` listed. Ask the user to pick one; do not guess.
        - Not a project -> matched=false, `candidates` empty. Treat the text as something else.

        Resolves when the text is one project's full name (ignoring case and accents), or when
        the fuzzy search finds exactly one project. So "Vinhomes Ocean Park" resolves even though
        "Vinhomes Ocean Park 2" and "3" also match, while a bare "Vinhomes" stays ambiguous.
        """
        candidates = svc.search_projects(query=text, province=None, limit=5)
        exact = [c for c in candidates if svc.is_same_name(c["name"], text)]
        # An exact name beats mere prefix matches; without this, typing a project's full name
        # stays "ambiguous" whenever a longer sibling ("... 2") exists.
        if len(exact) == 1:
            return {"matched": True, "project": exact[0], "candidates": []}
        if len(candidates) == 1:
            return {"matched": True, "project": candidates[0], "candidates": []}
        return {"matched": False, "project": None, "candidates": candidates}

    @mcp.tool
    @observe_tool
    def list_project_buildings(
        project_id: str,
        level: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List the buildings and clusters (phân khu) inside a project.

        Use after a project is chosen, to show the user how it is laid out and let them ask
        about a specific tower or subdivision by name.

        Returns location nodes sorted clusters-first then by name, each {id, level, name,
        province, district, parent_id, project_id, lat, lng}. `level` is "cluster" (a
        subdivision) or "building" (a tower); `parent_id` says which cluster a building sits
        under, so you can present them grouped. Big projects mix both layers — Vinhomes Ocean
        Park has 13 clusters and 53 buildings. An empty list means the project is not broken
        down further, which is normal for small projects; it is not an error. Raises if
        `project_id` is not a real project.

        Args:
            project_id: a project id from search_projects / resolve_project,
                e.g. "oh:amber-riverside". Not a cluster or building id.
            level: keep only one layer — "cluster" or "building". Omit for both. Some projects
                have clusters but no building rows, so "building" may return nothing.
            limit: max nodes to return (default 50).
        """
        project = svc.get_location(project_id)
        if project is None or project.get("level") != "project":
            raise ToolError(f"'{project_id}' is not a known project id.")
        if level not in (None, "cluster", "building"):
            raise ToolError(f"level must be 'cluster' or 'building', got '{level}'.")
        return svc.list_project_nodes(project_id=project_id, level=level, limit=limit)

    @mcp.tool
    @observe_tool
    def list_provinces() -> list[str]:
        """List the provinces that have at least one project, to offer the user location choices.

        Use when the user asks where projects are available, or to turn a vague "tìm chung cư"
        into a concrete location question.

        Returns a de-duplicated list of province name strings in Vietnamese alphabetical order,
        e.g. ["Hà Nội", "Hải Phòng", "Hồ Chí Minh", "Hưng Yên", "Long An"]. Pass one back
        verbatim as `province` to search_projects, which expects the accented spelling.

        Some projects have no province recorded, so this list does not cover the whole
        catalogue — search_projects without a province still searches everything. Never tell
        the user these are the only places we have projects.
        """
        return svc.list_provinces()

    @mcp.tool
    @observe_tool
    def calculate_commute_matrix(
        origins: list[dict],
        destinations: list[dict],
        vehicle: str = "motorcycle",
    ) -> dict:
        """Calculate exact travel distance (meters) and duration (minutes/seconds) on OpenStreetMap (OSRM) road network.

        Use when comparing commute times from multiple properties to a workplace, school, or landmark ("đi xe máy từ 4 căn đến trường mất bao lâu?").

        Args:
            origins: List of origin points, each dict containing {"lat": float, "lng": float, "label": str (optional)}.
            destinations: List of destination points, each dict containing {"lat": float, "lng": float, "label": str (optional)}.
            vehicle: Travel profile: "motorcycle" (default), "car", "bike", or "foot".

        Returns:
            Dict containing "status", "vehicle", "distances_m", "durations_s", and structured "matrix" with exact km & minutes for every origin-destination pair.
        """
        from ..services import osm as osm_svc

        origin_coords = [(float(o["lat"]), float(o["lng"])) for o in origins if "lat" in o and "lng" in o]
        dest_coords = [(float(d["lat"]), float(d["lng"])) for d in destinations if "lat" in d and "lng" in d]

        if not origin_coords or not dest_coords:
            raise ToolError("origins and destinations must contain valid lat/lng coordinates.")

        profile_map = {
            "motorcycle": "driving",
            "car": "driving",
            "driving": "driving",
            "foot": "walking",
            "walking": "walking",
            "bike": "cycling",
            "cycling": "cycling",
        }
        profile = profile_map.get(vehicle.lower().strip(), "driving")

        res = osm_svc.calculate_osrm_matrix(origin_coords, dest_coords, profile=profile)
        if res.get("status") == "error":
            raise ToolError(res.get("message", "Failed to calculate matrix"))

        res["vehicle"] = vehicle
        matrix = res.get("matrix", [])
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                if i < len(matrix) and j < len(matrix[i]):
                    matrix[i][j]["origin"] = o.get("label") or f"Origin #{i+1}"
                    matrix[i][j]["destination"] = d.get("label") or f"Destination #{j+1}"

        return res


