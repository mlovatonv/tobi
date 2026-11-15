# tobi

Geospatial analysis of open-pit mining haul data, visualized on a satellite map.

The project takes raw fleet-management exports (shovel GPS positions, loading/dumping cycles and mining polygons), joins them into a single geo-referenced dataset, and renders it on an interactive map built on [Traccar](https://www.traccar.org/) and its web UI (MapLibre GL).

## What it does

1. **Diagnose** the raw geo data (`data/check.py`).
2. **Merge** shovel positions with loading cycles and classify every point by mining polygon (`data/merge.py`, or `data/merge2.py` for a one-row-per-cycle CSV).
3. **Visualize** the resulting GeoJSON in a Traccar web map, served with a local satellite tile proxy (`traccar/server.py`).

## Repository layout

| Path | Description |
| --- | --- |
| [data/check.py](data/check.py) | Diagnostic script for large point datasets |
| [data/merge.py](data/merge.py) | Pipeline: time-aware join + polygon classification + GeoJSON export |
| [data/merge2.py](data/merge2.py) | Pipeline variant: one output row per loading cycle, using the center of the matched shovel coordinates (CSV only) |
| [traccar/](traccar) | Submodule: fork of Traccar ([mlovatonv/traccar](https://github.com/mlovatonv/traccar)) |
| [traccar/traccar-web/](traccar/traccar-web) | Nested submodule: fork of traccar-web ([mlovatonv/traccar-web](https://github.com/mlovatonv/traccar-web)) |
| [traccar/server.py](traccar/server.py) | Tile server / proxy for satellite imagery |

### Input data (`data/`, git-ignored because of size)

| File | Content |
| --- | --- |
| `Palas.csv` | Shovel GPS samples: equipment, fleet, UTM east/north, elevation, time, date, shift |
| `CargaDescarga.csv` | Loading/dumping cycles: shovel, truck, tonnage, material, destination, cycle start/end time, distances, etc. |
| `Poligonos.csv` | Polygon vertices (one per row) with UTM east/north/altitude |

## Data pipeline

### `data/merge.py`: join, classify, export

Joins loading cycles to the shovel coordinates that were valid at the time of each cycle, then colors each point by the polygon that contains it.

- **Time-aware join**: a shovel position matches a cycle when the equipment key matches, `PERIODO == Fecha` (`YYYYMMDD`), and `HORA` falls inside the cycle's start/end (inclusive). Overnight cycles (start > end) also accept positions from the following day up to the end time.
- **Scalable**: coordinates are indexed by `(equipment, date)` to avoid a huge many-to-many merge before time filtering; input is processed in chunks and the GeoJSON is written in batches so memory stays bounded.
- **Robust parsing**: column-name aliases and normalization, tolerant time/date parsing (`HH:MM`, `HH:MM:SS`, ISO timestamps; `YYYYMMDD` or `YYYY-MM-DD`), explicit handling of column collisions, and header validation before any output is touched.
- **Coordinate handling**: spatial tests run in UTM (`EPSG:32719`, configurable) with Shapely prepared polygons; output is reprojected with `pyproj` to WGS84 `[longitude, latitude]`, as GeoJSON/MapLibre require.
- **Polygon classification**: each point gets the first containing polygon (boundaries included) or `Outside`, with a deterministic, distinct color per polygon.
- **Two phases**: the joined CSV (`PalasCargaDescarga.csv`) is completed first, then the GeoJSON (`PalasCargaDescarga.json`) is produced. A log goes to `PalasCargaDescarga.log`.

```bash
cd data
python merge.py                                              # all dates
python merge.py --start-date 20260101 --end-date 20260131    # inclusive, on cycle date (Fecha)
python merge.py --start-date 20260115                        # from that date onward
```

### `data/merge2.py`: one row per cycle

A leaner variant of the join that outputs only `PalasCargaDescarga.csv` (no GeoJSON), with exactly one row per row of `CargaDescarga.csv` (or per cycle inside the date range when a filter is given).

- **Same matching rule** as `merge.py` (equipment key, `PERIODO == Fecha`, `HORA` inside the cycle, overnight cycles supported).
- **Center of matched coordinates**: when several shovel positions match a cycle, `east_avg`/`north_avg` are the mean of their UTM coordinates and `latitude_avg`/`longitude_avg` are that center reprojected to WGS84. The original Palas fields (`EQUIPO`, `HORA`, `ESTE`, `NORTE`, `latitude`, `longitude`, ...) come from the match closest to the cycle start. `palas_matches` holds the number of matches.
- **Polygon of the center**: `polygon` is the first polygon (boundaries included) containing the center, or `NA` when it is outside all polygons or there are no coordinates. `polygon_color` is a deterministic HEX color per polygon (`#9E9E9E` for `NA`).
- **Unmatched cycles are kept**: unknown equipment, invalid cycle time/date, or no `HORA` inside the cycle leave the Palas fields blank with `polygon = NA`.
- Takes the same `--start-date` / `--end-date` options and writes the log to `PalasCargaDescarga.log`.

```bash
python merge2.py                                             # all dates
python merge2.py --start-date 20260101 --end-date 20260131
```

### `data/check.py`: map point diagnostics

Built to answer "why does the map show so few points?" for a large dataset. It reads the CSV in chunks and reports:

- valid vs. missing coordinates,
- exact duplicate coordinate pairs,
- coordinate uniqueness at a configurable decimal precision (reveals stacked or near-identical locations),
- geographic bounds,
- optionally, one row per rounded location with its record count.

It also documents a common pitfall: UTM `ESTE/NORTE` values must be projected to WGS84 before being used as GeoJSON longitude/latitude.

```bash
python check.py <file>.csv --chunk-size 10000
python check.py <file>.csv --round-decimals 6 --sample-output unique_locations.csv
```

## Visualization (Traccar forks)

- [mlovatonv/traccar](https://github.com/mlovatonv/traccar): fork of Traccar, synced with upstream. My additions: the tile server (`server.py`), `.gitignore` changes, and pointing the `traccar-web` submodule to my own fork.
- [mlovatonv/traccar-web](https://github.com/mlovatonv/traccar-web): fork of the Traccar web UI (React, Vite, MapLibre GL). The generated `PalasCargaDescarga.json` is placed in `src/map/core/` (git-ignored) to feed the map with the mining points, colored by polygon.
- `traccar/server.py`: serves map tiles on a local port (default `8000`) with CORS enabled. Zoom ≥ 15 tiles come from the local `tiles/` folder when present; everything else falls back to Google satellite imagery, streamed without caching to disk.

## Setup

```bash
git clone --recurse-submodules git@github.com:mlovatonv/tobi.git
cd tobi/data
python -m venv .venv && source .venv/bin/activate
pip install pandas pyproj shapely

# Put Palas.csv, CargaDescarga.csv and Poligonos.csv in data/, then:
python merge.py

# Feed the web app and start everything
cp PalasCargaDescarga.json ../traccar/traccar-web/src/map/core/
python ../traccar/server.py 8000                 # tile server
cd ../traccar/traccar-web && npm install && npm start
```

## Tech stack

Python (pandas, pyproj, Shapely), GeoJSON, UTM/WGS84 projections, MapLibre GL, React/Vite, Traccar (Java).
