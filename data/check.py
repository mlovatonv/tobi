#!/usr/bin/env python3
"""
Diagnose why a large point dataset appears to show very few points in MapLibre.

Reads CSV data in chunks, counts valid/missing coordinates, detects exact duplicate
latitude/longitude pairs, summarizes coordinate uniqueness at configurable decimal
precision, and reports geographic bounds. It does NOT generate nearby-point pairs.

Install:
    python -m pip install pandas

Usage:
    python diagnose_map_points.py carga.csv --chunk-size 10000
    python diagnose_map_points.py carga.csv --chunk-size 10000 --delimiter ";"
    python diagnose_map_points.py carga.csv --round-decimals 6 --sample-output unique_locations.csv

Notes:
- latitude/longitude must be WGS84-style decimal degrees for MapLibre GeoJSON.
- If your map uses ESTE/NORTE (e.g. UTM), those coordinates must be projected correctly;
  do not put UTM values into GeoJSON longitude/latitude positions.
"""

import argparse
import csv
import logging
import time
from collections import Counter
from pathlib import Path

import pandas as pd


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Diagnose duplicate/missing coordinates in a CSV for MapLibre."
    )
    parser.add_argument("csv_file", help="Input CSV path")
    parser.add_argument("--chunk-size", type=int, default=10000, help="Rows per chunk (default: 10000)")
    parser.add_argument("--encoding", default="utf-8-sig", help="CSV encoding (default: utf-8-sig)")
    parser.add_argument("--delimiter", default=None, help="CSV delimiter such as ',' or ';'; auto-detect by default")
    parser.add_argument("--lat-column", default="latitude", help="Latitude column name (default: latitude)")
    parser.add_argument("--lon-column", default="longitude", help="Longitude column name (default: longitude)")
    parser.add_argument(
        "--round-decimals",
        type=int,
        default=8,
        help="Decimal precision for approximate location counts (default: 6)",
    )
    parser.add_argument(
        "--sample-output",
        default=None,
        help="Optional CSV path to write distinct rounded locations and counts",
    )
    args = parser.parse_args()

    if args.chunk_size < 1:
        parser.error("--chunk-size must be at least 1")
    if not 0 <= args.round_decimals <= 12:
        parser.error("--round-decimals must be between 0 and 12")

    source = Path(args.csv_file)
    if not source.is_file():
        parser.error(f"File not found: {source}")

    started = time.time()
    logging.info("Starting MapLibre coordinate diagnosis")
    logging.info("Input: %s", source.resolve())
    logging.info("Chunk size: %s", f"{args.chunk_size:,}")

    # Estimate line count for progress logs. This is approximate if quoted fields
    # contain embedded newlines.
    try:
        with open(source, "r", encoding=args.encoding, errors="replace") as f:
            estimated_rows = max(0, sum(1 for line in f if line.strip()) - 1)
        logging.info("Estimated data rows: %s", f"{estimated_rows:,}")
    except OSError:
        estimated_rows = 0

    if args.delimiter:
        delimiter = args.delimiter
    else:
        with open(source, "r", encoding=args.encoding, errors="replace") as f:
            sample = f.read(65536)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
            logging.warning("Could not detect delimiter; assuming comma.")
    logging.info("Delimiter: %r", delimiter)

    read_kwargs = {
        "chunksize": args.chunk_size,
        "dtype": str,
        "encoding": args.encoding,
        "sep": delimiter,
    }

    total_rows = 0
    valid_rows = 0
    missing_or_invalid_rows = 0
    exact_counts = Counter()
    rounded_counts = Counter()
    lat_min = lat_max = lon_min = lon_max = None
    sample_columns = None

    try:
        for chunk_num, chunk in enumerate(pd.read_csv(source, **read_kwargs), start=1):
            chunk.columns = [str(c).strip() for c in chunk.columns]
            if chunk_num == 1:
                missing_cols = [
                    c for c in (args.lat_column, args.lon_column)
                    if c not in chunk.columns
                ]
                if missing_cols:
                    parser.error(
                        "Missing coordinate column(s): "
                        + ", ".join(missing_cols)
                        + ". Available columns: "
                        + ", ".join(chunk.columns)
                    )
                sample_columns = list(chunk.columns)

            rows = len(chunk)
            total_rows += rows
            lat = pd.to_numeric(chunk[args.lat_column], errors="coerce")
            lon = pd.to_numeric(chunk[args.lon_column], errors="coerce")

            valid = (
                lat.notna()
                & lon.notna()
                & lat.between(-90, 90)
                & lon.between(-180, 180)
            )
            good_lat = lat.loc[valid]
            good_lon = lon.loc[valid]
            valid_count = int(valid.sum())
            invalid_count = rows - valid_count
            valid_rows += valid_count
            missing_or_invalid_rows += invalid_count

            if valid_count:
                chunk_coords = pd.DataFrame({
                    "latitude": good_lat.astype(float).to_numpy(),
                    "longitude": good_lon.astype(float).to_numpy(),
                })

                # Exact coordinate duplicates, independent of input row order.
                exact_counts.update(
                    zip(chunk_coords["latitude"], chunk_coords["longitude"])
                )

                # Rounded coordinate grouping helps reveal stacked/near-identical
                # locations. This is a diagnostic grouping, not a distance test.
                rounded_counts.update(
                    zip(
                        chunk_coords["latitude"].round(args.round_decimals),
                        chunk_coords["longitude"].round(args.round_decimals),
                    )
                )

                c_lat_min, c_lat_max = float(good_lat.min()), float(good_lat.max())
                c_lon_min, c_lon_max = float(good_lon.min()), float(good_lon.max())
                lat_min = c_lat_min if lat_min is None else min(lat_min, c_lat_min)
                lat_max = c_lat_max if lat_max is None else max(lat_max, c_lat_max)
                lon_min = c_lon_min if lon_min is None else min(lon_min, c_lon_min)
                lon_max = c_lon_max if lon_max is None else max(lon_max, c_lon_max)

            if estimated_rows:
                pct = min(100.0, total_rows * 100.0 / estimated_rows)
                logging.info(
                    "Chunk %d | rows=%s/%s (%.1f%%) | valid coords=%s | invalid coords=%s",
                    chunk_num, f"{total_rows:,}", f"{estimated_rows:,}", pct,
                    f"{valid_count:,}", f"{invalid_count:,}",
                )
            else:
                logging.info(
                    "Chunk %d | rows=%s | valid coords=%s | invalid coords=%s",
                    chunk_num, f"{total_rows:,}", f"{valid_count:,}", f"{invalid_count:,}",
                )
    except UnicodeDecodeError:
        parser.error(f"Could not decode file using {args.encoding}; try --encoding latin-1")

    unique_exact = len(exact_counts)
    unique_rounded = len(rounded_counts)
    duplicate_rows_exact = valid_rows - unique_exact

    logging.info("=" * 65)
    logging.info("DIAGNOSTIC SUMMARY")
    logging.info("Total CSV records: %s", f"{total_rows:,}")
    logging.info("Records with valid coordinates: %s", f"{valid_rows:,}")
    logging.info("Records with missing/invalid coordinates: %s", f"{missing_or_invalid_rows:,}")
    logging.info("Unique exact latitude/longitude pairs: %s", f"{unique_exact:,}")
    logging.info("Rows beyond the first occurrence of each exact location: %s", f"{duplicate_rows_exact:,}")
    logging.info(
        "Unique locations rounded to %d decimals: %s",
        args.round_decimals, f"{unique_rounded:,}",
    )

    if lat_min is not None:
        logging.info("Latitude bounds: %.8f .. %.8f", lat_min, lat_max)
        logging.info("Longitude bounds: %.8f .. %.8f", lon_min, lon_max)
        if lat_min >= -90 and lat_max <= 90 and lon_min >= -180 and lon_max <= 180:
            logging.info("Coordinate ranges are valid decimal degrees.")
        else:
            logging.warning("Coordinate bounds are outside valid longitude/latitude ranges.")

    if valid_rows and unique_exact <= 20:
        logging.warning(
            "Only %s exact coordinate locations exist for %s valid rows. "
            "Many records are stacked at identical locations.",
            unique_exact, f"{valid_rows:,}",
        )
    elif valid_rows and unique_exact < valid_rows * 0.1:
        logging.warning(
            "A high proportion of rows share exact coordinates; overlapping markers may hide each other."
        )
    elif valid_rows:
        logging.info(
            "There are many exact locations. If MapLibre still shows only ~12 points, "
            "inspect the GeoJSON feature count, coordinate order, filters, zoom/viewport, and source loading."
        )

    if args.sample_output:
        out_path = Path(args.sample_output)
        # Export one row per rounded location with number of records at that location.
        # This is intentionally a summary, not all original row properties.
        summary = pd.DataFrame(
            [
                {
                    args.lat_column: lat,
                    args.lon_column: lon,
                    "record_count": count,
                }
                for (lat, lon), count in rounded_counts.items()
            ]
        )
        summary.to_csv(out_path, index=False, encoding="utf-8-sig")
        logging.info("Wrote distinct rounded-location summary: %s", out_path.resolve())

    logging.info("Finished in %.2f seconds", time.time() - started)


if __name__ == "__main__":
    main()
