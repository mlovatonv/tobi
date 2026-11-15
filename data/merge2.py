#!/usr/bin/env python3
"""Join loading cycles to time-valid shovel coordinates.

Expected files in the working directory:
  - Palas.csv: EQUIPO, ESTE, NORTE, HORA, PERIODO (plus optional attributes)
  - CargaDescarga.csv: Pala, Fecha, Hora Inicio Ciclo, Hora Final Ciclo (plus attributes)
  - Poligonos.csv: one vertex per row, polygon identifier plus East/North coordinates

Polygon vertices are grouped by polygon identifier and kept in input order unless a
vertex-order column is available. Spatial tests use UTM coordinates.

Usage:
  python merge2.py                                        # all dates
  python merge2.py --start-date 20260101 --end-date 20260131
  python merge2.py --start-date 20260115                  # from that date onward
  python merge2.py --end-date 20260115                    # up to and including that date

Dates are YYYYMMDD (YYYY-MM-DD also accepted) and both bounds are inclusive. The filter is
applied to the cycle date (Fecha).
"""

import argparse
import colorsys
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

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
LOG_FILE = "PalasCargaDescarga.log"
CHUNK_SIZE = 100

REQUIRED_PALAS_COLUMNS = {"EQUIPO", "ESTE", "NORTE", "HORA", "PERIODO"}
REQUIRED_CARGA_COLUMNS = ("Pala", "Fecha", "Hora Inicio Ciclo", "Hora Final Ciclo")
# Helper columns of the Palas lookup; they are not copied into the output.
AVG_COLS = ("east_avg", "north_avg", "latitude_avg", "longitude_avg")
HELPER_COLS = {"match_key", "__palas_clock", "__palas_date", "__east_num", "__north_num", "latitude", "longitude",
               "palas_matches", *AVG_COLS}

POLYGON_ID_ALIASES = [
    "Poligono", "Polígono", "Polygon", "Polygon Name", "Nombre Poligono", "Nombre de Poligono",
    "Nombre de Polígono", "Polygon ID", "Nombre", "Name", "ID", "Id",
]
EAST_ALIASES = ["ESTE", "EAST", "East", "Easting", "X", "UTM Easting"]
NORTH_ALIASES = ["NORTE", "NORTH", "North", "Northing", "Y", "UTM Northing"]
VERTEX_ORDER_ALIASES = ["Orden", "Order", "Vertex Order", "Punto", "Point Order", "Secuencia", "Sequence", "Vertice", "Vértice"]

SECONDS_PER_DAY = 86400
NO_POLYGON = "NA"
NO_POLYGON_COLOR = "#9E9E9E"

# Match requires: equipment key AND PERIODO == Fecha (calendar date, format YYYYMMDD) AND
# HORA within the cycle's start/end (inclusive). Overnight cycles (start > end) also accept
# HORA <= end on the day after Fecha.
# Cycle start/end are compared as time-of-day only; the date comes from Fecha.
# The output has exactly one row per cycle (within the date filter). Palas attributes and
# ESTE/NORTE/latitude/longitude come from the match closest to the cycle start; east_avg,
# north_avg, latitude_avg and longitude_avg hold the center (mean) of all matched coordinates.
# The polygon (and its HEX color) is the one containing that center, or NA. Cycles without a
# match keep blank Palas fields.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")],
)
log = logging.getLogger("palas_join")

transformer = Transformer.from_crs(UTM_EPSG, "EPSG:4326", always_xy=True)


# ---------- Parsing helpers ----------

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
    """Return seconds since midnight, or NaN for blank/unparseable values."""
    if pd.isna(value):
        return float("nan")
    try:
        parsed = pd.to_datetime(str(value).strip(), errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    if pd.isna(parsed):
        return float("nan")
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second + parsed.microsecond / 1e6


def parse_date(value):
    """Return a datetime.date, or None. Expected format is YYYYMMDD (e.g. 20260101)."""
    if pd.isna(value):
        return None
    text = str(value).strip().removesuffix(".0")  # in case a number was saved as a float
    if not text:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        # Fallback for any stray ISO-style value such as 2026-01-01 or 2026-01-01 08:00:00.
        parsed = pd.to_datetime(text, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    except (TypeError, ValueError, OverflowError):
        return None


def in_range(day, first, last):
    """Inclusive date range check; either bound may be None. Missing days never match."""
    return not pd.isna(day) and (first is None or day >= first) and (last is None or day <= last)


# ---------- Command line ----------

def cli_date(value):
    """argparse type: accept YYYYMMDD or YYYY-MM-DD and return a datetime.date."""
    parsed = parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid date; use YYYYMMDD (e.g. 20260101)")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Join loading cycles to shovel coordinates by polygon.")
    parser.add_argument("--start-date", type=cli_date, default=None, metavar="YYYYMMDD",
                        help="first cycle date (Fecha) to include, inclusive")
    parser.add_argument("--end-date", type=cli_date, default=None, metavar="YYYYMMDD",
                        help="last cycle date (Fecha) to include, inclusive")
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error(f"--start-date ({args.start_date}) is after --end-date ({args.end_date})")
    return args


# ---------- Polygons ----------

def read_polygon_vertices(path):
    """Read, validate and order polygon vertices; returns one row per valid vertex."""
    log.info("Phase 2A: reading polygon vertices from %s", path)
    df = pd.read_csv(path, dtype=str, low_memory=False)
    if df.empty:
        raise ValueError(f"{path} contains no polygon vertices.")

    polygon_col = find_column(df, POLYGON_ID_ALIASES, "polygon identifier column")
    east_col = find_column(df, EAST_ALIASES, "polygon East/Easting coordinate column")
    north_col = find_column(df, NORTH_ALIASES, "polygon North/Northing coordinate column")
    order_col = find_column(df, VERTEX_ORDER_ALIASES, "polygon vertex order column", required=False)

    df["__polygon_name"] = df[polygon_col].fillna("").astype(str).str.strip()
    df["__east"] = pd.to_numeric(df[east_col], errors="coerce")
    df["__north"] = pd.to_numeric(df[north_col], errors="coerce")
    df["__source_row"] = range(len(df))
    if order_col:
        df["__vertex_order"] = pd.to_numeric(df[order_col], errors="coerce")
        df.sort_values(["__polygon_name", "__vertex_order", "__source_row"], inplace=True, na_position="last")
        log.info("Using '%s' to order polygon vertices where available", order_col)
    else:
        log.info("No vertex-order column found; using the order of rows in %s", path)

    valid = df["__polygon_name"].ne("") & df["__east"].notna() & df["__north"].notna()
    if (~valid).any():
        log.warning("Ignoring %s polygon rows with missing polygon names or invalid East/North values",
                    f"{int((~valid).sum()):,}")
    return df.loc[valid]


def build_polygon(name, coords):
    """Return a prepared polygon for the vertices, or None if it is unusable."""
    # A repeated final vertex is fine; Polygon closes rings automatically.
    if len(coords) < 3:
        log.warning("Skipping polygon '%s': only %d valid vertices (need at least 3)", name, len(coords))
        return None
    try:
        polygon = Polygon(coords)
        if not polygon.is_valid:
            log.warning("Polygon '%s' is invalid/self-intersecting; attempting make_valid via buffer(0)", name)
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon"):
            log.warning("Skipping polygon '%s': geometry is empty or not polygonal", name)
            return None
        log.info("Loaded polygon '%s' with %d vertices", name, len(coords))
        return prep(polygon)
    except Exception:
        log.exception("Could not build polygon '%s'; skipping it", name)
        return None


def polygon_color(index):
    """Distinct, deterministic HEX color per polygon index (golden-ratio hue spacing)."""
    r, g, b = colorsys.hsv_to_rgb((index * 0.618033988749895) % 1, 0.65, 0.95)
    return f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def load_polygons(path):
    """Return a list of (name, HEX color, prepared polygon) tuples."""
    vertices = read_polygon_vertices(path)
    polygons = []
    for name, group in vertices.groupby("__polygon_name", sort=False):
        prepared = build_polygon(name, list(zip(group["__east"], group["__north"])))
        if prepared is not None:
            polygons.append((str(name), polygon_color(len(polygons)), prepared))

    if not polygons:
        raise ValueError(
            f"No usable polygons were built from {path}. Check the polygon ID, East/North columns, "
            "and that each polygon has at least three valid vertices."
        )
    log.info("Usable polygons loaded: %d", len(polygons))
    return polygons


def assign_polygon(east, north, polygons):
    """Return (name, color) of the first polygon containing the point (boundary included), else NA."""
    if pd.isna(east) or pd.isna(north):
        return NO_POLYGON, NO_POLYGON_COLOR
    point = Point(east, north)
    return next(((name, color) for name, color, prepared in polygons if prepared.covers(point)),
                (NO_POLYGON, NO_POLYGON_COLOR))


# ---------- Palas (shovel coordinates) ----------

def filter_palas_by_date(palas, start_date, end_date):
    """Keep Palas rows in range, plus one extra day so overnight cycles on end_date still match."""
    last_day = end_date + timedelta(days=1) if end_date is not None else None
    keep = palas["__palas_date"].apply(lambda d: in_range(d, start_date, last_day)).astype(bool)
    log.info("Palas date filter: kept %s rows, dropped %s", f"{int(keep.sum()):,}", f"{int((~keep).sum()):,}")
    return palas.loc[keep].copy()


def add_coordinates(palas):
    """Add helper numeric UTM columns and WGS84 latitude/longitude."""
    palas["__east_num"] = pd.to_numeric(palas["ESTE"], errors="coerce")
    palas["__north_num"] = pd.to_numeric(palas["NORTE"], errors="coerce")
    valid = palas["__east_num"].notna() & palas["__north_num"].notna()
    palas["latitude"] = float("nan")
    palas["longitude"] = float("nan")
    if valid.any():
        lon, lat = transformer.transform(
            palas.loc[valid, "__east_num"].to_numpy(),
            palas.loc[valid, "__north_num"].to_numpy(),
        )
        palas.loc[valid, "latitude"] = lat
        palas.loc[valid, "longitude"] = lon
    return int(valid.sum())


def load_palas(start_date, end_date):
    log.info("Phase 1A: loading %s", PALAS_FILE)
    palas = pd.read_csv(PALAS_FILE, dtype=str, low_memory=False)
    missing = REQUIRED_PALAS_COLUMNS - set(palas.columns)
    if missing:
        raise ValueError(f"{PALAS_FILE} is missing required columns: {', '.join(sorted(missing))}")

    palas["match_key"] = palas["EQUIPO"].apply(transform_equipo)
    palas["__palas_clock"] = palas["HORA"].apply(parse_clock)
    palas["__palas_date"] = palas["PERIODO"].apply(parse_date)
    log.info("Palas rows with unparseable PERIODO: %s", f"{int(palas['__palas_date'].isna().sum()):,}")
    if start_date is not None or end_date is not None:
        palas = filter_palas_by_date(palas, start_date, end_date)
    valid_utm = add_coordinates(palas)

    log.info("Palas rows: %s", f"{len(palas):,}")
    log.info("Distinct normalized equipment keys: %s", f"{palas['match_key'].nunique():,}")
    log.info("Palas rows with valid numeric UTM coordinates: %s", f"{valid_utm:,}")
    log.info("Palas rows with unparseable HORA: %s", f"{int(palas['__palas_clock'].isna().sum()):,}")
    return palas


def build_lookup(palas, carga_columns):
    """Index Palas records by (equipment, date) to avoid a huge many-to-many merge before time filtering."""
    # Palas attributes that collide with loading fields are dropped rather than overwriting cycle data.
    overlaps = (set(palas.columns) & set(carga_columns)) - HELPER_COLS
    lookup = palas.drop(columns=list(overlaps))
    by_key = {
        (str(key), day): group.to_dict(orient="records")
        for (key, day), group in lookup.groupby(["match_key", "__palas_date"], sort=False, dropna=True)
    }
    log.info("Prepared lookup for %s equipment/date combinations", f"{len(by_key):,}")
    # Template used for cycles without a Palas match so every output row has the same columns.
    return by_key, dict.fromkeys(lookup.columns)


def find_matches(by_key, key, day, start, end):
    """Palas records for this equipment/date whose HORA falls inside the cycle."""
    same_day = by_key.get((key, day), [])
    if start <= end:
        return [c for c in same_day if start <= c["__palas_clock"] <= end]
    # Overnight cycle: late part on Fecha, early part on the following day.
    next_day = by_key.get((key, day + timedelta(days=1)), [])
    return ([c for c in same_day if c["__palas_clock"] >= start]
            + [c for c in next_day if c["__palas_clock"] <= end])


def pick_match(matches, start):
    """The Palas record closest to the cycle start (wrapping past midnight)."""
    return min(matches, key=lambda c: (c["__palas_clock"] - start) % SECONDS_PER_DAY)


def build_candidate(matches, start):
    """Closest Palas record plus the center (mean) of all matched coordinates in the *_avg fields."""
    points = [(c["__east_num"], c["__north_num"]) for c in matches
              if pd.notna(c["__east_num"]) and pd.notna(c["__north_num"])]
    east = north = lat = lon = float("nan")
    if points:
        east = sum(p[0] for p in points) / len(points)
        north = sum(p[1] for p in points) / len(points)
        lon, lat = transformer.transform(east, north)
    return {**pick_match(matches, start), "east_avg": east, "north_avg": north,
            "latitude_avg": lat, "longitude_avg": lon, "palas_matches": len(matches)}


# ---------- Cycle processing ----------

def merge_record(record, candidate, key, polygons):
    """Combine a loading cycle with a Palas candidate (or the blank template); returns (row, polygon name)."""
    merged = dict(record)
    merged.update({col: value for col, value in candidate.items() if col not in HELPER_COLS})
    merged["match_key"] = key
    for col in ("HORA", "ESTE", "NORTE", "latitude", "longitude", *AVG_COLS):
        merged[col] = candidate.get(col)
    merged["palas_matches"] = candidate.get("palas_matches", 0)
    merged["polygon"], merged["polygon_color"] = assign_polygon(merged["east_avg"], merged["north_avg"], polygons)
    return merged, merged["polygon"]


def process_chunk(chunk, by_key, blank, known_keys, polygons, start_date, end_date, stats, polygon_counts):
    """Left-join one chunk of cycles to Palas records: one output row per cycle, matched or not."""
    filtering = start_date is not None or end_date is not None
    chunk["Pala"] = chunk["Pala"].fillna("").astype(str).str.strip()
    chunk["__cycle_start"] = chunk["Hora Inicio Ciclo"].apply(parse_clock)
    chunk["__cycle_end"] = chunk["Hora Final Ciclo"].apply(parse_clock)
    chunk["__cycle_date"] = chunk["Fecha"].apply(parse_date)

    out_records = []
    for record in chunk.to_dict(orient="records"):
        day, start, end, key = record["__cycle_date"], record["__cycle_start"], record["__cycle_end"], record["Pala"]

        if filtering and not in_range(day, start_date, end_date):
            stats["outside_date_range"] += 1
            continue

        candidate = blank
        if key not in known_keys:
            stats["unmatched_equipment"] += 1
        elif pd.isna(start) or pd.isna(end) or pd.isna(day):
            stats["invalid_cycle_time"] += 1
        else:
            matches = find_matches(by_key, key, day, start, end)
            if not matches:
                stats["no_time_match"] += 1
            else:
                stats["multiple_matches"] += len(matches) > 1
                candidate = build_candidate(matches, start)

        merged, polygon_name = merge_record(record, candidate, key, polygons)
        polygon_counts[polygon_name] += 1
        if polygon_name == NO_POLYGON:
            stats["no_polygon"] += 1
        out_records.append(merged)
    return out_records


def append_output(out_records, write_header):
    """Append joined rows to the output CSV and return the number written."""
    output = pd.DataFrame.from_records(out_records)
    output.drop(columns=["__cycle_start", "__cycle_end", "__cycle_date"], inplace=True, errors="ignore")
    output.to_csv(OUTPUT_CSV, mode="a", index=False, header=write_header)
    return len(output)


def write_empty_output(carga_columns):
    """Create an empty CSV with a useful header when there were no matches."""
    base_columns = list(carga_columns) + ["match_key", "HORA", "ESTE", "NORTE", "latitude", "longitude"]
    pd.DataFrame(columns=list(dict.fromkeys(base_columns))).to_csv(OUTPUT_CSV, index=False)


def join_cycles(by_key, blank, polygons, carga_columns, start_date, end_date):
    """Phase 1B: stream CargaDescarga in chunks and write the joined CSV."""
    log.info("Phase 1B: processing %s in chunks of %d", CARGA_FILE, CHUNK_SIZE)
    if os.path.exists(OUTPUT_CSV):
        os.remove(OUTPUT_CSV)
        log.info("Removed previous output CSV: %s", OUTPUT_CSV)

    known_keys = {key for key, _ in by_key}
    stats = Counter()
    polygon_counts = Counter()
    try:
        reader = pd.read_csv(CARGA_FILE, chunksize=CHUNK_SIZE, dtype=str, low_memory=False)
        for chunk_count, chunk in enumerate(reader, start=1):
            stats["input"] += len(chunk)
            out_records = process_chunk(chunk, by_key, blank, known_keys, polygons, start_date, end_date,
                                        stats, polygon_counts)
            if out_records:
                stats["output"] += append_output(out_records, write_header=stats["output"] == 0)
            log.info(
                "CSV chunk %d complete: input=%s, joined-so-far=%s, unmatched-equipment=%s, no-time-match=%s",
                chunk_count, f"{stats['input']:,}", f"{stats['output']:,}",
                f"{stats['unmatched_equipment']:,}", f"{stats['no_time_match']:,}",
            )
        if stats["output"] == 0:
            write_empty_output(carga_columns)
    except Exception as exc:
        log.exception("CSV phase failed after processing %s input rows", f"{stats['input']:,}")
        raise RuntimeError(f"Failed while creating {OUTPUT_CSV}: {exc}") from exc
    return stats, polygon_counts


def log_summary(stats, polygon_counts, filtering, elapsed):
    log.info("Phase 1 complete: %s written with %s joined rows", OUTPUT_CSV, f"{stats['output']:,}")
    if filtering:
        log.info("Rows skipped by date filter (outside range or invalid Fecha): %s", f"{stats['outside_date_range']:,}")
    log.info("Rows without equipment match (kept, blank Palas fields): %s", f"{stats['unmatched_equipment']:,}")
    log.info("Rows with invalid cycle start/end time or Fecha (kept, blank Palas fields): %s", f"{stats['invalid_cycle_time']:,}")
    log.info("Rows with equipment match but no HORA inside cycle date/interval (kept, blank Palas fields): %s", f"{stats['no_time_match']:,}")
    log.info("Rows with several matching HORA records (coordinates averaged): %s", f"{stats['multiple_matches']:,}")
    log.info("Workflow completed successfully in %s", elapsed)
    log.info("Input CargaDescarga rows: %s", f"{stats['input']:,}")
    log.info("CSV joined rows: %s", f"{stats['output']:,}")
    log.info("Rows with polygon NA (no coordinates or center outside all polygons): %s", f"{stats['no_polygon']:,}")
    log.info("Point counts by polygon: %s", json.dumps(dict(polygon_counts), ensure_ascii=False))
    log.info("Output CSV: %s", os.path.abspath(OUTPUT_CSV))
    log.info("Log file: %s", os.path.abspath(LOG_FILE))


def main(start_date=None, end_date=None):
    started = datetime.now()
    for filename in (PALAS_FILE, CARGA_FILE, POLIGONOS_FILE):
        if not os.path.isfile(filename):
            raise FileNotFoundError(f"Required input file not found: {filename}")

    carga_columns = pd.read_csv(CARGA_FILE, nrows=0).columns
    for col in REQUIRED_CARGA_COLUMNS:
        if col not in carga_columns:
            raise ValueError(f"{CARGA_FILE} is missing required column '{col}'")

    log.info("Starting workflow; UTM CRS=%s; chunk size=%s", UTM_EPSG, CHUNK_SIZE)
    log.info("Date filter (inclusive, on Fecha): start=%s, end=%s", start_date or "none", end_date or "none")

    palas = load_palas(start_date, end_date)
    by_key, blank = build_lookup(palas, carga_columns)
    polygons = load_polygons(POLIGONOS_FILE)

    stats, polygon_counts = join_cycles(by_key, blank, polygons, carga_columns, start_date, end_date)
    log_summary(stats, polygon_counts, start_date is not None or end_date is not None, datetime.now() - started)


if __name__ == "__main__":
    cli_args = parse_args()
    try:
        main(start_date=cli_args.start_date, end_date=cli_args.end_date)
    except Exception as error:
        log.error("FATAL: %s", error)
        log.error("Workflow stopped. See %s for details.", LOG_FILE)
        sys.exit(1)
