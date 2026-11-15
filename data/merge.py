#!/usr/bin/env python3
"""Join loading cycles to time-valid shovel coordinates, then color points by polygon.

Expected files in the working directory:
  - Palas.csv: EQUIPO, ESTE, NORTE, HORA, PERIODO (plus optional attributes)
  - CargaDescarga.csv: Pala, Fecha, Hora Inicio Ciclo, Hora Final Ciclo (plus attributes)
  - Poligonos.csv: one vertex per row, polygon identifier plus East/North coordinates

Polygon vertices are grouped by polygon identifier and kept in input order unless a
vertex-order column is available. Spatial tests use UTM coordinates; GeoJSON output
uses WGS84 [longitude, latitude].

Usage:
  python palas_join.py                                        # all dates
  python palas_join.py --start-date 20260101 --end-date 20260131
  python palas_join.py --start-date 20260115                  # from that date onward
  python palas_join.py --end-date 20260115                    # up to and including that date

Dates are YYYYMMDD (YYYY-MM-DD also accepted) and both bounds are inclusive. The filter is
applied to the cycle date (Fecha).
"""

import argparse
import colorsys
import json
import logging
import os
import sys
import traceback
from collections import defaultdict
from datetime import datetime, time, timedelta

import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

# --- CONFIGURATION ---
UTM_EPSG = "EPSG:32719"  # EPSG:32718 = WGS 84 / UTM zone 18S, EPSG:32719 = zone 19S
PALAS_FILE = "Palas.csv"
CARGA_FILE = "CargaDescarga.csv"
POLIGONOS_FILE = "Poligonos.csv"
OUTPUT_CSV = "PalasCargaDescarga.csv"
OUTPUT_GEOJSON = "PalasCargaDescarga.json"
LOG_FILE = "PalasCargaDescarga.log"
CHUNK_SIZE = 100
GEOJSON_BATCH_SIZE = 1000

# Match requires: equipment key AND PERIODO == Fecha (calendar date, format YYYYMMDD) AND
# HORA within the cycle's start/end (inclusive). Overnight cycles (start > end) also accept
# HORA <= end on the day after Fecha.
# Cycle start/end are compared as time-of-day only; the date comes from Fecha.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")],
)
log = logging.getLogger("palas_join")

try:
    transformer = Transformer.from_crs(UTM_EPSG, "EPSG:4326", always_xy=True)
except Exception as exc:
    raise RuntimeError(f"Could not initialize coordinate transformer for {UTM_EPSG}: {exc}") from exc


def transform_equipo(value):
    """Normalize Palas EQUIPO to match CargaDescarga Pala."""
    equipo = "" if pd.isna(value) else str(value).strip()
    if equipo.startswith(("PATQ", "CFTQ")):
        return "S" + equipo[-2:]
    if equipo == "EXTQ005":
        return "PC7001"
    return equipo


def normalize_header(value):
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def find_column(df, aliases, description, required=True):
    normalized = {normalize_header(col): col for col in df.columns}
    for alias in aliases:
        found = normalized.get(normalize_header(alias))
        if found is not None:
            return found
    if required:
        raise ValueError(
            f"Could not identify {description}. Tried column names: {', '.join(aliases)}. "
            f"Available columns: {', '.join(map(str, df.columns))}"
        )
    return None


def parse_clock(value):
    """Return seconds since midnight, or None for blank/unparseable values."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    # Prefer pandas parsing, which accepts HH:MM, HH:MM:SS and datetime strings.
    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if not pd.isna(parsed):
            return parsed.hour * 3600 + parsed.minute * 60 + parsed.second + parsed.microsecond / 1e6
    except (TypeError, ValueError, OverflowError):
        pass
    # Fallback for ISO timestamps with unusual formats.
    try:
        parsed_time = time.fromisoformat(text)
        return parsed_time.hour * 3600 + parsed_time.minute * 60 + parsed_time.second + parsed_time.microsecond / 1e6
    except (TypeError, ValueError):
        return None


def time_in_interval(clock, start, end):
    """Inclusive clock-time interval; supports intervals crossing midnight."""
    if clock is None or start is None or end is None:
        return False
    if start <= end:
        return start <= clock <= end
    return clock >= start or clock <= end


def parse_date(value):
    """Return a datetime.date, or None. Expected format is YYYYMMDD (e.g. 20260101)."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0"):  # in case a number was saved as a float
        text = text[:-2]
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        # Fallback for any stray ISO-style value such as 2026-01-01 or 2026-01-01 08:00:00.
        parsed = pd.to_datetime(text, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    except (TypeError, ValueError, OverflowError):
        return None


def cli_date(value):
    """argparse type: accept YYYYMMDD or YYYY-MM-DD and return a datetime.date."""
    parsed = parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid date; use YYYYMMDD (e.g. 20260101)")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Join loading cycles to shovel coordinates and color points by polygon."
    )
    parser.add_argument("--start-date", type=cli_date, default=None, metavar="YYYYMMDD",
                        help="first cycle date (Fecha) to include, inclusive")
    parser.add_argument("--end-date", type=cli_date, default=None, metavar="YYYYMMDD",
                        help="last cycle date (Fecha) to include, inclusive")
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error(f"--start-date ({args.start_date}) is after --end-date ({args.end_date})")
    return args


def find_matches(by_key, key, day, start, end):
    """Palas records for this equipment/date whose HORA falls inside the cycle."""
    if start <= end:
        return [c for c in by_key.get((key, day), [])
                if time_in_interval(c["__palas_clock"], start, end)]
    # Overnight cycle: late part on Fecha, early part on the following day.
    first = [c for c in by_key.get((key, day), [])
             if pd.notna(c["__palas_clock"]) and c["__palas_clock"] >= start]
    second = [c for c in by_key.get((key, day + timedelta(days=1)), [])
              if pd.notna(c["__palas_clock"]) and c["__palas_clock"] <= end]
    return first + second


def to_number(value):
    try:
        if pd.isna(value) or str(value).strip() == "":
            return None
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def make_polygon_color(index):
    """Generate reasonably distinct deterministic CSS hex colors."""
    hue = (index * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.72, 0.92)
    return "#{:02x}{:02x}{:02x}".format(round(red * 255), round(green * 255), round(blue * 255))


def load_polygons(path):
    log.info("Phase 2A: reading polygon vertices from %s", path)
    polygons_df = pd.read_csv(path, dtype=str, low_memory=False)
    if polygons_df.empty:
        raise ValueError(f"{path} contains no polygon vertices.")

    polygon_col = find_column(
        polygons_df,
        ["Poligono", "Polígono", "Polygon", "Polygon Name", "Nombre Poligono", "Nombre de Poligono", "Nombre de Polígono", "Polygon ID", "Nombre", "Name", "ID", "Id"],
        "polygon identifier column",
    )
    east_col = find_column(polygons_df, ["ESTE", "EAST", "East", "Easting", "X", "UTM Easting"], "polygon East/Easting coordinate column")
    north_col = find_column(polygons_df, ["NORTE", "NORTH", "North", "Northing", "Y", "UTM Northing"], "polygon North/Northing coordinate column")
    order_col = find_column(
        polygons_df,
        ["Orden", "Order", "Vertex Order", "Punto", "Point Order", "Secuencia", "Sequence", "Vertice", "Vértice"],
        "polygon vertex order column",
        required=False,
    )

    polygons_df["__polygon_name"] = polygons_df[polygon_col].fillna("").astype(str).str.strip()
    polygons_df["__east"] = pd.to_numeric(polygons_df[east_col], errors="coerce")
    polygons_df["__north"] = pd.to_numeric(polygons_df[north_col], errors="coerce")
    polygons_df["__source_row"] = range(len(polygons_df))
    if order_col:
        polygons_df["__vertex_order"] = pd.to_numeric(polygons_df[order_col], errors="coerce")
        polygons_df.sort_values(["__polygon_name", "__vertex_order", "__source_row"], inplace=True, na_position="last")
        log.info("Using '%s' to order polygon vertices where available", order_col)
    else:
        log.info("No vertex-order column found; using the order of rows in %s", path)

    valid_rows = polygons_df["__polygon_name"].ne("") & polygons_df["__east"].notna() & polygons_df["__north"].notna()
    invalid_vertex_rows = int((~valid_rows).sum())
    if invalid_vertex_rows:
        log.warning("Ignoring %s polygon rows with missing polygon names or invalid East/North values", f"{invalid_vertex_rows:,}")
    polygons_df = polygons_df.loc[valid_rows]

    groups = []
    for name, group in polygons_df.groupby("__polygon_name", sort=False):
        coords = list(zip(group["__east"].astype(float), group["__north"].astype(float)))
        # A repeated final vertex is fine; Polygon closes rings automatically.
        if len(coords) < 3:
            log.warning("Skipping polygon '%s': only %d valid vertices (need at least 3)", name, len(coords))
            continue
        try:
            polygon = Polygon(coords)
            if not polygon.is_valid:
                log.warning("Polygon '%s' is invalid/self-intersecting; attempting make_valid via buffer(0)", name)
                polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon"):
                log.warning("Skipping polygon '%s': geometry is empty or not polygonal", name)
                continue
            groups.append({"name": str(name), "geometry": polygon, "prepared": prep(polygon)})
            log.info("Loaded polygon '%s' with %d vertices", name, len(coords))
        except Exception:
            log.exception("Could not build polygon '%s'; skipping it", name)

    if not groups:
        raise ValueError(
            f"No usable polygons were built from {path}. Check the polygon ID, East/North columns, "
            "and that each polygon has at least three valid vertices."
        )

    # Add a stable color mapping in file order.
    for i, item in enumerate(groups):
        item["color"] = make_polygon_color(i)
    log.info("Usable polygons loaded: %d", len(groups))
    return groups


def assign_polygon(east, north, polygons):
    """Return (name, color) for the first containing polygon, else ('Outside', neutral)."""
    if east is None or north is None:
        return "No valid coordinates", "#808080"
    point = Point(east, north)
    for item in polygons:
        # covers() includes polygon boundaries, unlike contains().
        try:
            if item["prepared"].covers(point):
                return item["name"], item["color"]
        except Exception:
            # Fallback for Shapely versions where PreparedGeometry lacks covers().
            if item["geometry"].covers(point):
                return item["name"], item["color"]
    return "Outside all polygons", "#808080"


def json_safe(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main(start_date=None, end_date=None):
    started = datetime.now()
    for filename in (PALAS_FILE, CARGA_FILE, POLIGONOS_FILE):
        if not os.path.isfile(filename):
            raise FileNotFoundError(f"Required input file not found: {filename}")

    date_filter_active = start_date is not None or end_date is not None
    log.info("Starting workflow; UTM CRS=%s; chunk size=%s", UTM_EPSG, CHUNK_SIZE)
    log.info(
        "Date filter (inclusive, on Fecha): start=%s, end=%s",
        start_date or "none", end_date or "none",
    )
    log.info("Output CSV will be completed before GeoJSON creation begins")

    # ---------- Phase 1: build the joined CSV only ----------
    log.info("Phase 1A: loading %s", PALAS_FILE)
    palas = pd.read_csv(PALAS_FILE, dtype=str, low_memory=False)
    required_palas = {"EQUIPO", "ESTE", "NORTE", "HORA", "PERIODO"}
    missing = required_palas - set(palas.columns)
    if missing:
        raise ValueError(f"{PALAS_FILE} is missing required columns: {', '.join(sorted(missing))}")

    palas["match_key"] = palas["EQUIPO"].apply(transform_equipo)
    palas["__palas_clock"] = palas["HORA"].apply(parse_clock)
    palas["__palas_date"] = palas["PERIODO"].apply(parse_date)
    log.info("Palas rows with unparseable PERIODO: %s", f"{int(palas['__palas_date'].isna().sum()):,}")
    if date_filter_active:
        # Keep one extra day after end_date so overnight cycles starting on end_date still match.
        keep = palas["__palas_date"].notna()
        if start_date is not None:
            keep &= palas["__palas_date"].apply(lambda d: d is not None and not pd.isna(d) and d >= start_date)
        if end_date is not None:
            last_palas_day = end_date + timedelta(days=1)
            keep &= palas["__palas_date"].apply(lambda d: d is not None and not pd.isna(d) and d <= last_palas_day)
        dropped = int((~keep).sum())
        palas = palas.loc[keep].copy()
        log.info("Palas date filter: kept %s rows, dropped %s", f"{len(palas):,}", f"{dropped:,}")
    palas["__east_num"] = pd.to_numeric(palas["ESTE"], errors="coerce")
    palas["__north_num"] = pd.to_numeric(palas["NORTE"], errors="coerce")
    valid_utm = palas["__east_num"].notna() & palas["__north_num"].notna()
    palas["latitude"] = float("nan")
    palas["longitude"] = float("nan")
    if valid_utm.any():
        try:
            lon, lat = transformer.transform(
                palas.loc[valid_utm, "__east_num"].to_numpy(),
                palas.loc[valid_utm, "__north_num"].to_numpy(),
            )
            palas.loc[valid_utm, "latitude"] = lat
            palas.loc[valid_utm, "longitude"] = lon
        except Exception as exc:
            raise RuntimeError(f"Failed converting Palas.csv UTM coordinates using {UTM_EPSG}: {exc}") from exc

    log.info("Palas rows: %s", f"{len(palas):,}")
    log.info("Distinct normalized equipment keys: %s", f"{palas['match_key'].nunique():,}")
    log.info("Palas rows with valid numeric UTM coordinates: %s", f"{int(valid_utm.sum()):,}")
    log.info("Palas rows with unparseable HORA: %s", f"{int(palas['__palas_clock'].isna().sum()):,}")

    # Preserve all rows and attributes except helper columns; avoid collisions with loading fields.
    carga_header = pd.read_csv(CARGA_FILE, nrows=0).columns
    overlaps = (set(palas.columns) & set(carga_header)) - {"latitude", "longitude"}
    lookup = palas.drop(columns=list(overlaps), errors="ignore").copy()
    # Keep the requested equipment key and helper columns explicitly.
    lookup["match_key"] = palas["match_key"]
    lookup["__palas_clock"] = palas["__palas_clock"]
    lookup["__palas_date"] = palas["__palas_date"]
    lookup["__east_num"] = palas["__east_num"]
    lookup["__north_num"] = palas["__north_num"]
    lookup["latitude"] = palas["latitude"]
    lookup["longitude"] = palas["longitude"]

    # Index coordinate records by (equipment, date) to avoid a huge many-to-many merge
    # before time filtering.
    by_key = {}
    known_keys = set()
    for (key, day), group in lookup.groupby(["match_key", "__palas_date"], sort=False, dropna=True):
        by_key[(str(key), day)] = group.to_dict(orient="records")
        known_keys.add(str(key))
    log.info("Prepared lookup for %s equipment/date combinations", f"{len(by_key):,}")

    for col in ("Pala", "Fecha", "Hora Inicio Ciclo", "Hora Final Ciclo"):
        # Header validation is performed before output is touched.
        if col not in carga_header:
            raise ValueError(f"{CARGA_FILE} is missing required column '{col}'")

    if os.path.exists(OUTPUT_CSV):
        os.remove(OUTPUT_CSV)
        log.info("Removed previous output CSV: %s", OUTPUT_CSV)
    if os.path.exists(OUTPUT_GEOJSON):
        log.info("Existing GeoJSON will not be touched until the CSV phase has completed")

    total_input = total_output = unmatched_equipment = no_time_match = invalid_cycle_time = 0
    outside_date_range = 0
    first_csv = True
    chunk_count = 0
    log.info("Phase 1B: processing %s in chunks of %d; GeoJSON is NOT being created yet", CARGA_FILE, CHUNK_SIZE)

    try:
        reader = pd.read_csv(CARGA_FILE, chunksize=CHUNK_SIZE, dtype=str, low_memory=False)
        for chunk_count, chunk in enumerate(reader, start=1):
            total_input += len(chunk)
            chunk["Pala"] = chunk["Pala"].fillna("").astype(str).str.strip()
            chunk["__cycle_start"] = chunk["Hora Inicio Ciclo"].apply(parse_clock)
            chunk["__cycle_end"] = chunk["Hora Final Ciclo"].apply(parse_clock)
            chunk["__cycle_date"] = chunk["Fecha"].apply(parse_date)
            out_records = []

            for record in chunk.to_dict(orient="records"):
                if date_filter_active:
                    cycle_day = record.get("__cycle_date")
                    if (
                        cycle_day is None
                        or pd.isna(cycle_day)
                        or (start_date is not None and cycle_day < start_date)
                        or (end_date is not None and cycle_day > end_date)
                    ):
                        outside_date_range += 1
                        continue

                key = record.get("Pala", "")
                if key not in known_keys:
                    unmatched_equipment += 1
                    continue

                start = record.get("__cycle_start")
                end = record.get("__cycle_end")
                day = record.get("__cycle_date")
                if pd.isna(start) or pd.isna(end) or day is None or pd.isna(day):
                    invalid_cycle_time += 1
                    continue

                matches = find_matches(by_key, key, day, start, end)
                for candidate in matches:
                    merged = dict(record)
                    for col, value in candidate.items():
                        if col in {"match_key", "__palas_clock", "__palas_date", "__east_num", "__north_num", "latitude", "longitude"}:
                            continue
                        # Palas attribute collision should be explicit rather than overwrite cycle data.
                        out_col = col if col not in merged else f"{col}_palas"
                        merged[out_col] = value
                    merged["match_key"] = key
                    merged["HORA"] = candidate.get("HORA")
                    merged["ESTE"] = candidate.get("ESTE")
                    merged["NORTE"] = candidate.get("NORTE")
                    merged["latitude"] = candidate.get("latitude")
                    merged["longitude"] = candidate.get("longitude")
                    out_records.append(merged)
                if not matches:
                    no_time_match += 1

            if out_records:
                output_chunk = pd.DataFrame.from_records(out_records)
                output_chunk.drop(columns=["__cycle_start", "__cycle_end", "__cycle_date", "__palas_clock"], inplace=True, errors="ignore")
                # Keep coordinate helper columns in CSV for polygon membership and auditability.
                output_chunk.to_csv(OUTPUT_CSV, mode="a", index=False, header=first_csv)
                first_csv = False
                total_output += len(output_chunk)
            log.info(
                "CSV chunk %d complete: input=%s, joined-so-far=%s, unmatched-equipment=%s, no-time-match=%s",
                chunk_count, f"{total_input:,}", f"{total_output:,}", f"{unmatched_equipment:,}", f"{no_time_match:,}"
            )

        if first_csv:
            # Create an empty CSV with a useful header even if there were no matches.
            base_columns = list(pd.read_csv(CARGA_FILE, nrows=0).columns) + ["match_key", "HORA", "ESTE", "NORTE", "latitude", "longitude"]
            pd.DataFrame(columns=list(dict.fromkeys(base_columns))).to_csv(OUTPUT_CSV, index=False)
        log.info("Phase 1 complete: %s written with %s joined rows", OUTPUT_CSV, f"{total_output:,}")
        if date_filter_active:
            log.info("Rows skipped by date filter (outside range or invalid Fecha): %s", f"{outside_date_range:,}")
        log.info("Rows without equipment match: %s", f"{unmatched_equipment:,}")
        log.info("Rows with invalid cycle start/end time or Fecha: %s", f"{invalid_cycle_time:,}")
        log.info("Rows with equipment match but no HORA inside cycle date/interval: %s", f"{no_time_match:,}")

    except Exception as exc:
        log.exception("CSV phase failed after processing %s input rows", f"{total_input:,}")
        raise RuntimeError(f"Failed while creating {OUTPUT_CSV}: {exc}") from exc

    # ---------- Phase 2: only after CSV is complete, read polygons and write GeoJSON ----------
    polygons = load_polygons(POLIGONOS_FILE)
    polygon_lookup = {item["name"]: item["color"] for item in polygons}
    log.info("Polygon colors: %s", json.dumps(polygon_lookup, ensure_ascii=False))
    log.info("Phase 2B: reading completed CSV and writing GeoJSON to %s", OUTPUT_GEOJSON)

    if os.path.exists(OUTPUT_GEOJSON):
        os.remove(OUTPUT_GEOJSON)
        log.info("Removed previous GeoJSON: %s", OUTPUT_GEOJSON)

    feature_count = 0
    invalid_geojson_coords = 0
    polygon_counts = defaultdict(int)
    outside_count = 0
    buffer = []

    try:
        with open(OUTPUT_GEOJSON, "w", encoding="utf-8") as geojson_file:
            geojson_file.write('{"type":"FeatureCollection","features":[\n')
            first_feature = True
            for csv_chunk_no, df in enumerate(pd.read_csv(OUTPUT_CSV, chunksize=CHUNK_SIZE, dtype=str, low_memory=False), start=1):
                log.info("GeoJSON source chunk %d: read %s CSV rows", csv_chunk_no, f"{len(df):,}")
                for row in df.to_dict(orient="records"):
                    east = to_number(row.get("ESTE"))
                    north = to_number(row.get("NORTE"))
                    lat = to_number(row.get("latitude"))
                    lon = to_number(row.get("longitude"))
                    if east is None or north is None or lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                        invalid_geojson_coords += 1
                        continue

                    polygon_name, color = assign_polygon(east, north, polygons)
                    polygon_counts[polygon_name] += 1
                    if polygon_name == "Outside all polygons":
                        outside_count += 1

                    props = {}
                    for col, value in row.items():
                        if col in {"latitude", "longitude"}:
                            continue
                        if col.startswith("__"):
                            continue
                        props[col] = json_safe(value)
                    props["polygon_name"] = polygon_name
                    props["polygon_color"] = color
                    # MapLibre-friendly common property for data-driven circle styling.
                    props["marker-color"] = color

                    feature = {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                        "properties": props,
                    }
                    buffer.append(json.dumps(feature, ensure_ascii=False, allow_nan=False))
                    feature_count += 1
                    if len(buffer) >= GEOJSON_BATCH_SIZE:
                        if not first_feature:
                            geojson_file.write(",\n")
                        geojson_file.write(",\n".join(buffer))
                        first_feature = False
                        buffer.clear()

                # Flush per source chunk so memory stays bounded.
                if buffer:
                    if not first_feature:
                        geojson_file.write(",\n")
                    geojson_file.write(",\n".join(buffer))
                    first_feature = False
                    buffer.clear()
                log.info(
                    "GeoJSON progress: source rows read=%s, features written=%s, invalid coordinates=%s",
                    f"{min(csv_chunk_no * CHUNK_SIZE, total_output):,}", f"{feature_count:,}", f"{invalid_geojson_coords:,}"
                )
            geojson_file.write("\n]}")
    except Exception as exc:
        log.exception("GeoJSON phase failed after writing %s features", f"{feature_count:,}")
        raise RuntimeError(f"Failed while creating {OUTPUT_GEOJSON}: {exc}") from exc

    elapsed = datetime.now() - started
    log.info("Workflow completed successfully in %s", elapsed)
    log.info("Input CargaDescarga rows: %s", f"{total_input:,}")
    log.info("CSV joined rows: %s", f"{total_output:,}")
    log.info("GeoJSON features written: %s", f"{feature_count:,}")
    log.info("GeoJSON rows skipped for invalid coordinates: %s", f"{invalid_geojson_coords:,}")
    log.info("Points outside all polygons: %s", f"{outside_count:,}")
    log.info("Point counts by polygon: %s", json.dumps(dict(polygon_counts), ensure_ascii=False))
    log.info("Output CSV: %s", os.path.abspath(OUTPUT_CSV))
    log.info("Output GeoJSON: %s", os.path.abspath(OUTPUT_GEOJSON))
    log.info("Log file: %s", os.path.abspath(LOG_FILE))


if __name__ == "__main__":
    cli_args = parse_args()
    try:
        main(start_date=cli_args.start_date, end_date=cli_args.end_date)
    except Exception as error:
        log.error("FATAL: %s", error)
        log.error("Workflow stopped. See %s for details.", LOG_FILE)
        sys.exit(1)