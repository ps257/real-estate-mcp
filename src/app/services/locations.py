"""Location hierarchy access.

`locations` is a self-referential tree keyed by text ids:
  level='project'  -> root nodes (57 of them), parent_id is NULL, project_id is NULL
  level='cluster'  -> optional mid layer, parent_id -> project
  level='building' -> leaf, parent_id -> cluster/project, project_id -> its project

Listings reference locations by `project_id` / `building_id` / `location_id` (all text).
"""

from __future__ import annotations

import unicodedata

from ..db import get_client
from ..observability import observe_operation
from ..shaping import shape_location

# Only the columns shape_location actually keeps. `select("*")` would also pull the three jsonb
# columns (sources, source_refs, attrs) on every search just to throw them away.
LOCATION_COLUMNS = "id,level,name,province,district,parent_id,project_id,lat,lng"

# LIKE wildcards and the quote/escape chars used by PostgREST's filter syntax. Vietnamese place
# names never contain these, and PostgREST offers no way to escape them inside an `or=(...)`
# group, so the only safe option is to drop them from user input.
_UNSAFE_IN_FILTER = str.maketrans({"%": None, "_": None, '"': None, "\\": None})


def _sanitize(value: str) -> str:
    """Strip characters that would be read as wildcards or break the filter syntax."""
    return value.translate(_UNSAFE_IN_FILTER).strip()


def _fold_accents(value: str) -> str:
    """Fold Vietnamese accents and case, to match against `locations.name_norm`.

    'Hải Vân' -> 'hai van'. `đ`/`Đ` have no combining-mark decomposition, so map them first.
    """
    value = value.replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def is_same_name(a: str, b: str) -> bool:
    """Compare two place names ignoring case, accents and repeated whitespace.

    Used to tell "the user typed this project's full name" apart from "the user typed something
    that merely matches it", which fuzzy search alone cannot distinguish.
    """
    return " ".join(_fold_accents(a).split()) == " ".join(_fold_accents(b).split())


@observe_operation("db.projects.search", as_type="retriever")
def search_projects(query: str | None, province: str | None, limit: int) -> list[dict]:
    """Search project nodes by name and/or province, best match first.

    Delegates to the `search_projects_fuzzy` Postgres function (see migrations/), which matches
    on three levels and ranks by trigram score:
      1. `name ILIKE %q%`            — accented input, as typed
      2. `name_norm ILIKE %folded%`  — accent-folded input ("chung cu" finds "Chung cư ...")
      3. `word_similarity >= 0.55`   — typos ("vinhoms" finds "Vinhomes ...")

    Filtering happens entirely in SQL, so `limit` never silently drops matches the way a
    post-fetch Python filter would. Requires the `pg_trgm` extension.

    Known gap: `name_norm` also drops generic words ("Khu", "The"), so a query containing one
    matches only via the `name` branch.
    """
    rows = (
        get_client()
        .rpc(
            "search_projects_fuzzy",
            {
                "q": _sanitize(query) if query else "",
                "q_folded": _fold_accents(_sanitize(query)) if query else "",
                "lim": limit,
                "prov": _sanitize(province) if province else None,
            },
        )
        .execute()
        .data
        or []
    )
    return [shape_location(r) for r in rows]


@observe_operation("db.locations.get", as_type="retriever")
def get_location(location_id: str) -> dict | None:
    rows = (
        get_client()
        .table("locations")
        .select(LOCATION_COLUMNS)
        .eq("id", location_id)
        .limit(1)
        .execute()
        .data
    )
    return shape_location(rows[0]) if rows else None


@observe_operation("db.locations.list-project-nodes", as_type="retriever")
def list_project_nodes(project_id: str, level: str | None, limit: int) -> list[dict]:
    """List every cluster/building under a project, clusters first then buildings, by name.

    Matches on `project_id`, not `parent_id`: a project's direct children are clusters whenever
    that project has a cluster layer (23 of 57 do), so walking one level down hides the
    buildings entirely — Vinhomes Ocean Park has 13 direct children but 53 buildings. Every
    cluster and building row carries `project_id`, and it always equals the root of its parent
    chain, so this returns the whole subtree in one query.

    Callers that want a single layer pass `level`. Note some projects have clusters but no
    building rows at all, so `level="building"` can legitimately come back empty.
    """
    q = get_client().table("locations").select(LOCATION_COLUMNS).eq("project_id", project_id)
    if level:
        q = q.eq("level", level)
    # 'cluster' > 'building' alphabetically, so desc puts parents before their children.
    rows = q.order("level", desc=True).order("name").limit(limit).execute().data or []
    return [shape_location(r) for r in rows]


@observe_operation("db.projects.by-province", as_type="retriever")
def project_ids_in_province(province: str, district: str | None = None) -> list[str]:
    """Project ids sitting in a province/district — step one of a province-wide listing search.

    `listings` has no province column, so the only route from a province to its listings runs
    through here: resolve to project ids, then filter `listings.project_id`.
    """
    q = (
        get_client()
        .table("locations")
        .select("id")
        .eq("level", "project")
    )
    if province:
        q = q.ilike("province", f"%{_sanitize(province)}%")
    if district:
        q = q.ilike("district", f"%{_sanitize(district)}%")

    rows = q.limit(1000).execute().data or []
    return [r["id"] for r in rows]


@observe_operation("db.provinces.list", as_type="retriever")
def list_provinces() -> list[str]:
    """Distinct provinces holding at least one project, in Vietnamese alphabetical order.

    Sorting keys on the accent-folded name. Python's plain `sorted()` compares Unicode
    codepoints, which puts 'Hưng Yên' (ư = U+01B0) ahead of 'Hải Phòng' (ả = U+1EA3) and
    'Hồ Chí Minh' (ồ = U+1ED3) — visibly wrong to a Vietnamese reader. Folding collapses ă/â
    into a and đ into d, so this is not a full Vietnamese collation (which sorts them as
    separate letters), but it orders real province names correctly. The unfolded name breaks
    ties so the result stays deterministic.

    9 of the 57 projects have no province recorded; they are simply absent from this list.
    """
    rows = (
        get_client()
        .table("locations")
        .select("province")
        .eq("level", "project")
        .not_.is_("province", "null")
        .limit(1000)  # explicit: PostgREST silently caps at 1000 rows otherwise
        .execute()
        .data
        or []
    )
    provinces = {p for r in rows if (p := (r.get("province") or "").strip())}
    return sorted(provinces, key=lambda p: (_fold_accents(p), p))
