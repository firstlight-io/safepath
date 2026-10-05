#!/usr/bin/env python3
"""
SafePath — Pre-generate a Delhi-central pedestrian street network GraphML bundle.

Run this ONCE on your localhost (where Overpass API or internet access works).
It downloads a large but reasonable walking-graph bounding box that covers all
three Delhi demo presets plus a generous margin so every possible coordinate
pair within the 6km app limit is fully contained.

The resulting `assets/delhi_central_walk.graphml` ships with the repo. On Render
(or anywhere else that blocks public Overpass endpoints), the app cuts a
subgraph from this bundle at request time instead of calling Overpass.

Usage:
    python3 scripts/generate_delhi_graph.py
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [graph-builder] %(message)s",
)
log = logging.getLogger("graph-builder")

# ---------------------------------------------------------------------------
# Bounding box: Central Delhi, India
# Covers all three demo presets with large margins so the 6km max route in the
# app always fits entirely inside even with the worst endpoint positions.
#
#   lat (y): 28.56 (S) .. 28.70 (N)  ≈ 15.6 km  N-S
#   lon (x): 77.16 (W) .. 77.26 (E)  ≈ 11.1 km  E-W  (at 28.63° lat)
#
#   Area ≈ 173 km²  (walk network ~12k nodes / 30k edges / 8-15 MB GraphML)
# ---------------------------------------------------------------------------
BBOX_NORTH = 28.705
BBOX_SOUTH = 28.555
BBOX_EAST = 77.265
BBOX_WEST = 77.155

OUT_PATH = os.path.join(ASSETS_DIR, "delhi_central_walk.graphml")

PRESETS = {
    "Lodi Garden": (28.5931, 77.2199),
    "Khan Market": (28.6005, 77.2272),
    "India Gate":  (28.6129, 77.2295),
    "CP":          (28.6315, 77.2167),
    "DU North":    (28.6880, 77.2150),
    "Kashmiri Gate": (28.6675, 77.2286),
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def main() -> int:
    import osmnx as ox

    # Consistent engine settings
    ox.settings.use_cache = True
    ox.settings.cache_folder = CACHE_DIR
    ox.settings.overpass_rate_limit = False
    ox.settings.timeout = 180  # generous for a one-off large download
    ox.settings.http_user_agent = (
        "SafePath-Routing-Engine-GraphBuilder/1.0 (local-graph-build@safepath.local)"
    )

    log.info("=" * 66)
    log.info("SafePath — Delhi-central walk network graph builder")
    log.info(f"  BBOX N/S/E/W : {BBOX_NORTH}, {BBOX_SOUTH}, {BBOX_EAST}, {BBOX_WEST}")
    log.info(f"  N-S span     : {haversine_m(BBOX_SOUTH, (BBOX_EAST+BBOX_WEST)/2, BBOX_NORTH, (BBOX_EAST+BBOX_WEST)/2)/1000:.1f} km")
    log.info(f"  E-W span     : {haversine_m((BBOX_NORTH+BBOX_SOUTH)/2, BBOX_WEST, (BBOX_NORTH+BBOX_SOUTH)/2, BBOX_EAST)/1000:.1f} km")
    log.info(f"  Output file  : {OUT_PATH}")
    log.info("=" * 66)

    log.info("Downloading walk network from Overpass (this takes 20-60 seconds)...")
    t0 = time.time()

    G = ox.graph_from_bbox(
        bbox=(BBOX_WEST, BBOX_SOUTH, BBOX_EAST, BBOX_NORTH),
        network_type="walk",
        simplify=True,
    )
    dl_elapsed = time.time() - t0
    log.info(
        f"Download done in {dl_elapsed:.1f}s — "
        f"{len(G)} nodes, {G.number_of_edges()} edges"
    )

    # Sanity check: all three demo preset coordinates resolve to nodes
    log.info("Verifying all 6 Delhi preset coordinates resolve inside the graph...")
    for name, (lat, lon) in PRESETS.items():
        if not (BBOX_SOUTH <= lat <= BBOX_NORTH and BBOX_WEST <= lon <= BBOX_EAST):
            log.error(f"  ❌ {name} ({lat}, {lon}) is OUTSIDE the bbox!")
            return 2
        node_id = ox.nearest_nodes(G, X=lon, Y=lat)
        nlat, nlon = G.nodes[node_id]["y"], G.nodes[node_id]["x"]
        snap = haversine_m(lat, lon, nlat, nlon)
        status = "✅" if snap < 200 else "⚠️"
        log.info(f"  {status} {name:<16s} snaps to node {node_id} in {snap:.0f}m")

    # Final save
    log.info(f"Saving graph to {OUT_PATH} ...")
    ox.save_graphml(G, filepath=OUT_PATH)

    size_mb = os.path.getsize(OUT_PATH) / (1024 * 1024)
    total = time.time() - t0
    log.info("=" * 66)
    log.info(f"DONE in {total:.1f}s — {OUT_PATH} ({size_mb:.1f} MB)")
    log.info("Next steps:")
    log.info("  1. git add assets/delhi_central_walk.graphml")
    log.info("  2. git commit -m 'Add Delhi-central bundled walk graph (offline routing)'")
    log.info("  3. git push && redeploy Render")
    log.info("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
