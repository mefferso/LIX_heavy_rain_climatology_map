#!/usr/bin/env python3
"""Build LIX heavy-rain climatology from NOAA nClimGrid-Daily.

The first run processes 1951 through the latest complete calendar year. Yearly
threshold counts and annual maxima are retained so arbitrary climatology periods
and comparison views can be generated later without rereading the full archive.
Output JSON is static so the web map can live on GitHub Pages with no server.

Data are read from NOAA's public NODD S3 copy using HTTP byte-range requests.
Only the LIX-area precipitation subset is loaded from each monthly NetCDF file;
the full CONUS monthly files are never downloaded.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import fsspec
import numpy as np
import pandas as pd
import requests
import xarray as xr
from shapely import contains_xy
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
DOCS_DATA = ROOT / "docs" / "data"
STATE_DIR = ROOT / "data"
STATE_FILE = STATE_DIR / "climatology_state.npz"

START_YEAR = 1951
THRESHOLDS_IN = np.array([1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0], dtype=np.float32)
THRESHOLDS_MM = THRESHOLDS_IN * 25.4
STATE_SCHEMA_VERSION = 2

CWA_URL = (
    "https://mapservices.weather.noaa.gov/static/rest/services/"
    "nws_reference_maps/nws_reference_map/FeatureServer/1/query"
)
NODD_ROOT = "https://noaa-nclimgrid-daily-pds.s3.amazonaws.com/access/grids"
USER_AGENT = "LIX-heavy-rain-climatology/1.0 (NOAA climate analysis; GitHub Pages)"
HTTP_BLOCK_SIZE = 8 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--end-year",
        type=int,
        default=datetime.now(timezone.utc).year - 1,
        help="Latest complete calendar year to include (default: previous UTC year).",
    )
    parser.add_argument("--rebuild", action="store_true", help="Ignore saved aggregation state.")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("NCLIMGRID_WORKERS", "12")),
        help="Concurrent monthly NOAA NODD range reads.",
    )
    return parser.parse_args()


def fetch_cwa() -> dict:
    params = {
        "where": "cwa='LIX'",
        "outFields": "cwa,wfo,citystate",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    response = requests.get(CWA_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=60)
    response.raise_for_status()
    geojson = response.json()
    if not geojson.get("features"):
        raise RuntimeError("NWS reference service returned no LIX CWA feature")
    return geojson


def period_metadata(end_year: int) -> list[dict]:
    specs = [
        ("full", f"{START_YEAR}–{end_year} · full record", START_YEAR, end_year),
        ("era_2005_2025", "2005–2025", 2005, 2025),
        ("era_1985_2005", "1985–2005", 1985, 2005),
        ("era_1965_1985", "1965–1985", 1965, 1985),
        ("early_1951_1990", "1951–1990", 1951, 1990),
        ("modern_1991_2025", "1991–2025", 1991, 2025),
    ]
    periods = []
    for period_id, label, start_year, stop_year in specs:
        if start_year > end_year:
            continue
        stop_year = min(stop_year, end_year)
        if stop_year < start_year:
            continue
        periods.append(
            {
                "id": period_id,
                "label": label,
                "start_year": start_year,
                "end_year": stop_year,
                "years": stop_year - start_year + 1,
                "file": f"data/climatology_{period_id}.json",
            }
        )
    return periods


def comparison_metadata() -> list[dict]:
    return [
        {
            "id": "modern_vs_early",
            "label": "1991–2025 minus 1951–1990",
            "newer_period": "modern_1991_2025",
            "older_period": "early_1951_1990",
            "description": "Modern record compared with the earlier 1951–1990 baseline.",
        },
        {
            "id": "latest_vs_mid",
            "label": "2005–2025 minus 1985–2005",
            "newer_period": "era_2005_2025",
            "older_period": "era_1985_2005",
            "description": "Latest era compared with the preceding era.",
        },
        {
            "id": "mid_vs_early",
            "label": "1985–2005 minus 1965–1985",
            "newer_period": "era_1985_2005",
            "older_period": "era_1965_1985",
            "description": "Middle era compared with the earlier era.",
        },
        {
            "id": "latest_vs_early",
            "label": "2005–2025 minus 1965–1985",
            "newer_period": "era_2005_2025",
            "older_period": "era_1965_1985",
            "description": "Latest era compared directly with the earliest 21-year era.",
        },
    ]


def dataset_candidates(year: int, month: int) -> list[str]:
    ym = f"{year}{month:02d}"
    return [f"ncdd-{ym}-grd-scaled.nc", f"ncdd-{ym}-grd-prelim.nc"]


def _read_nodd(
    url: str,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    west, south, east, north = bbox
    remote = fsspec.open(url, mode="rb", block_size=HTTP_BLOCK_SIZE, cache_type="readahead")
    with remote as fh:
        with xr.open_dataset(fh, engine="h5netcdf", decode_times=True) as ds:
            if "prcp" not in ds:
                raise RuntimeError("dataset does not contain prcp")
            if "lat" not in ds.coords or "lon" not in ds.coords:
                raise RuntimeError("dataset is missing lat/lon coordinates")

            lat_values = np.asarray(ds["lat"].values)
            lon_values = np.asarray(ds["lon"].values)
            lat_slice = slice(south, north) if lat_values[0] <= lat_values[-1] else slice(north, south)
            lon_slice = slice(west, east) if lon_values[0] <= lon_values[-1] else slice(east, west)

            da = ds["prcp"].sel(lat=lat_slice, lon=lon_slice).transpose("time", "lat", "lon").load()
            if da.size == 0:
                raise RuntimeError("NODD subset returned no precipitation cells")

            units = str(da.attrs.get("units", "")).lower()
            if units and units not in {"mm", "millimeter", "millimeters"}:
                raise RuntimeError(f"Unexpected precipitation units: {da.attrs.get('units')}")

            values = np.asarray(da.values, dtype=np.float32)
            lats = np.asarray(da["lat"].values, dtype=np.float64)
            lons = np.asarray(da["lon"].values, dtype=np.float64)
            dates = pd.to_datetime(np.asarray(da["time"].values)).strftime("%Y%m%d").astype(np.int32)

    return values, lats, lons, dates


def fetch_month(
    year: int,
    month: int,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    errors: list[str] = []
    for filename in dataset_candidates(year, month):
        url = f"{NODD_ROOT}/{year}/{filename}"
        for attempt in range(1, 5):
            try:
                return _read_nodd(url, bbox)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{filename} attempt {attempt}: {exc}")
                if attempt < 4:
                    time.sleep(min(8, 2 ** (attempt - 1)))
    tail = " | ".join(errors[-8:])
    raise RuntimeError(f"Unable to read {year}-{month:02d} from NOAA NODD: {tail}")


def initialize_state(
    grid_lats: np.ndarray,
    grid_lons: np.ndarray,
    cwa_geometry,
    end_year: int,
) -> dict:
    lon2d, lat2d = np.meshgrid(grid_lons, grid_lats)
    inside = contains_xy(cwa_geometry, lon2d, lat2d)
    rows, cols = np.where(inside)
    if rows.size == 0:
        raise RuntimeError("No nClimGrid points were found inside the LIX CWA")

    npoints = rows.size
    nyears = end_year - START_YEAR + 1
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_year": START_YEAR - 1,
        "thresholds_in": THRESHOLDS_IN.copy(),
        "grid_lats": grid_lats.copy(),
        "grid_lons": grid_lons.copy(),
        "rows": rows.astype(np.int16),
        "cols": cols.astype(np.int16),
        "point_lats": grid_lats[rows].astype(np.float32),
        "point_lons": grid_lons[cols].astype(np.float32),
        "yearly_counts": np.zeros((nyears, len(THRESHOLDS_IN), 12, npoints), dtype=np.uint16),
        "yearly_max_mm": np.full((nyears, npoints), -np.inf, dtype=np.float32),
        "yearly_max_date": np.zeros((nyears, npoints), dtype=np.int32),
    }


def expand_state_to_year(state: dict, end_year: int) -> None:
    wanted = end_year - START_YEAR + 1
    current = state["yearly_counts"].shape[0]
    if wanted <= current:
        return
    add = wanted - current
    npoints = len(state["point_lats"])
    state["yearly_counts"] = np.concatenate(
        [state["yearly_counts"], np.zeros((add, len(THRESHOLDS_IN), 12, npoints), dtype=np.uint16)],
        axis=0,
    )
    state["yearly_max_mm"] = np.concatenate(
        [state["yearly_max_mm"], np.full((add, npoints), -np.inf, dtype=np.float32)], axis=0
    )
    state["yearly_max_date"] = np.concatenate(
        [state["yearly_max_date"], np.zeros((add, npoints), dtype=np.int32)], axis=0
    )


def load_state(end_year: int, rebuild: bool) -> dict | None:
    if rebuild or not STATE_FILE.exists():
        return None
    with np.load(STATE_FILE, allow_pickle=False) as z:
        if "schema_version" not in z or int(z["schema_version"]) != STATE_SCHEMA_VERSION:
            print("Saved state uses the old aggregate schema; rebuilding yearly state from 1951.", flush=True)
            return None
        saved_thresholds = z["thresholds_in"]
        last_year = int(z["last_year"])
        if not np.array_equal(saved_thresholds, THRESHOLDS_IN):
            print("Threshold definition changed; rebuilding from 1951.", flush=True)
            return None
        if last_year > end_year:
            print("Saved state extends beyond requested end year; rebuilding.", flush=True)
            return None
        state = {
            "schema_version": int(z["schema_version"]),
            "last_year": last_year,
            "thresholds_in": saved_thresholds.copy(),
            "grid_lats": z["grid_lats"].copy(),
            "grid_lons": z["grid_lons"].copy(),
            "rows": z["rows"].copy(),
            "cols": z["cols"].copy(),
            "point_lats": z["point_lats"].copy(),
            "point_lons": z["point_lons"].copy(),
            "yearly_counts": z["yearly_counts"].copy(),
            "yearly_max_mm": z["yearly_max_mm"].copy(),
            "yearly_max_date": z["yearly_max_date"].copy(),
        }
    expand_state_to_year(state, end_year)
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        STATE_FILE,
        schema_version=np.int16(STATE_SCHEMA_VERSION),
        last_year=np.int32(state["last_year"]),
        thresholds_in=state["thresholds_in"],
        grid_lats=state["grid_lats"],
        grid_lons=state["grid_lons"],
        rows=state["rows"],
        cols=state["cols"],
        point_lats=state["point_lats"],
        point_lons=state["point_lons"],
        yearly_counts=state["yearly_counts"],
        yearly_max_mm=state["yearly_max_mm"],
        yearly_max_date=state["yearly_max_date"],
    )


def process_month_data(
    data: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    year: int,
    month: int,
    state: dict,
) -> None:
    values, lats, lons, dates = data
    if values.shape[1:] != (len(state["grid_lats"]), len(state["grid_lons"])):
        raise RuntimeError(
            f"Grid shape changed in {year}-{month:02d}: {values.shape[1:]} != "
            f"{(len(state['grid_lats']), len(state['grid_lons']))}"
        )
    if not np.allclose(lats, state["grid_lats"], atol=1e-6) or not np.allclose(
        lons, state["grid_lons"], atol=1e-6
    ):
        raise RuntimeError(f"Grid coordinates changed in {year}-{month:02d}")

    selected = values[:, state["rows"], state["cols"]]
    selected_safe = np.where(np.isfinite(selected), selected, -np.inf)
    year_index = year - START_YEAR

    threshold_counts = np.stack(
        [np.count_nonzero(selected >= threshold_mm, axis=0) for threshold_mm in THRESHOLDS_MM],
        axis=0,
    ).astype(np.uint16)
    state["yearly_counts"][year_index, :, month - 1, :] = threshold_counts

    max_indices = np.argmax(selected_safe, axis=0)
    point_index = np.arange(selected_safe.shape[1])
    month_max = selected_safe[max_indices, point_index]
    month_dates = dates[max_indices]
    update = month_max > state["yearly_max_mm"][year_index]
    state["yearly_max_mm"][year_index, update] = month_max[update]
    state["yearly_max_date"][year_index, update] = month_dates[update]


def process_jobs(
    state: dict,
    bbox: tuple[float, float, float, float],
    jobs: Iterable[tuple[int, int]],
    workers: int,
) -> None:
    jobs = list(jobs)
    total = len(jobs)
    if total == 0:
        return
    print(f"Processing {total} NOAA NODD monthly subsets with {workers} workers.", flush=True)
    failures: list[str] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_month, y, m, bbox): (y, m) for y, m in jobs}
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                process_month_data(future.result(), year, month, state)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{year}-{month:02d}: {exc}")
            completed += 1
            if completed % 6 == 0 or completed == total:
                print(f"  {completed}/{total} months complete", flush=True)
    if failures:
        raise RuntimeError(f"{len(failures)} monthly NODD reads failed:\n" + "\n".join(failures[:30]))


def aggregate_period(state: dict, meta: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    start_idx = meta["start_year"] - START_YEAR
    end_idx = meta["end_year"] - START_YEAR + 1
    counts = state["yearly_counts"][start_idx:end_idx].sum(axis=0, dtype=np.uint32)

    maxima = state["yearly_max_mm"][start_idx:end_idx]
    dates = state["yearly_max_date"][start_idx:end_idx]
    best_year_idx = np.argmax(maxima, axis=0)
    point_idx = np.arange(maxima.shape[1])
    max_mm = maxima[best_year_idx, point_idx]
    max_date = dates[best_year_idx, point_idx]
    return counts, max_mm, max_date


def write_period_json(state: dict, meta: dict) -> None:
    counts, max_mm, max_date = aggregate_period(state, meta)
    points = []
    for i in range(len(state["point_lats"])):
        points.append(
            {
                "lat": round(float(state["point_lats"][i]), 5),
                "lon": round(float(state["point_lons"][i]), 5),
                "c": counts[:, :, i].astype(int).tolist(),
                "mx": round(float(max_mm[i]), 2) if np.isfinite(max_mm[i]) else None,
                "md": int(max_date[i]) if max_date[i] else None,
            }
        )
    payload = {
        "period": {k: meta[k] for k in ("id", "label", "start_year", "end_year", "years")},
        "thresholds_in": THRESHOLDS_IN.astype(float).tolist(),
        "points": points,
    }
    output = DOCS_DATA / Path(meta["file"]).name
    output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)} ({output.stat().st_size / 1_000_000:.1f} MB)", flush=True)


def write_outputs(state: dict, cwa_geojson: dict, end_year: int) -> None:
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    (DOCS_DATA / "lix_cwa.geojson").write_text(
        json.dumps(cwa_geojson, separators=(",", ":")), encoding="utf-8"
    )
    periods = period_metadata(end_year)
    keep_files = {Path(meta["file"]).name for meta in periods}
    for old_file in DOCS_DATA.glob("climatology_*.json"):
        if old_file.name not in keep_files:
            old_file.unlink()
    for meta in periods:
        write_period_json(state, meta)

    manifest = {
        "status": "ready",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": "NOAA/NCEI nClimGrid-Daily v1.0.0 via NOAA Open Data Dissemination",
        "source_url": "https://registry.opendata.aws/noaa-nclimgrid-daily/",
        "grid_resolution_deg": 1 / 24,
        "thresholds_in": THRESHOLDS_IN.astype(float).tolist(),
        "periods": periods,
        "comparisons": comparison_metadata(),
        "cwa_file": "data/lix_cwa.geojson",
        "era_note": (
            "The 2005–2025, 1985–2005, and 1965–1985 era labels are inclusive; "
            "adjacent eras therefore share the boundary year exactly as requested."
        ),
        "daily_period_note": (
            "These are nClimGrid-Daily fixed daily precipitation analyses derived from "
            "GHCN-Daily station observations, not arbitrary rolling 24-hour maxima."
        ),
    }
    (DOCS_DATA / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.end_year < 2025:
        raise SystemExit("end-year must be 2025 or later for the configured era comparisons")

    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    cwa_geojson = fetch_cwa()
    cwa_geometry = shape(cwa_geojson["features"][0]["geometry"])
    west, south, east, north = cwa_geometry.bounds
    bbox = (west - 0.10, south - 0.10, east + 0.10, north + 0.10)
    print(f"LIX CWA bbox: {bbox}", flush=True)

    state = load_state(args.end_year, args.rebuild)
    if state is None:
        print("Reading 1951-01 from NOAA NODD to establish grid and CWA mask…", flush=True)
        first = fetch_month(START_YEAR, 1, bbox)
        state = initialize_state(first[1], first[2], cwa_geometry, args.end_year)
        process_month_data(first, START_YEAR, 1, state)
        first_year = START_YEAR
        first_year_month = 2
    else:
        print(f"Loaded yearly aggregation state through {state['last_year']}.", flush=True)
        first_year = state["last_year"] + 1
        first_year_month = 1

    for year in range(first_year, args.end_year + 1):
        start_month = first_year_month if year == first_year else 1
        jobs = [(year, month) for month in range(start_month, 13)]
        process_jobs(state, bbox, jobs, args.workers)
        state["last_year"] = year
        save_state(state)
        print(f"Saved yearly aggregation state through {year}.", flush=True)

    if state["last_year"] < args.end_year:
        raise RuntimeError(f"Aggregation stopped at {state['last_year']} but end year is {args.end_year}")

    save_state(state)
    write_outputs(state, cwa_geojson, args.end_year)
    print(f"Done. Climatology is current through {args.end_year}.", flush=True)


if __name__ == "__main__":
    main()
