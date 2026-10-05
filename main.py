"""
SafePath API Server
===================
FastAPI application delivering dynamically weighted heuristic pedestrian routing.

Endpoints:
- POST /api/route: Calculates Standard vs. SafePath routes, hazard zones, and metrics.
- GET /api/health: Health check endpoint.
- Static mount: Serves the Leaflet dark-mode UI directly from /static.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import compute_routes

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [SafePath API] %(message)s",
)
logger = logging.getLogger("safepath.api")

app = FastAPI(
    title="SafePath Routing Engine API",
    description="Dynamically weighted heuristic routing engine balancing travel distance against environmental AQI hazards.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RouteRequest(BaseModel):
    start_lat: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Origin latitude in WGS84 degrees",
        examples=[40.7290],
    )
    start_lon: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Origin longitude in WGS84 degrees",
        examples=[-73.9970],
    )
    end_lat: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Destination latitude in WGS84 degrees",
        examples=[40.7320],
    )
    end_lon: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Destination longitude in WGS84 degrees",
        examples=[-73.9930],
    )


class RouteMetric(BaseModel):
    distance_km: float = Field(..., description="Physical distance along the street network in kilometers")
    exposure: str = Field(..., description="Qualitative environmental exposure score (e.g. Critical vs Low)")
    hazard_distance_km: float = Field(..., description="Distance traversed through high-AQI hazard zones in kilometers")


class MetricsSummary(BaseModel):
    standard: RouteMetric
    safepath: RouteMetric


class HazardZone(BaseModel):
    id: str
    lat: float
    lon: float
    radius: float
    aqi: int
    severity: str
    description: str


class RouteResponse(BaseModel):
    standard_route: List[List[float]] = Field(
        ..., description="List of [latitude, longitude] pairs for Route A (Standard Shortest Path)"
    )
    safepath_route: List[List[float]] = Field(
        ..., description="List of [latitude, longitude] pairs for Route B (SafePath Detour)"
    )
    hazard_zones: List[HazardZone] = Field(
        ..., description="High AQI Hazard zones located between origin and destination"
    )
    metrics: MetricsSummary = Field(
        ..., description="Comparative metrics for Standard vs SafePath journeys"
    )


@app.get("/api/health", tags=["System"])
def health_check() -> Dict[str, str]:
    """Health check endpoint confirming API service status."""
    return {"status": "ok", "service": "SafePath Routing Engine"}


@app.post(
    "/api/route",
    response_model=RouteResponse,
    status_code=status.HTTP_200_OK,
    tags=["Routing"],
    summary="Calculate Standard and SafePath Pedestrian Routes",
)
def calculate_route_endpoint(payload: RouteRequest) -> RouteResponse:
    """
    Computes two pedestrian routes between Start and End coordinates:

    1. **Route A (Standard Route)**: Shortest distance path via OSMnx pedestrian graph.
    2. **Route B (SafePath Route)**: Cost-penalized path detouring away from High-AQI hazard zones.

    Returns the geometry for both routes, hazard zone locations, and comparative exposure metrics.
    """
    logger.info(
        f"Route request received: ({payload.start_lat}, {payload.start_lon}) -> ({payload.end_lat}, {payload.end_lon})"
    )

    try:
        result = compute_routes(
            start_lat=payload.start_lat,
            start_lon=payload.start_lon,
            end_lat=payload.end_lat,
            end_lon=payload.end_lon,
        )
        return RouteResponse(**result)

    except ValueError as val_err:
        logger.warning(f"Validation or boundary error: {val_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        ) from val_err

    except HTTPException:
        raise

    except Exception as exc:
        logger.error(f"Routing computation failure: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Routing engine could not complete the calculation. "
                "This may be due to the Overpass API (OpenStreetMap data source) "
                "being slow or unavailable — please try again in a few moments."
            ),
        ) from exc


# ==============================================================================
# Static UI Mounting
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(BASE_DIR, "static")
os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    """Serves the Leaflet.js Dark-Mode SafePath user interface."""
    index_file = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_file):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Frontend index.html file not found.",
        )
    return FileResponse(index_file, media_type="text/html")


@app.on_event("startup")
async def on_startup() -> None:
    """Log startup diagnostics — useful when debugging Render / PaaS deploys."""
    logger.info("=" * 60)
    logger.info("SafePath API Server starting up...")
    logger.info(f"  Python:   {sys.version.split()[0]}")
    logger.info(f"  Platform: {sys.platform}")
    logger.info(f"  CWD:      {os.getcwd()}")
    logger.info(f"  BASE_DIR: {BASE_DIR}")
    logger.info(f"  STATIC:   {static_dir} (exists={os.path.isdir(static_dir)})")
    port = os.environ.get("PORT", "8000")
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"  Bind:     {host}:{port}")
    logger.info("=" * 60)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    logger.info(f"Launching uvicorn on {host}:{port}")
    uvicorn.run("main:app", host=host, port=port, reload=False)
