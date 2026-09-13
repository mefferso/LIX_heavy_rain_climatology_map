# LIX Heavy-Rain Climatology Map

Interactive climatology of heavy daily precipitation across the NWS New Orleans/Baton Rouge (LIX) County Warning Area.

## What it shows

- Frequency of days with **≥1, ≥2, ≥3, ≥5, ≥8, ≥10, and ≥12 inches** of precipitation.
- Full-record climatology plus inclusive era slices: **2005–2025, 1985–2005, and 1965–1985**.
- Broader **1951–1990** and **1991–2025** periods for climate-era comparisons.
- Change maps showing newer-period minus older-period exceedance frequency, including:
  - 1991–2025 minus 1951–1990
  - 2005–2025 minus 1985–2005
  - 1985–2005 minus 1965–1985
  - 2005–2025 minus 1965–1985
- Annual and seasonal filters.
- Hover inspection of every ~5-km nClimGrid-Daily grid point.
- Maximum daily precipitation and date at each grid point for single-period views.

The requested era labels are inclusive, so 2005 is present in both the 2005–2025 and 1985–2005 slices, and 1985 is present in both 1985–2005 and 1965–1985.

## Data

The analysis uses NOAA/NCEI **nClimGrid-Daily v1.0.0**, a ~5-km gridded daily precipitation analysis derived from GHCN-Daily observations and available from 1951 to present. Data are read from NOAA's public Open Data Dissemination (NODD) S3 copy using HTTP byte-range requests.

Source: https://registry.opendata.aws/noaa-nclimgrid-daily/

The LIX CWA boundary is retrieved from the NWS reference-map FeatureServer.

## Important interpretation note

This is a **climatological frequency analysis of an interpolated gridded dataset**, not a collection of exact point-gauge return periods. nClimGrid-Daily is useful for spatial/temporal climate analysis, but individual grid points and individual days have uncertainty from station density, observation time, local variability, and interpolation. Treat small pixel-to-pixel differences cautiously; coherent multi-grid patterns are much more meaningful.

Mapped precipitation values are fixed daily 24-hour accumulations, not all possible rolling 24-hour windows.

Comparison maps use **absolute change in exceedance days per year** (newer period minus older period). Hover tooltips also show percent change when the older-period frequency is nonzero.

## Updating the data

GitHub Actions runs `scripts/build_climatology.py`. The saved state stores **year-by-year threshold counts and annual maxima**, allowing new climatological periods and comparisons to be generated from the existing state without rereading the entire archive. Once the yearly state exists, routine annual updates only add new complete years.

Generated files live in `docs/data/`; the static site in `docs/` is deployed with GitHub Pages.

## Local development

```bash
python -m pip install -r requirements.txt
python scripts/build_climatology.py
python -m http.server 8000 --directory docs
```

Then open `http://localhost:8000`.
