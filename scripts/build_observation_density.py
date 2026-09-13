#!/usr/bin/env python3
"""Build nClimGrid-Daily precipitation observation-density support layers.

The companion nClimGrid-Daily-Auxiliary product provides, for every day and
native grid point, the number of precipitation observations within 30 miles
that were used as input to nClimGrid-Daily. NCEI publishes these counts as an
indicator of observation density, one factor affecting uncertainty in the
interpolated grid-point estimates.

This script downloads only the LIX-area subset through NCEI's THREDDS NetCDF
Subset Service, aggregates daily precipitation-observation counts by calendar
month and year, persists resumable state, and writes compact period JSON files
for the static GitHub Pages map.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
DOCS_DATA = ROOT / "docs" / "data"
STATE_DIR = ROOT / "data"
STATE_FILE = STATE_DIR / "observation_density_state.npz"
CLIMO_STATE_FILE = STATE_DIR / "climatology_state.npz"
START_YEAR = 1951
STATE_SCHEMA_VERSION = 1
USER_AGENT = "LIX-heavy-rain-climatology/1.0 (NOAA climate analysis; GitHub Pages)"
AUX_NCSS_ROOT = "https://www.ncei.noaa.gov/thredds/ncss/grid/nclimgrid-daily-auxiliary"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--end-year",
        type=int,
        default=datetime.now(timezone.utc).year - 1,
        help="Latest complete calendar year to include (default: previous UTC year).",
    )
    parser.add_argument("--rebuild", action="store_true", help="Ignore saved observation-density state.")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("NCLIMGRID_AUX_WORKERS", "12")),
        help="Concurrent monthly NCEI auxiliary subset requests.",
    )
    return parser.parse_args()


def load_grid_points() -> tuple[np.ndarray, np.ndarray]:
    if not CLIMO_STATE_FILE.exists():
        raise RuntimeError("data/climatology_state.npz is required; run build_climatology.py first")
    with np.load(CLIMO_STATE_FILE, allow_pickle=False) as z:
        return z["point_lats"].astype(np.float64), z["point_lons"].astype(np.float64)


def initialize_state(point_lats: np.ndarray, point_lons: np.ndarray, end_year: int) -> dict:
    nyears = end_year - START_YEAR + 1
    npoints = len(point_lats)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_year": START_YEAR - 1,
        "point_lats": point_lats.astype(np.float32),
        "point_lons": point_lons.astype(np.float32),
        "yearly_sum": np.zeros((nyears, 12, npoints), dtype=np.float32),
        "yearly_days": np.zeros((nyears, 12, npoints), dtype=np.uint16),
    }


def expand_state_to_year(state: dict, end_year: int) -> None:
    wanted = end_year - START_YEAR + 1
    current = state["yearly_sum"].shape[0]
    if wanted <= current:
        return
    add = wanted - current
    npoints = len(state["point_lats"])
    state["yearly_sum"] = np.concatenate(
        [state["yearly_sum"], np.zeros((add, 12, npoints), dtype=np.float32)], axis=0
    )
    state["yearly_days"] = np.concatenate(
        [state["yearly_days"], np.zeros((add, 12, npoints), dtype=np.uint16)], axis=0
    )


def load_state(point_lats: np.ndarray, point_lons: np.ndarray, end_year: int, rebuild: bool) -> dict | None:
    if rebuild or not STATE_FILE.exists():
        return None
    with np.load(STATE_FILE, allow_pickle=False) as z:
        if "schema_version" not in z or int(z["schema_version"]) != STATE_SCHEMA_VERSION:
            return None
        if not np.allclose(z["point_lats"], point_lats, atol=1e-5) or not np.allclose(
            z["point_lons"], point_lons, atol=1e-5
        ):
            print("Grid-point set changed; rebuilding observation-density state.", flush=True)
            return None
        last_year = int(z["last_year"])
        if last_year > end_year:
            return None
        state = {
            "schema_version": int(z["schema_version"]),
            "last_year": last_year,
            "point_lats": z["point_lats"].copy(),
            "point_lons": z["point_lons"].copy(),
            "yearly_sum": z["yearly_sum"].copy(),
            "yearly_days": z["yearly_days"].copy(),
        }
    expand_state_to_year(state, end_year)
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        STATE_FILE,
        schema_version=np.int16(STATE_SCHEMA_VERSION),
        last_year=np.int32(state["last_year"]),
        point_lats=state["point_lats"],
        point_lons=state["point_lons"],
        yearly_sum=state["yearly_sum"],
        yearly_days=state["yearly_days"],
    )


def month_url(year: int, month: int) -> str:
    filename = f"ncddsupp-{year}{month:02d}-obcounts.nc"
    return f"{AUX_NCSS_ROOT}/{year}/{filename}"


def fetch_month(
    year: int,
    month: int,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fetch all days in one monthly auxiliary file for only the LIX-area grid.

    Each ncddsupp file already contains exactly one calendar month. Asking NCSS
    for an explicit start/end range caused HTTP 400 responses on NCEI's TDS for
    these files, so request the file's complete time axis with ``time=all``.
    """
    west, south, east, north = bbox
    params = {
        "var": "cntp",
        "north": f"{north:.5f}",
        "south": f"{south:.5f}",
        "west": f"{west:.5f}",
        "east": f"{east:.5f}",
        "horizStride": "1",
        "time": "all",
        "addLatLon": "true",
        "accept": "netCDF4",
    }
    errors: list[str] = []
    for attempt in range(1, 5):
        try:
            response = requests.get(
                month_url(year, month),
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=120,
            )
            if not response.ok:
                detail = response.text[:500].replace("\n", " ").strip()
                raise RuntimeError(f"HTTP {response.status_code}: {detail or response.reason}")
            with xr.open_dataset(io.BytesIO(response.content), engine="h5netcdf", decode_times=False) as ds:
                if "cntp" not in ds:
                    raise RuntimeError("auxiliary subset does not contain cntp")
                da = ds["cntp"].transpose("time", "lat", "lon").load()
                values = np.asarray(da.values, dtype=np.float32)
                lats = np.asarray(da["lat"].values, dtype=np.float64)
                lons = np.asarray(da["lon"].values, dtype=np.float64)
            return values, lats, lons
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < 4:
                time.sleep(min(12, 2 ** attempt))
    raise RuntimeError(f"Unable to read auxiliary counts for {year}-{month:02d}: {' | '.join(errors)}")


def map_grid_points(
    aux_lats: np.ndarray,
    aux_lons: np.ndarray,
    point_lats: np.ndarray,
    point_lons: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    lat_lookup = {round(float(v), 5): i for i, v in enumerate(aux_lats)}
    lon_lookup = {round(float(v), 5): i for i, v in enumerate(aux_lons)}
    try:
        rows = np.array([lat_lookup[round(float(v), 5)] for v in point_lats], dtype=np.int32)
        cols = np.array([lon_lookup[round(float(v), 5)] for v in point_lons], dtype=np.int32)
    except KeyError as exc:
        raise RuntimeError(f"Auxiliary grid does not align with climatology grid at {exc}") from exc
    return rows, cols


def process_month_data(data: tuple[np.ndarray, np.ndarray, np.ndarray], year: int, month: int, state: dict) -> None:
    values, lats, lons = data
    rows, cols = map_grid_points(lats, lons, state["point_lats"], state["point_lons"])
    selected = values[:, rows, cols]
    valid = np.isfinite(selected) & (selected >= 0)
    selected = np.where(valid, selected, 0.0)
    year_index = year - START_YEAR
    state["yearly_sum"][year_index, month - 1, :] = selected.sum(axis=0, dtype=np.float64).astype(np.float32)
    state["yearly_days"][year_index, month - 1, :] = valid.sum(axis=0).astype(np.uint16)


def process_year(state: dict, year: int, bbox: tuple[float, float, float, float], workers: int) -> None:
    failures: list[str] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_month, year, month, bbox): month for month in range(1, 13)}
        for future in as_completed(futures):
            month = futures[future]
            try:
                process_month_data(future.result(), year, month, state)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{year}-{month:02d}: {exc}")
            completed += 1
            if completed % 3 == 0 or completed == 12:
                print(f"  {year}: {completed}/12 auxiliary months complete", flush=True)
    if failures:
        raise RuntimeError("Auxiliary count fetch failures:\n" + "\n".join(failures))


def load_periods() -> list[dict]:
    manifest_file = DOCS_DATA / "manifest.json"
    if not manifest_file.exists():
        raise RuntimeError("docs/data/manifest.json is required; run build_climatology.py first")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    return manifest["periods"]


def write_period_json(state: dict, meta: dict) -> None:
    start_idx = meta["start_year"] - START_YEAR
    end_idx = meta["end_year"] - START_YEAR + 1
    sums = state["yearly_sum"][start_idx:end_idx].sum(axis=0, dtype=np.float64)
    days = state["yearly_days"][start_idx:end_idx].sum(axis=0, dtype=np.uint32)
    points = []
    for i in range(len(state["point_lats"])):
        points.append(
            {
                "lat": round(float(state["point_lats"][i]), 5),
                "lon": round(float(state["point_lons"][i]), 5),
                "os": [round(float(v), 2) for v in sums[:, i]],
                "on": days[:, i].astype(int).tolist(),
            }
        )
    payload = {
        "period": {k: meta[k] for k in ("id", "label", "start_year", "end_year", "years")},
        "metric": "mean daily count of nClimGrid-Daily precipitation input observations within 30 miles",
        "source": "NOAA/NCEI nClimGrid-Daily-Auxiliary v1.0.0",
        "points": points,
    }
    output = DOCS_DATA / f"observation_density_{meta['id']}.json"
    output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)} ({output.stat().st_size / 1_000_000:.2f} MB)", flush=True)


def update_manifest(periods: list[dict]) -> None:
    manifest_file = DOCS_DATA / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["observation_density"] = {
        "available": True,
        "source": "NOAA/NCEI nClimGrid-Daily-Auxiliary v1.0.0",
        "source_url": "https://www.ncei.noaa.gov/products/land-based-station/nclimgrid-daily",
        "description": "Mean daily number of precipitation input observations within 30 miles of each grid point.",
        "files": {p["id"]: f"data/observation_density_{p['id']}.json" for p in periods},
        "interpretation": (
            "Higher counts indicate denser nearby observation support and typically lower uncertainty from station sparsity, "
            "but this is not a formal confidence score; local gradients, observation timing, topography, and interpolation also matter."
        ),
    }
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    point_lats, point_lons = load_grid_points()
    bbox = (
        float(point_lons.min()) - 0.06,
        float(point_lats.min()) - 0.06,
        float(point_lons.max()) + 0.06,
        float(point_lats.max()) + 0.06,
    )
    state = load_state(point_lats, point_lons, args.end_year, args.rebuild)
    if state is None:
        state = initialize_state(point_lats, point_lons, args.end_year)
        first_year = START_YEAR
    else:
        print(f"Loaded observation-density state through {state['last_year']}.", flush=True)
        first_year = state["last_year"] + 1

    for year in range(first_year, args.end_year + 1):
        print(f"Processing auxiliary observation counts for {year}…", flush=True)
        process_year(state, year, bbox, args.workers)
        state["last_year"] = year
        save_state(state)

    periods = load_periods()
    keep = {f"observation_density_{p['id']}.json" for p in periods}
    for old_file in DOCS_DATA.glob("observation_density_*.json"):
        if old_file.name not in keep:
            old_file.unlink()
    for meta in periods:
        write_period_json(state, meta)
    update_manifest(periods)
    save_state(state)
    print(f"Observation-density layers current through {args.end_year}.", flush=True)


if __name__ == "__main__":
    main()
