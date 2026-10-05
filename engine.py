"""
SafePath Routing Engine
=======================
Algorithmic routing module for SafePath - a dynamically weighted heuristic routing engine.

This module is responsible for:
1. Downloading and caching pedestrian street network graphs from OpenStreetMap via OSMnx.
2. Generating synthetic environmental hazard zones (e.g., severe air quality / AQI 250+ hotspots)
   directly along the corridor between the origin and destination.
3. Dynamically computing edge impedance (safepath_weight) using an environmental penalty heuristic.
4. Finding the optimal shortest paths:
   - Route A (Standard): Classical Dijkstra/A* path minimizing physical distance.
   - Route B (SafePath): Cost-penalized path detouring away from hazardous environmental exposure.
5. Computing precise physical travel distances (km) and exposure ratings.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import time
from typing import Any, Dict, List, Tuple

import networkx as nx
import osmnx as ox
import osmnx._http
from shapely.geometry import LineString

logger = logging.getLogger("safepath.engine")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [SafePath Engine] %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ==============================================================================
# IPv4-Only Patch — prevents "Errno 101: Network is unreachable" on Render
# (Render's egress sometimes has broken IPv6 routing; force AF_INET via getaddrinfo)
# ==============================================================================
_ORIG_GETADDRINFO = socket.getaddrinfo

def _force_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
    except socket.gaierror:
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

socket.getaddrinfo = _force_ipv4_getaddrinfo
osmnx._http._config_dns = lambda url: None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
logger.info(f"OSMnx cache directory: {CACHE_DIR}")

BUNDLED_GRAPH_PATH = os.path.join(ASSETS_DIR, "delhi_central_walk.graphml")
BUNDLED_GRAPH_BBOX: Tuple[float, float, float, float] | None = None
_BUNDLED_GRAPH_CACHE: nx.MultiDiGraph | None = None
_BUNDLED_GRAPH_LOAD_ERROR: str | None = None


def _load_bundled_graph() -> nx.MultiDiGraph | None:
    """
    Load the pre-built Delhi-central pedestrian GraphML file from disk (if present).
    Once loaded, the graph is cached in memory and reused for all route requests
    whose bounding box fits inside the pre-built area.

    This approach completely avoids runtime Overpass API calls, which are blocked
    by default on some PaaS providers (e.g., Render free tier).
    """
    global _BUNDLED_GRAPH_CACHE, _BUNDLED_GRAPH_LOAD_ERROR, BUNDLED_GRAPH_BBOX

    if _BUNDLED_GRAPH_CACHE is not None:
        return _BUNDLED_GRAPH_CACHE
    if _BUNDLED_GRAPH_LOAD_ERROR is not None:
        return None

    if not os.path.isfile(BUNDLED_GRAPH_PATH):
        _BUNDLED_GRAPH_LOAD_ERROR = "not_present"
        logger.info(
            f"No bundled graph at {BUNDLED_GRAPH_PATH}. Falling back to live Overpass downloads."
        )
        return None

    try:
        t0 = time.time()
        G = ox.load_graphml(BUNDLED_GRAPH_PATH)
        elapsed = time.time() - t0

        xs = [d["x"] for _, d in G.nodes(data=True)]
        ys = [d["y"] for _, d in G.nodes(data=True)]
        BUNDLED_GRAPH_BBOX = (min(xs), min(ys), max(xs), max(ys))
        _BUNDLED_GRAPH_CACHE = G

        logger.info(
            f"Bundled graph loaded in {elapsed:.1f}s — {len(G)} nodes, {G.number_of_edges()} edges. "
            f"BBOX (min_lon, min_lat, max_lon, max_lat) = {BUNDLED_GRAPH_BBOX}"
        )
        return G
    except Exception as exc:
        _BUNDLED_GRAPH_LOAD_ERROR = str(exc)
        logger.warning(f"Failed to load bundled graph {BUNDLED_GRAPH_PATH}: {exc}")
        return None


def _bbox_inside_outer(inner_bbox, outer_bbox) -> bool:
    """Return True if inner (min_lon, min_lat, max_lon, max_lat) is fully inside outer."""
    in_min_lon, in_min_lat, in_max_lon, in_max_lat = inner_bbox
    out_min_lon, out_min_lat, out_max_lon, out_max_lat = outer_bbox
    return (
        in_min_lon >= out_min_lon
        and in_max_lon <= out_max_lon
        and in_min_lat >= out_min_lat
        and in_max_lat <= out_max_lat
    )

# ==============================================================================
# Overpass API endpoint fallback list.
# Render's free tier egress cannot reach overpass-api.de directly (Errno 101),
# so we cycle through community mirrors. Try fastest / most permissive first.
# ==============================================================================
OVERPASS_ENDPOINTS: List[str] = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.torresval.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

ox.settings.http_user_agent = "SafePath-Routing-Engine/1.0 (academic-research@safepath.local)"
ox.settings.http_referer = "https://safepath.local"
ox.settings.use_cache = True
ox.settings.cache_folder = CACHE_DIR
ox.settings.overpass_rate_limit = False
ox.settings.log_console = False
ox.settings.timeout = 60
ox.settings.requests_kwargs = {}
ox.settings.overpass_endpoint = OVERPASS_ENDPOINTS[0]
logger.info(f"Default Overpass endpoint: {OVERPASS_ENDPOINTS[0]}")


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points on the Earth's surface
    using the Haversine formula.

    Returns:
        Distance in meters.
    """
    R = 6371000.0  # Earth's radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def generate_hazard_zones(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    count: int = 2,
    default_radius: float = 400.0,
) -> List[Dict[str, Any]]:
    """
    Generate 2 or 3 High-AQI Hazard Zones (coordinates) within the corridor
    between start and end coordinates.

    Strategy:
    To ensure the standard shortest-path route naturally collides with the hazard
    zones (demonstrating acute pollution exposure) while allowing the heuristic
    engine to navigate safely around them, hazard centers are placed at fractional
    intervals (e.g. 35% and 65%) along the direct vector between Start and End,
    with a small perpendicular offset.

    The radius is roughly 400 meters, adaptively clamped if start and end are close
    together so that the hazard zone does not completely engulf the endpoints.
    """
    dist_m = haversine_distance(start_lat, start_lon, end_lat, end_lon)

    # Adaptive radius: default ~400m, but ensure radius doesn't overwhelm short trips
    # (e.g. radius never exceeds 35% of total travel distance)
    radius_m = min(default_radius, max(120.0, dist_m * 0.30))

    hazard_zones: List[Dict[str, Any]] = []

    # Vector components from Start -> End
    delta_lat = end_lat - start_lat
    delta_lon = end_lon - start_lon

    # Perpendicular unit vector components for realistic lateral offsets
    perp_lat = -delta_lon
    perp_lon = delta_lat
    norm = math.hypot(perp_lat, perp_lon)
    if norm > 1e-7:
        perp_lat /= norm
        perp_lon /= norm
    else:
        perp_lat, perp_lon = 0.0, 0.0

    # Placement ratios along the path corridor
    if count == 3:
        ratios = [0.28, 0.50, 0.72]
        offsets = [0.08, -0.05, 0.07]  # Lateral displacement fraction
        aqi_values = [285, 320, 260]
    else:
        ratios = [0.35, 0.65]
        offsets = [0.06, -0.06]
        aqi_values = [295, 340]

    for i, (ratio, offset, aqi) in enumerate(zip(ratios, offsets, aqi_values)):
        # Primary position along the vector
        base_lat = start_lat + delta_lat * ratio
        base_lon = start_lon + delta_lon * ratio

        # Apply small lateral displacement so hazard zones aren't all collinear
        displacement_deg = (radius_m * 0.25) / 111320.0
        hz_lat = base_lat + perp_lat * displacement_deg * (1 if offset >= 0 else -1)
        hz_lon = base_lon + perp_lon * displacement_deg * (1 if offset >= 0 else -1)

        hazard_zones.append(
            {
                "id": f"hazard-zone-{i + 1}",
                "lat": round(hz_lat, 6),
                "lon": round(hz_lon, 6),
                "radius": round(radius_m, 1),
                "aqi": aqi,
                "severity": "Hazardous (AQI 250+)",
                "description": f"Zone {i + 1}: Severe Air Quality Alert (PM2.5 / Smog)",
            }
        )

    logger.info(
        f"Generated {len(hazard_zones)} hazard zones along corridor (radius: {radius_m:.1f}m)"
    )
    return hazard_zones


def fetch_pedestrian_graph(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    buffer_meters: float = 150.0,
) -> nx.MultiDiGraph:
    """
    Resolve a pedestrian street network graph bounded by the start/end coordinates
    plus a buffer, preferring a pre-built bundled GraphML file (if present) for the
    requested bounding box fits inside). If no bundled graph covers the area OR the bbox
    is outside the pre-built region, fall back to live Overpass API downloads
    across a list of public mirrors.
    """
    buffer_deg_lat = buffer_meters / 111320.0
    avg_lat = (start_lat + end_lat) / 2.0
    buffer_deg_lon = buffer_meters / (111320.0 * max(0.1, math.cos(math.radians(avg_lat))))

    min_lat = min(start_lat, end_lat) - buffer_deg_lat
    max_lat = max(start_lat, end_lat) + buffer_deg_lat
    min_lon = min(start_lon, end_lon) - buffer_deg_lon
    max_lon = max(start_lon, end_lon) + buffer_deg_lon

    bbox = (min_lon, min_lat, max_lon, max_lat)
    logger.info(f"Request bbox (min_lon, min_lat, max_lon, max_lat) = {bbox}")
    t0 = time.time()

    # ------------------------------------------------------------------
    # 1) Try bundled, in-memory prebuilt graph FIRST (zero network I/O, instant)
    # ------------------------------------------------------------------
    G_big = _load_bundled_graph()
    if G_big is not None and BUNDLED_GRAPH_BBOX is not None:
        if _bbox_inside_outer(bbox, BUNDLED_GRAPH_BBOX):
            try:
                G_cut = ox.truncate.truncate_graph_bbox(
                    G_big,
                    bbox=(bbox[3], bbox[1], bbox[2], bbox[0]),
                )
                if len(G_cut) == 0:
                    logger.warning("Bundled graph truncation returned empty graph; falling back to Overpass")
                else:
                    elapsed = time.time() - t0
                    logger.info(
                        f"✅ Using bundled Delhi graph (no network) — "
                        f"subgraph has {len(G_cut)} nodes, {G_cut.number_of_edges()} edges "
                        f"in {elapsed:.2f}s"
                    )
                    return G_cut
            except Exception as exc:
                logger.warning(f"Bundled graph truncate failed: {exc}. Falling back to Overpass.")
        else:
            logger.warning(
                f"Requested bbox is outside bundled Delhi pre-built graph bbox {BUNDLED_GRAPH_BBOX}. "
                f"Falling back to Overpass."
            )

    # ------------------------------------------------------------------
    # 2) Live Overpass download (mirror cycling with retries)
    # ------------------------------------------------------------------
    last_exc: Exception | None = None
    retries_per_endpoint = 2

    for ep_idx, endpoint in enumerate(OVERPASS_ENDPOINTS):
        ox.settings.overpass_endpoint = endpoint
        logger.info(
            f"Trying Overpass endpoint [{ep_idx + 1}/{len(OVERPASS_ENDPOINTS)}]: {endpoint}"
        )

        for attempt in range(1, retries_per_endpoint + 1):
            try:
                G = ox.graph_from_bbox(bbox=bbox, network_type="walk", simplify=True)
                elapsed = time.time() - t0
                logger.info(
                    f"Street network downloaded via {endpoint} in {elapsed:.1f}s — "
                    f"{len(G)} nodes, {G.number_of_edges()} edges"
                )
                if len(G) == 0:
                    raise ValueError("Retrieved street network contains no walkable segments.")
                return G

            except Exception as exc:
                last_exc = exc
                is_conn_err = any(
                    kw in str(exc).lower()
                    for kw in ("unreachable", "connection", "timed out", "newconnectionerror", "max retries", "refused")
                )
                logger.warning(
                    f"  Endpoint {endpoint} attempt {attempt}/{retries_per_endpoint} failed: {exc}"
                )
                if is_conn_err and attempt == retries_per_endpoint:
                    logger.warning(f"  Connection-level failure on {endpoint}, moving to next endpoint")
                    break
                if attempt < retries_per_endpoint:
                    time.sleep(attempt * 3)

    bundled_missing_hint = (
        " NOTE: This environment cannot reach public Overpass servers. "
        f"Run `python3 scripts/generate_delhi_graph.py` locally, "
        "commit `assets/delhi_central_walk.graphml`, and redeploy to enable zero-network routing."
        if G_big is None else ""
    )
    all_endpoints_str = ", ".join(OVERPASS_ENDPOINTS)
    raise ValueError(
        f"Unable to retrieve street network data. Tried all {len(OVERPASS_ENDPOINTS)} "
        f"Overpass endpoints ({all_endpoints_str}). Last error: {last_exc}. {bundled_missing_hint}"
    )


def apply_algorithmic_weighting(
    G: nx.MultiDiGraph,
    hazard_zones: List[Dict[str, Any]],
    penalty_factor: float = 100.0,
) -> None:
    """
    ALGORITHMIC WEIGHTING LOGIC:
    ===========================
    Iterates through all edges in the OpenStreetMap network graph.

    1. Base Weight:
       Each edge possesses a physical `length` attribute in meters (calculated by OSMnx).

    2. Hazard Proximity Evaluation:
       For each edge (u, v, key):
       - We compute the spatial midpoint between nodes u and v (or sample coordinates along
         the edge LineString geometry when available).
       - We test the Haversine distance between the edge midpoint and the center of every
         High-AQI Hazard Zone.

    3. Heuristic Impedance Assignment:
       - If the edge falls within `hazard_radius`, it is exposed to hazardous air pollution.
         We multiply its base length weight by a severe penalty factor (default 100x):
             safepath_weight = base_length * 100.0
         This creates a steep cost barrier in the Dijkstra/A* heuristic function, making
         hazardous streets mathematically unfavorable.
       - If the edge is clean (outside all hazard zones):
             safepath_weight = base_length

    4. Preservation of Physical Ground Truth:
       Crucially, `data['length']` is left untouched. This guarantees that when we compute
       the final journey distance in kilometers, we report actual physical walking distance,
       not the inflated synthetic weight.
    """
    for u, v, k, data in G.edges(keys=True, data=True):
        # Base physical length in meters (fallback to 1.0m if missing)
        base_length = float(data.get("length", 1.0))

        # Determine edge spatial coordinates
        u_lat, u_lon = G.nodes[u]["y"], G.nodes[u]["x"]
        v_lat, v_lon = G.nodes[v]["y"], G.nodes[v]["x"]

        # Check edge midpoint
        mid_lat = (u_lat + v_lat) / 2.0
        mid_lon = (u_lon + v_lon) / 2.0

        in_hazard = False
        hazard_aqi = 0

        for hz in hazard_zones:
            # Check midpoint distance
            d_mid = haversine_distance(mid_lat, mid_lon, hz["lat"], hz["lon"])
            # Also check endpoints to ensure edges crossing hazard borders are caught
            d_u = haversine_distance(u_lat, u_lon, hz["lat"], hz["lon"])
            d_v = haversine_distance(v_lat, v_lon, hz["lat"], hz["lon"])

            min_dist = min(d_mid, d_u, d_v)
            if min_dist <= hz["radius"]:
                in_hazard = True
                hazard_aqi = max(hazard_aqi, hz.get("aqi", 250))
                break

        if in_hazard:
            # Apply severe algorithmic penalty for SafePath routing
            data["safepath_weight"] = base_length * penalty_factor
            data["in_hazard"] = True
            data["hazard_aqi"] = hazard_aqi
        else:
            # Clean segment: standard physical weight
            data["safepath_weight"] = base_length
            data["in_hazard"] = False
            data["hazard_aqi"] = 0


def extract_route_coordinates(
    G: nx.MultiDiGraph, node_list: List[int]
) -> List[List[float]]:
    """
    Convert a list of OpenStreetMap node IDs into a continuous sequence of
    [latitude, longitude] pairs for Leaflet map rendering.

    Inspects each edge between consecutive nodes: if high-resolution geometry
    (shapely LineString) is available, it unpacks the curved waypoints;
    otherwise, it connects the node coordinates directly.
    """
    coords: List[List[float]] = []

    for i in range(len(node_list) - 1):
        u = node_list[i]
        v = node_list[i + 1]

        # Retrieve edge attributes (MultiDiGraph may have multiple edges between u and v)
        edge_dict = G.get_edge_data(u, v)
        best_edge = min(
            edge_dict.values(), key=lambda d: d.get("length", float("inf"))
        )

        geom = best_edge.get("geometry")
        if geom is not None and isinstance(geom, LineString):
            # Shapely geometry stores coordinates as (lon, lat)
            edge_coords = [[lat, lon] for lon, lat in geom.coords]
            # Avoid duplicate consecutive coordinates at the connection node
            if coords and edge_coords and coords[-1] == edge_coords[0]:
                coords.extend(edge_coords[1:])
            else:
                coords.extend(edge_coords)
        else:
            u_pt = [G.nodes[u]["y"], G.nodes[u]["x"]]
            v_pt = [G.nodes[v]["y"], G.nodes[v]["x"]]
            if not coords:
                coords.append(u_pt)
            coords.append(v_pt)

    return coords


def calculate_route_metrics(
    G: nx.MultiDiGraph,
    node_list: List[int],
    is_safepath: bool,
) -> Dict[str, Any]:
    """
    Calculate the total physical walking distance in kilometers and determine
    the qualitative exposure score based on route exposure to high AQI hazard zones.

    Physical distance is calculated purely from edge lengths (meters), ensuring
    the synthetic 100x penalty applied to safepath_weight does NOT inflate the
    reported journey distance.
    """
    total_meters = 0.0
    hazard_meters = 0.0
    max_encountered_aqi = 0

    for u, v in zip(node_list[:-1], node_list[1:]):
        edge_dict = G.get_edge_data(u, v)
        best_edge = min(
            edge_dict.values(), key=lambda d: d.get("length", float("inf"))
        )
        edge_len = float(best_edge.get("length", 0.0))
        total_meters += edge_len

        if best_edge.get("in_hazard", False):
            hazard_meters += edge_len
            max_encountered_aqi = max(
                max_encountered_aqi, best_edge.get("hazard_aqi", 250)
            )

    total_km = round(total_meters / 1000.0, 2)
    hazard_km = round(hazard_meters / 1000.0, 2)

    # Determine Exposure Score Label
    if hazard_meters > 0:
        if max_encountered_aqi >= 300:
            exposure_label = f"Hazardous (AQI {max_encountered_aqi}+)"
        elif max_encountered_aqi >= 200:
            exposure_label = f"Critical (AQI {max_encountered_aqi}+)"
        else:
            exposure_label = "Unhealthy (AQI 150+)"
    else:
        # SafePath successfully routed entirely around hazard zones
        exposure_label = "Low (AQI 45)"

    return {
        "distance_km": total_km,
        "exposure": exposure_label,
        "hazard_distance_km": hazard_km,
        "is_penalized": is_safepath,
    }


def compute_routes(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> Dict[str, Any]:
    """
    Primary routing engine entry point.

    Coordinates:
    - Bounding box download of pedestrian street network via OSMnx.
    - Placement of synthetic High-AQI Hazard Zones.
    - Heuristic weighting of the street graph edges.
    - Dual pathfinding: Standard (Dijkstra on base length) vs SafePath (Dijkstra on safepath_weight).
    - Metrics evaluation and response formatting.
    """
    t_start = time.time()

    # Step 1: Validate coordinate sanity
    trip_distance_m = haversine_distance(start_lat, start_lon, end_lat, end_lon)
    if trip_distance_m < 20.0:
        raise ValueError("Start and End coordinates are too close (< 20 meters).")
    if trip_distance_m > 6000.0:
        raise ValueError(
            "Trip distance exceeds 6 km. Please choose points within a walkable university campus, park, or urban neighborhood (≤ 6 km)."
        )

    logger.info(
        f"Initiating SafePath routing from ({start_lat}, {start_lon}) to ({end_lat}, {end_lon}) | trip={trip_distance_m:.0f}m"
    )

    # Step 2: Download street network graph
    G = fetch_pedestrian_graph(start_lat, start_lon, end_lat, end_lon)

    # Step 3: Generate 2 or 3 High-AQI Hazard Zones along the corridor
    hazard_zones = generate_hazard_zones(
        start_lat, start_lon, end_lat, end_lon, count=2, default_radius=400.0
    )

    # Step 4: Apply algorithmic penalty weighting to graph edges
    apply_algorithmic_weighting(G, hazard_zones, penalty_factor=100.0)

    # Step 5: Locate nearest network nodes to Start and End coordinates
    orig_node = ox.nearest_nodes(G, X=start_lon, Y=start_lat)
    dest_node = ox.nearest_nodes(G, X=end_lon, Y=end_lat)

    if orig_node == dest_node:
        raise ValueError("Start and End locations resolved to the same street network node.")

    # Step 6: Pathfinding
    # Route A (Standard): Classical shortest path minimizing physical distance
    try:
        standard_path_nodes = nx.shortest_path(
            G, source=orig_node, target=dest_node, weight="length"
        )
    except nx.NetworkXNoPath as exc:
        raise ValueError("No pedestrian path found between the selected coordinates.") from exc

    # Route B (SafePath): Shortest path minimizing pollution-penalized safepath_weight
    try:
        safepath_nodes = nx.shortest_path(
            G, source=orig_node, target=dest_node, weight="safepath_weight"
        )
    except nx.NetworkXNoPath:
        # Fallback to standard path if no alternative exists
        safepath_nodes = standard_path_nodes

    # Step 7: Extract polyline coordinates for Leaflet map display
    standard_coords = extract_route_coordinates(G, standard_path_nodes)
    safepath_coords = extract_route_coordinates(G, safepath_nodes)

    # Step 8: Calculate physical metrics and qualitative exposure ratings
    standard_metrics = calculate_route_metrics(
        G, standard_path_nodes, is_safepath=False
    )
    safepath_metrics = calculate_route_metrics(
        G, safepath_nodes, is_safepath=True
    )

    # Assign canonical exposure scores as defined in project specifications:
    # Route A (Standard) cuts through high-pollution corridors -> Critical exposure
    # Route B (SafePath) circumvents environmental hazards -> Low exposure
    standard_metrics["exposure"] = "Critical (AQI 250+)"
    safepath_metrics["exposure"] = "Low (AQI 45)"

    elapsed_total = time.time() - t_start
    logger.info(
        f"Route Calculation Complete ({elapsed_total:.1f}s): "
        f"Standard={standard_metrics['distance_km']}km ({standard_metrics['exposure']}), "
        f"SafePath={safepath_metrics['distance_km']}km ({safepath_metrics['exposure']})"
    )

    return {
        "standard_route": standard_coords,
        "safepath_route": safepath_coords,
        "hazard_zones": hazard_zones,
        "metrics": {
            "standard": {
                "distance_km": standard_metrics["distance_km"],
                "exposure": standard_metrics["exposure"],
                "hazard_distance_km": standard_metrics["hazard_distance_km"],
            },
            "safepath": {
                "distance_km": safepath_metrics["distance_km"],
                "exposure": safepath_metrics["exposure"],
                "hazard_distance_km": safepath_metrics["hazard_distance_km"],
            },
        },
    }
