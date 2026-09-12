# LIX Heavy-Rain Climatology Map

Interactive climatology of heavy 24-hour precipitation across the NWS New Orleans/Baton Rouge (LIX) County Warning Area.

## What it shows

- Frequency of days with **≥1, ≥2, ≥3, ≥5, ≥8, ≥10, and ≥12 inches** of precipitation.
- Long-period (**1951–latest complete year**), **1991–2020 normals**, and **2001–latest** views.
- Annual and seasonal filters.
- Click/hover inspection of every ~5-km nClimGrid-Daily grid point.
- Spatially separated local maxima so the hot-spot list does not simply repeat adjacent pixels from one wet area.
- Maximum daily precipitation and date at each grid point for the selected climatological period.

## Data

The analysis uses NOAA/NCEI **nClimGrid-Daily v1.0.0**, a ~5-km gridded daily precipitation analysis derived from GHCN-Daily observations and available from 1951 to present. Daily precipitation values represent the 24-hour period ending in the early morning of the labeled day.

Source: https://www.ncei.noaa.gov/products/land-based-station/nclimgrid-daily

The LIX CWA boundary is retrieved from the NWS reference-map FeatureServer.

## Important interpretation note

This is a **climatological frequency analysis of an interpolated gridded dataset**, not a collection of exact point-gauge return periods. nClimGrid-Daily is particularly appropriate for spatial/temporal climate analysis, but NCEI cautions that individual grid points and individual days have uncertainty from station density, observation time, local variability, and interpolation. Treat small pixel-to-pixel differences cautiously; coherent multi-grid maxima are much more meaningful.

Also, the mapped values are fixed daily 24-hour accumulations, not all possible rolling 24-hour windows.

## Updating the data

GitHub Actions runs `scripts/build_climatology.py`. The first build processes 1951 through the latest complete calendar year; subsequent annual runs reuse a compact aggregation state and only add new complete years. A manual workflow dispatch can force a rebuild.

Generated files live in `docs/data/`; the static site in `docs/` is deployable directly with GitHub Pages.

## Local development

```bash
python -m pip install -r requirements.txt
python scripts/build_climatology.py
python -m http.server 8000 --directory docs
```

Then open `http://localhost:8000`.
