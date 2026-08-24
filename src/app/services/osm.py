"""OpenStreetMap (OSM) integration for nearby amenities."""

import json
import urllib.parse
import urllib.request
from typing import Any

from ..observability import mark_current_observation_error, observe_operation

_OSRM_CACHE: dict[str, Any] = {}


@observe_operation("osm.overpass.query", as_type="span")
def get_nearby_amenities(lat: float, lng: float, radius: int = 2000) -> list[dict[str, Any]]:
    """Fetch nearby amenities (schools, hospitals, parks, etc.) from OSM Overpass API.
    
    Args:
        lat: Latitude of the center point.
        lng: Longitude of the center point.
        radius: Search radius in meters (default 2000).
        
    Returns:
        List of amenities shaped as {"id", "name", "type", "lat", "lng"}.
    """
    # Overpass QL query
    query = f"""
    [out:json][timeout:25];
    (
      node["amenity"~"school|hospital|clinic|marketplace|kindergarten"](around:{radius},{lat},{lng});
      way["amenity"~"school|hospital|clinic|marketplace|kindergarten"](around:{radius},{lat},{lng});
      node["leisure"~"park"](around:{radius},{lat},{lng});
      way["leisure"~"park"](around:{radius},{lat},{lng});
    );
    out center;
    """
    
    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({'data': query}).encode('utf-8')
    headers = {
        'User-Agent': 'RealEstateMCP/1.0 (test bot)',
        'Accept': '*/*'
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            
        amenities = []
        for element in result.get('elements', []):
            tags = element.get('tags', {})
            name = tags.get('name')
            if not name:
                continue # Skip unnamed POIs
                
            # Determine type
            if 'amenity' in tags:
                poi_type = tags['amenity']
            elif 'leisure' in tags:
                poi_type = tags['leisure']
            else:
                poi_type = 'unknown'
                
            # Ways return center lat/lon if 'out center;' is used
            element_lat = element.get('lat') or element.get('center', {}).get('lat')
            element_lon = element.get('lon') or element.get('center', {}).get('lon')
            
            if element_lat and element_lon:
                amenities.append({
                    "id": str(element.get('id')),
                    "name": name,
                    "type": poi_type,
                    "lat": element_lat,
                    "lng": element_lon
                })
        
        # Sort by distance loosely (just return top 50 to avoid clutter)
        return amenities[:50]
        
    except Exception as exc:  # noqa: BLE001 - a map without amenities beats no map at all
        # beats no map at all, so every failure degrades to an empty list.
        mark_current_observation_error(exc)
        return []


@observe_operation("osm.osrm.matrix", as_type="span")
def calculate_osrm_matrix(
    origins: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
    profile: str = "driving",  # "driving", "walking", "cycling"
) -> dict[str, Any]:
    """Calculate exact road distance and duration matrix using OpenStreetMap's OSRM routing engine.

    Args:
        origins: List of (lat, lng) tuples representing start points.
        destinations: List of (lat, lng) tuples representing target points.
        profile: "driving" (cars/motorcycles), "walking" (pedestrians), or "cycling" (bikes).

    Returns:
        Structured dict with status, distances_m, durations_s, and structured matrix.
    """
    if not origins or not destinations:
        return {"status": "error", "message": "Missing coordinates", "matrix": []}

    # Format coordinates as "lng,lat" (OSRM standard)
    all_points = list(origins) + list(destinations)
    coords_str = ";".join(f"{round(lng, 6)},{round(lat, 6)}" for lat, lng in all_points)

    src_indices = ";".join(str(i) for i in range(len(origins)))
    dst_indices = ";".join(str(len(origins) + j) for j in range(len(destinations)))

    cache_key = f"{src_indices}_{dst_indices}_{profile}_{coords_str[:50]}"
    if cache_key in _OSRM_CACHE:
        return _OSRM_CACHE[cache_key]

    url = f"http://router.project-osrm.org/table/v1/{profile}/{coords_str}?sources={src_indices}&destinations={dst_indices}&annotations=duration,distance"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RealEstateMCP/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("code") != "Ok":
            mark_current_observation_error(RuntimeError("OSRM returned a non-OK response"))
            return {"status": "error", "message": "OSRM routing failed", "matrix": []}

        distances = data.get("distances", [])
        durations = data.get("durations", [])

        structured_matrix = []
        for i in range(len(origins)):
            row = []
            for j in range(len(destinations)):
                dist_m = distances[i][j] if i < len(distances) and j < len(distances[i]) else 0.0
                dur_s = durations[i][j] if i < len(durations) and j < len(durations[i]) else 0.0

                dur_min = max(1, round(dur_s / 60.0)) if dur_s else 1

                if dist_m < 1000:
                    dist_text = f"{round(dist_m)} m"
                    dist_km = round(dist_m / 1000.0, 2)
                else:
                    dist_km_val = round(dist_m / 1000.0, 1)
                    dist_text = f"{int(dist_km_val) if dist_km_val.is_integer() else dist_km_val} km"
                    dist_km = dist_km_val

                row.append({
                    "distance_m": round(dist_m, 1),
                    "distance_km": dist_km,
                    "distance_text": dist_text,
                    "duration_s": round(dur_s),
                    "duration_min": dur_min,
                    "text": f"{dist_text} • {dur_min} phút",
                })
            structured_matrix.append(row)

        res = {
            "status": "success",
            "source": "OpenStreetMap OSRM",
            "profile": profile,
            "distances_m": distances,
            "durations_s": durations,
            "matrix": structured_matrix,
        }
        _OSRM_CACHE[cache_key] = res
        return res
    except Exception as exc:  # noqa: BLE001 - third-party routing degrades to a safe error
        mark_current_observation_error(exc)
        return {"status": "error", "message": "OSRM request failed", "matrix": []}


@observe_operation("data.amenities.commute", as_type="span")
def fetch_nearby_amenities_with_commute(
    origin_lat: float,
    origin_lng: float,
    profile: str = "driving",
) -> list[dict[str, Any]]:
    """Fetch UC5's OSM amenities and compute road commute distance & travel minutes via OSRM.

    Args:
        origin_lat: Latitude of property.
        origin_lng: Longitude of property.
        profile: "driving" (cars/motorcycles), "walking" (pedestrians), "cycling" (bikes).

    Returns:
        List of amenities from UC5 with real road distance (km/m) and travel minutes.
    """
    raw_amenities = get_nearby_amenities(origin_lat, origin_lng, radius=2000)
    if not raw_amenities:
        return []

    # Map type to standard Vietnamese categories
    category_map = {
        "school": "Trường học",
        "kindergarten": "Trường học",
        "hospital": "Bệnh viện/Phòng khám",
        "clinic": "Bệnh viện/Phòng khám",
        "marketplace": "Chợ / Siêu thị",
        "park": "Công viên & Giải trí",
    }

    shaped_items = []
    dest_coords: list[tuple[float, float]] = []

    for a in raw_amenities:
        cat_name = category_map.get(a.get("type"), "Tiện ích khác")
        shaped_items.append({
            "id": a["id"],
            "name": a["name"],
            "type": a.get("type"),
            "category": cat_name,
            "lat": a["lat"],
            "lng": a["lng"],
        })
        dest_coords.append((float(a["lat"]), float(a["lng"])))

    if not dest_coords:
        return shaped_items

    matrix_res = calculate_osrm_matrix(
        origins=[(origin_lat, origin_lng)],
        destinations=dest_coords,
        profile=profile,
    )
    matrix_rows = matrix_res.get("matrix", [[]])

    if matrix_rows and len(matrix_rows[0]) == len(shaped_items):
        for idx, item in enumerate(shaped_items):
            m_stat = matrix_rows[0][idx]
            item["distance_m"] = m_stat["distance_m"]
            item["distance_km"] = m_stat["distance_km"]
            item["distance_text"] = m_stat.get("distance_text", f"{m_stat['distance_km']} km")
            item["duration_min"] = m_stat["duration_min"]
            item["duration_s"] = m_stat["duration_s"]
            item["travel_summary"] = m_stat["text"]

    return shaped_items
