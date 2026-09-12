#!/usr/bin/env python3
"""Build LIX heavy-rain climatology from NOAA nClimGrid-Daily.

The first run processes 1951 through the latest complete calendar year. A compact
NPZ state is retained so later annual runs only need to process newly completed
years. Output JSON is intentionally static so the web map can live on GitHub
Pages with no server.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

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
PERIOD_IDS = np.array(["full", "normals", "recent"])

CWA_URL = (
    "https://mapservices.weather.noaa.gov/static/rest/services/"
    "nws_reference_maps/nws_reference_map/FeatureServer/1/query"
)
NCSS_ROOT = "https://www.ncei.noaa.gov/thredds/ncss/grid/nclimgrid-daily"
USER_AGENT = "LIX-heavy-rain-climatology/1.0 (NOAA climate analysis; GitHub Pages)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--end-year",
        type=int,
        default=datetime.now(timezone.utc).year - 1,
        help="Latest complete calendar year to include (default: previous UTC year).",
    )
    parser.add_argument("--rebuild", action="store_true", help="Ignore saved aggregation state.")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("NCLIMGRID_WORKERS", "8")))
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
    return [
        {
            "id": "full",
            "label": f"{START_YEAR}–{end_year} (full record)",
            "start_year": START_YEAR,
            "end_year": end_year,
            "years": end_year - START_YEAR + 1,
            "file": "data/climatology_full.json",
        },
        {
            "id": "normals",
            "label": "1991–2020 normals",
            "start_year": 1991,
            "end_year": 2020,
            "years": 30,
            "file": "data/climatology_normals.json",
        },
        {
            "id": "recent",
            "label": f"2001–{end_year} (recent)",
            "start_year": 2001,
            "end_year": end_year,
            "years": end_year - 2001 + 1,
            "file": "data/climatology_recent.json",
        },
    ]


def period_indexes_for_year(year: int) -> list[int]:
    indexes = [0]  # full record
    if 1991 <= year <= 2020:
        indexes.append(1)
    if year >= 2001:
        indexes.append(2)
    return indexes


def dataset_candidates(year: int, month: int) -> list[str]:
    ym = f"{year}{month:02d}"
    return [
        f"ncdd-{ym}-grd-scaled.nc",
        f"prcp-{ym}-grd-scaled.nc",
        f"ncdd-{ym}-grd-prelim.nc",
        f"prcp-{ym}-grd-prelim.nc",
    ]


def download_month(year: int, month: int, bbox: tuple[float, float, float, float], dest: Path) -> Path:
    west, south, east, north = bbox
    params = {
        "var": "prcp",
        "north": f"{north:.5f}",
        "south": f"{south:.5f}",
        "east": f"{east:.5f}",
        "west": f"{west:.5f}",
        "horizStride": "1",
        "accept": "netcdf4",
    }
    errors = []
    for filename in dataset_candidates(year, month):
        url = f"{NCSS_ROOT}/{year}/{filename}"
        for attempt in range(4):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(20, 180),
                )
                if response.status_code == 404:
                    break
                response.raise_for_status()
                if len(response.content) < 1024:
                    raise RuntimeError(f"suspiciously small NCSS response ({len(response.content)} bytes)")
                dest.write_bytes(response.content)
                return dest
            except Exception as exc:  # noqa: BLE001 - retain context across retries
                errors.append(f"{filename} attempt {attempt + 1}: {exc}")
                if attempt < 3:
                    time.sleep(2 ** attempt)
        # Move to the next filename after a 404 or exhausted retries.
    raise RuntimeError(f"Unable to download {year}-{month:02d}: " + " | ".join(errors[-6:]))


def open_precip(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with xr.open_dataset(path, engine="netcdf4") as ds:
        if "prcp" not in ds:
            raise RuntimeError(f"{path.name} does not contain prcp")
        da = ds["prcp"].transpose("time", "lat", "lon")
        values = da.values.astype(np.float32, copy=False)
        lats = ds["lat"].values.astype(np.float64)
        lons = ds["lon"].values.astype(np.float64)
        dates = pd.to_datetime(ds["time"].values).strftime("%Y%m%d").astype(np.int32)
    return values, lats, lons, dates


def initialize_state(first_file: Path, cwa_geometry, end_year: int) -> dict:
    values, grid_lats, grid_lons, _ = open_precip(first_file)
    del values
    lon2d, lat2d = np.meshgrid(grid_lons, grid_lats)
    inside = contains_xy(cwa_geometry, lon2d, lat2d)
    rows, cols = np.where(inside)
    if rows.size == 0:
        raise RuntimeError("No nClimGrid points were found inside the LIX CWA")

    npoints = rows.size
    return {
        "last_year": START_YEAR - 1,
        "thresholds_in": THRESHOLDS_IN.copy(),
        "grid_lats": grid_lats,
        "grid_lons": grid_lons,
        "rows": rows.astype(np.int16),
        "cols": cols.astype(np.int16),
        "point_lats": grid_lats[rows].astype(np.float32),
        "point_lons": grid_lons[cols].astype(np.float32),
        "counts": np.zeros((3, len(THRESHOLDS_IN), 12, npoints), dtype=np.uint16),
        "max_mm": np.full((3, npoints), -np.inf, dtype=np.float32),
        "max_date": np.zeros((3, npoints), dtype=np.int32),
        "target_end_year": end_year,
    }


def load_state(end_year: int, rebuild: bool) -> dict | None:
    if rebuild or not STATE_FILE.exists():
        return None
    with np.load(STATE_FILE, allow_pickle=False) as z:
        saved_thresholds = z["thresholds_in"]
        last_year = int(z["last_year"])
        if not np.array_equal(saved_thresholds, THRESHOLDS_IN):
            print("Threshold definition changed; rebuilding from 1951.")
            return None
        if last_year > end_year:
            print("Saved state extends beyond requested end year; rebuilding.")
            return None
        return {
            "last_year": last_year,
            "thresholds_in": saved_thresholds.copy(),
            "grid_lats": z["grid_lats"].copy(),
            "grid_lons": z["grid_lons"].copy(),
            "rows": z["rows"].copy(),
            "cols": z["cols"].copy(),
            "point_lats": z["point_lats"].copy(),
            "point_lons": z["point_lons"].copy(),
            "counts": z["counts"].copy(),
            "max_mm": z["max_mm"].copy(),
            "max_date": z["max_date"].copy(),
            "target_end_year": end_year,
        }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        STATE_FILE,
        last_year=np.int32(state["last_year"]),
        thresholds_in=state["thresholds_in"],
        grid_lats=state["grid_lats"],
        grid_lons=state["grid_lons"],
        rows=state["rows"],
        cols=state["cols"],
        point_lats=state["point_lats"],
        point_lons=state["point_lons"],
        counts=state["counts"],
        max_mm=state["max_mm"],
        max_date=state["max_date"],
    )


def process_month(path: Path, year: int, month: int, state: dict) -> None:
    values, lats, lons, dates = open_precip(path)
    if values.shape[1:] != (len(state["grid_lats"]), len(state["grid_lons"])):
        raise RuntimeError(f"Grid shape changed in {path.name}: {values.shape[1:]}")
    if not np.allclose(lats, state["grid_lats"], atol=1e-6) or not np.allclose(lons, state["grid_lons"], atol=1e-6):
        raise RuntimeError(f"Grid coordinates changed in {path.name}")

    selected = values[:, state["rows"], state["cols"]]
    selected_safe = np.where(np.isfinite(selected), selected, -np.inf)

    threshold_counts = np.stack(
        [np.count_nonzero(selected >= threshold_mm, axis=0) for threshold_mm in THRESHOLDS_MM],
        axis=0,
    ).astype(np.uint16)

    max_indices = np.argmax(selected_safe, axis=0)
    point_index = np.arange(selected_safe.shape[1])
    month_max = selected_safe[max_indices, point_index]
    month_dates = dates[max_indices]

    for period_index in period_indexes_for_year(year):
        state["counts"][period_index, :, month - 1, :] += threshold_counts
        update = month_max > state["max_mm"][period_index]
        state["max_mm"][period_index, update] = month_max[update]
        state["max_date"][period_index, update] = month_dates[update]


def process_years(state: dict, bbox: tuple[float, float, float, float], start_year: int, end_year: int, workers: int) -> None:
    if start_year > end_year:
        print("Aggregation state is already current.")
        return

    jobs = [(year, month) for year in range(start_year, end_year + 1) for month in range(1, 13)]
    total = len(jobs)
    completed = 0
    print(f"Processing {total} monthly files ({start_year}–{end_year}) with {workers} workers.")

    with tempfile.TemporaryDirectory(prefix="lix-rain-") as tmp_name:
        tmp = Path(tmp_name)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(download_month, y, m, bbox, tmp / f"{y}{m:02d}.nc"): (y, m)
                for y, m in jobs
            }
            errors = []
            for future in as_completed(futures):
                year, month = futures[future]
                try:
                    path = future.result()
                    process_month(path, year, month, state)
                    path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{year}-{month:02d}: {exc}")
                completed += 1
                if completed % 24 == 0 or completed == total:
                    print(f"  {completed}/{total} months complete")
            if errors:
                raise RuntimeError("Monthly processing failures:\n" + "\n".join(errors[:20]))

    state["last_year"] = end_year


def write_period_json(state: dict, meta: dict, period_index: int) -> None:
    points = []
    counts = state["counts"][period_index]
    max_mm = state["max_mm"][period_index]
    max_date = state["max_date"][period_index]

    for i in range(len(state["point_lats"])):
        point_counts = counts[:, :, i].astype(int).tolist()
        mx = float(max_mm[i]) if np.isfinite(max_mm[i]) else None
        points.append(
            {
                "lat": round(float(state["point_lats"][i]), 5),
                "lon": round(float(state["point_lons"][i]), 5),
                "c": point_counts,
                "mx": None if mx is None else round(mx, 2),
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
    print(f"Wrote {output.relative_to(ROOT)} ({output.stat().st_size / 1_000_000:.1f} MB)")


def write_outputs(state: dict, cwa_geojson: dict, end_year: int) -> None:
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    (DOCS_DATA / "lix_cwa.geojson").write_text(
        json.dumps(cwa_geojson, separators=(",", ":")), encoding="utf-8"
    )

    periods = period_metadata(end_year)
    for idx, meta in enumerate(periods):
        write_period_json(state, meta, idx)

    manifest = {
        "status": "ready",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": "NOAA/NCEI nClimGrid-Daily v1.0.0",
        "source_url": "https://www.ncei.noaa.gov/products/land-based-station/nclimgrid-daily",
        "grid_resolution_deg": 1 / 24,
        "thresholds_in": THRESHOLDS_IN.astype(float).tolist(),
        "periods": periods,
        "cwa_file": "data/lix_cwa.geojson",
        "daily_period_note": "Each value is the 24-hour precipitation total ending in the early morning of the labeled day.",
    }
    (DOCS_DATA / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.end_year < 2001:
        raise SystemExit("end-year must be 2001 or later for the configured recent-period view")

    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    cwa_geojson = fetch_cwa()
    cwa_geometry = shape(cwa_geojson["features"][0]["geometry"])
    west, south, east, north = cwa_geometry.bounds
    # Small padding ensures all CWA-edge grid centers are returned by NCSS.
    bbox = (west - 0.10, south - 0.10, east + 0.10, north + 0.10)
    print(f"LIX CWA bbox: {bbox}")

    state = load_state(args.end_year, args.rebuild)
    if state is None:
        # One synchronous file establishes the exact NCSS grid and CWA mask.
        with tempfile.TemporaryDirectory(prefix="lix-init-") as tmp_name:
            first_path = Path(tmp_name) / f"{START_YEAR}01.nc"
            download_month(START_YEAR, 1, bbox, first_path)
            state = initialize_state(first_path, cwa_geometry, args.end_year)
            process_month(first_path, START_YEAR, 1, state)
        # Process the remaining 11 months of the first year, then all later years.
        with tempfile.TemporaryDirectory(prefix="lix-firstyear-") as tmp_name:
            tmp = Path(tmp_name)
            with ThreadPoolExecutor(max_workers=min(args.workers, 6)) as executor:
                futures = {
                    executor.submit(download_month, START_YEAR, m, bbox, tmp / f"{START_YEAR}{m:02d}.nc"): m
                    for m in range(2, 13)
                }
                for future in as_completed(futures):
                    m = futures[future]
                    path = future.result()
                    process_month(path, START_YEAR, m, state)
                    path.unlink(missing_ok=True)
        state["last_year"] = START_YEAR
        process_years(state, bbox, START_YEAR + 1, args.end_year, args.workers)
    else:
        print(f"Loaded aggregation state through {state['last_year']}.")
        process_years(state, bbox, state["last_year"] + 1, args.end_year, args.workers)

    save_state(state)
    write_outputs(state, cwa_geojson, args.end_year)
    print(f"Done. Climatology is current through {args.end_year}.")


if __name__ == "__main__":
    main()
