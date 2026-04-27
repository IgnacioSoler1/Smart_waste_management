"""
etl_daily.py — SmartWaste MVD / Glue ETL Job (PySpark, Glue 4.0)

Daily analytics ETL that runs at 03:00 UTC (midnight Montevideo).

Bronze → Silver → Gold pipeline:
  1. Read container metadata from DynamoDB (driver-side, boto3)
  2. Read Bronze GZIP JSON sensor readings from S3 (Spark)
  3. Convert Bronze → Silver Parquet (Spark write, partitioned by date)
  4. Compute Spark aggregations: container stats, hourly fill pattern
  5. Read route-results Bronze JSONs from S3 (Spark)
  6. Compute route efficiency stats with Spark groupBy/agg
  7. Compute fill rate predictions using scipy (driver-side, reads Silver Parquet)
  8. Assemble Gold analytics JSON and write to S3 (driver-side)

Glue job arguments (injected by Terraform):
  --DATA_LAKE_BUCKET   S3 bucket for data lake
  --CONTAINERS_TABLE   DynamoDB table with container metadata
  --ROUTES_TABLE       DynamoDB table with active route data
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import numpy as np
from scipy.optimize import curve_fit

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, StringType
from pyspark.sql.window import Window

# ─────────────────────────────────────────────────────────
# Glue / Spark initialization
# ─────────────────────────────────────────────────────────

args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "DATA_LAKE_BUCKET",
    "CONTAINERS_TABLE",
    "ROUTES_TABLE",
])

# Optional: override the processing date (YYYY-MM-DD). Default: yesterday UTC.
# getResolvedOptions only processes declared required keys, so read optional args from sys.argv.
_run_date_idx = sys.argv.index("--RUN_DATE") if "--RUN_DATE" in sys.argv else -1
_RUN_DATE_OVERRIDE: str | None = sys.argv[_run_date_idx + 1] if _run_date_idx >= 0 else None

DATA_LAKE_BUCKET = args["DATA_LAKE_BUCKET"]
CONTAINERS_TABLE = args["CONTAINERS_TABLE"]
ROUTES_TABLE     = args["ROUTES_TABLE"]

sc = SparkContext.getOrCreate()
glue_ctx = GlueContext(sc)
spark: SparkSession = glue_ctx.spark_session
job = Job(glue_ctx)
job.init(args["JOB_NAME"], args)

logger = logging.getLogger("etl_daily")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)

# ─────────────────────────────────────────────────────────
# AWS clients (driver-side only)
# ─────────────────────────────────────────────────────────

dynamodb = boto3.resource("dynamodb")
s3       = boto3.client("s3")


# ─────────────────────────────────────────────────────────
# Bronze sensor file parser (serialized to Spark executors)
# ─────────────────────────────────────────────────────────

def _parse_concatenated_gz(path_content_pair: tuple) -> list:
    """
    Extract individual JSON objects from a GZIP file that contains multiple
    JSON objects concatenated without newlines (Kinesis Firehose format).

    Spark's spark.read.json() treats each file as a stream of newline-delimited
    records. With zero newlines the entire file = one "line" → only the first
    JSON object is parsed. This function uses Python's JSONDecoder.raw_decode()
    to stream through the full text and extract every object.

    Called via sc.binaryFiles().flatMap() so all imports must be local.
    """
    import gzip as _gzip
    import json as _json

    _path, raw_bytes = path_content_pair
    try:
        text = _gzip.decompress(bytes(raw_bytes)).decode("utf-8")
    except Exception:
        return []

    decoder = _json.JSONDecoder()
    results: list = []
    pos = 0
    n = len(text)
    while pos < n:
        # Skip whitespace between objects
        while pos < n and text[pos] in " \t\n\r":
            pos += 1
        if pos >= n:
            break
        try:
            obj, end_pos = decoder.raw_decode(text, pos)
            results.append(_json.dumps(obj))
            pos = end_pos
        except _json.JSONDecodeError:
            break
    return results


# ─────────────────────────────────────────────────────────
# Step 1: Container metadata from DynamoDB (driver-side)
# ─────────────────────────────────────────────────────────

def _circuit_to_zone(circuit_id: str) -> str:
    """Derive zone from circuit_id convention."""
    cid = circuit_id.upper()
    if "_DU_" in cid or "_DI_" in cid:
        return "east"
    if "_RU_" in cid or "_RI_" in cid:
        return "west"
    return "unknown"


def load_container_metadata() -> dict[str, dict]:
    """Scan DynamoDB containers table, return {container_id: metadata}."""
    table = dynamodb.Table(CONTAINERS_TABLE)
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "ProjectionExpression": "container_id, circuit_id, latitude, longitude, shift, capacity_liters",
    }
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    metadata: dict[str, dict] = {}
    for item in items:
        cid = str(item.get("container_id", ""))
        if not cid:
            continue
        circuit = str(item.get("circuit_id", ""))
        metadata[cid] = {
            "circuit_id":      circuit,
            "shift":           str(item.get("shift", "")),
            "latitude":        float(item.get("latitude", 0)),
            "longitude":       float(item.get("longitude", 0)),
            "capacity_liters": float(item.get("capacity_liters", 1100)),
            "zone":            _circuit_to_zone(circuit),
        }
    logger.info("Loaded %d containers from DynamoDB", len(metadata))
    return metadata


# ─────────────────────────────────────────────────────────
# Step 2 + 3: Bronze → Silver (Spark)
# ─────────────────────────────────────────────────────────

def process_sensor_data(date: datetime) -> tuple[DataFrame, DataFrame]:
    """
    Read Bronze GZIP JSON for the given date, write Silver Parquet,
    and return (container_aggs_df, hourly_pattern_df).

    Bronze path: s3://{bucket}/sensor-readings/year=Y/month=MM/day=DD/hour=HH/
    Silver path: s3://{bucket}/sensor-readings-parquet/date=YYYY-MM-DD/
    """
    y, m, d = date.year, date.month, date.day
    date_str = date.strftime("%Y-%m-%d")

    # Bronze sensor GZIP files contain ALL container readings concatenated on a
    # single line with NO newlines between JSON objects (Kinesis Firehose behaviour).
    # spark.read.json() with multiline=false treats each file as one "line" and
    # parses only the FIRST JSON object → ~1 record per file instead of ~10,937.
    #
    # Fix: list files via boto3 (driver), read raw bytes with sc.binaryFiles(),
    # then parse every JSON object with _parse_concatenated_gz on executors.
    s3_prefix = f"sensor-readings/year={y}/month={m:02d}/day={d:02d}/"
    logger.info("Listing Bronze sensor files for %s under s3://%s/%s",
                date_str, DATA_LAKE_BUCKET, s3_prefix)

    paginator = s3.get_paginator("list_objects_v2")
    s3_file_uris: list[str] = []
    for page in paginator.paginate(Bucket=DATA_LAKE_BUCKET, Prefix=s3_prefix):
        for obj in page.get("Contents", []):
            s3_file_uris.append(f"s3://{DATA_LAKE_BUCKET}/{obj['Key']}")

    logger.info("Found %d Bronze sensor files for %s", len(s3_file_uris), date_str)

    if not s3_file_uris:
        logger.warning("No Bronze sensor files found for %s — returning empty aggregations", date_str)
        empty_agg = spark.createDataFrame(
            [],
            schema=(
                "container_id string, readings long, avg_fill double, max_fill double,"
                " min_fill double, avg_battery double, min_battery double,"
                " avg_temp double, max_temp double"
            ),
        )
        empty_hourly = spark.createDataFrame(
            [],
            schema="hour_of_day int, avg_fill double, containers long",
        )
        return empty_agg, empty_hourly

    # Read raw bytes per file and parse ALL concatenated JSON objects from each
    raw_rdd = sc.binaryFiles(",".join(s3_file_uris))
    json_lines_rdd = raw_rdd.flatMap(_parse_concatenated_gz)
    logger.info("Parsing Bronze GZIP files with concatenated-JSON streaming decoder")

    df = (
        spark.read
        .json(json_lines_rdd)
        .select(
            F.col("container_id").cast(StringType()),
            F.col("timestamp").cast(StringType()),
            F.col("fill_level").cast(DoubleType()),
            F.col("battery").cast(DoubleType()),
            F.col("temperature").cast(DoubleType()),
            F.col("latitude").cast(DoubleType()),
            F.col("longitude").cast(DoubleType()),
        )
        .filter(F.col("container_id").isNotNull())
        .cache()
    )

    row_count = df.count()
    logger.info("Bronze: %d rows loaded for %s", row_count, date_str)

    if row_count == 0:
        logger.warning("No Bronze data found for %s — returning empty aggregations", date_str)
        df.unpersist()
        empty_agg = spark.createDataFrame(
            [],
            schema=(
                "container_id string, readings long, avg_fill double, max_fill double,"
                " min_fill double, avg_battery double, min_battery double,"
                " avg_temp double, max_temp double"
            ),
        )
        empty_hourly = spark.createDataFrame(
            [],
            schema="hour_of_day int, avg_fill double, containers long",
        )
        return empty_agg, empty_hourly

    # ── Write Silver Parquet (overwrite allows re-runs to be idempotent) ──
    silver_path = f"s3://{DATA_LAKE_BUCKET}/sensor-readings-parquet/date={date_str}/"
    logger.info("Writing Silver Parquet to %s", silver_path)
    df.write.mode("overwrite").parquet(silver_path)
    logger.info("Silver Parquet written successfully")

    # ── Container aggregations (remain in Spark, collected later) ──
    container_aggs = df.groupBy("container_id").agg(
        F.count("*").alias("readings"),
        F.avg("fill_level").alias("avg_fill"),
        F.max("fill_level").alias("max_fill"),
        F.min("fill_level").alias("min_fill"),
        F.avg("battery").alias("avg_battery"),
        F.min("battery").alias("min_battery"),
        F.avg("temperature").alias("avg_temp"),
        F.max("temperature").alias("max_temp"),
    )

    # ── Hourly fill pattern ──
    hourly_pattern = (
        df
        .withColumn("hour_of_day", F.hour(F.to_timestamp("timestamp")))
        .filter(F.col("hour_of_day").isNotNull())
        .groupBy("hour_of_day").agg(
            F.avg("fill_level").alias("avg_fill"),
            F.countDistinct("container_id").alias("containers"),
        )
        .orderBy("hour_of_day")
    )

    df.unpersist()
    return container_aggs, hourly_pattern


# ─────────────────────────────────────────────────────────
# Step 4: Route efficiency from S3 Bronze (Spark)
# ─────────────────────────────────────────────────────────

def load_route_results_spark(date: datetime) -> DataFrame | None:
    """
    Read route result NDJSON GZIP files from S3 Bronze (written by Kinesis Firehose).
    Firehose uses Hive partitioning: year=Y/month=MM/day=DD/hour=HH/

    Also writes Silver Parquet for efficient future Athena ad-hoc queries.
    """
    y, m, d = date.year, date.month, date.day
    date_str = date.strftime("%Y-%m-%d")

    # Read all route Bronze files for the day using the day-level path.
    # Using a single day-level prefix + recursiveFileLookup avoids failures when
    # individual hour= subdirectories don't exist (Firehose only creates hour
    # directories when there are records to flush — unlike sensor data which
    # writes all 24 hours, routes may only produce 1–3 files/day).
    day_path = f"s3://{DATA_LAKE_BUCKET}/route-results/year={y}/month={m:02d}/day={d:02d}/"
    logger.info("Reading route-results Bronze for %s from %s", date_str, day_path)

    try:
        df = (
            spark.read
            .option("recursiveFileLookup", "true")
            .option("multiline", "false")
            .json(day_path)
            # Filter by the `date` field inside each record (set by the route-optimizer
            # at creation time). This is critical when Firehose buffers routes from
            # multiple days into a single flush (e.g., the backfill script, or routes
            # generated just before midnight that flush into the next day's partition).
            .filter(F.col("date") == date_str)
            .filter(F.col("circuit_id").isNotNull())
            .cache()
        )
        count = df.count()
        logger.info("Route-results Bronze: %d records with date=%s", count, date_str)

        if count == 0:
            df.unpersist()
            return None

        # ── Bronze → Silver Parquet ──
        silver_path = f"s3://{DATA_LAKE_BUCKET}/route-results-parquet/date={date_str}/"
        logger.info("Writing route-results Silver Parquet to %s", silver_path)
        df.write.mode("overwrite").parquet(silver_path)
        logger.info("Route-results Silver Parquet written successfully")

        df.unpersist()

        # Return from Silver (Parquet read is faster for subsequent aggregations)
        return spark.read.parquet(silver_path)

    except Exception as exc:
        logger.warning("No route-results Bronze found for %s: %s", date_str, exc)
        return None


def compute_route_efficiency_spark(
    routes_df: DataFrame | None,
    container_meta: dict[str, dict],
) -> dict:
    """
    Aggregate route results into efficiency statistics using Spark groupBy/agg.
    Multiple truck routes per circuit are summed (optimized) / maxed (baseline).
    """
    _empty: dict = {
        "summary": {
            "circuits_with_routes": 0,
            "avg_distance_improvement_pct": 0.0,
            "total_distance_saved_km": 0.0,
            "avg_duration_improvement_pct": 0.0,
            "total_duration_saved_min": 0.0,
            "total_stops_skipped": 0,
        },
        "by_circuit": [], "by_zone": [], "by_shift": [],
        "top_improving": [], "needs_attention": [],
    }

    if routes_df is None:
        return _empty

    # Build circuit → zone/shift lookup and broadcast it
    circuit_zone:  dict[str, str] = {}
    circuit_shift: dict[str, str] = {}
    for meta in container_meta.values():
        cid = meta.get("circuit_id", "")
        if cid:
            circuit_zone[cid]  = meta.get("zone", "unknown")
            circuit_shift[cid] = meta.get("shift", "unknown")

    zone_bc  = sc.broadcast(circuit_zone)
    shift_bc = sc.broadcast(circuit_shift)

    @F.udf(StringType())
    def get_zone(circuit_id: str | None) -> str:
        return zone_bc.value.get(circuit_id or "", _circuit_to_zone(circuit_id or ""))

    @F.udf(StringType())
    def get_shift(circuit_id: str | None) -> str:
        return shift_bc.value.get(circuit_id or "", "unknown")

    # The route-optimizer runs every 15 minutes, so a single circuit can have
    # dozens of route records per day (one set of truck routes per run).
    # We want the LATEST run per circuit (most recent optimization snapshot),
    # not the cumulative sum of all runs. Strategy: find the max created_at per
    # circuit, then keep only routes that share the same minute as that max.
    # All trucks dispatched in the same optimizer run have created_at within
    # a few hundred milliseconds of each other, so minute-level grouping is safe.
    # Note: created_at is ISO 8601 ("2026-04-05T00:50:25.043671+00:00") which
    # sorts correctly as a string — we use the first 16 chars (YYYY-MM-DDTHH:MM).
    circuit_window = Window.partitionBy("circuit_id")
    latest_routes_df = (
        routes_df
        .filter(F.col("circuit_id").isNotNull())
        .withColumn("ts_minute", F.substring("created_at", 1, 16))
        .withColumn("max_ts_minute", F.max("ts_minute").over(circuit_window))
        .filter(F.col("ts_minute") == F.col("max_ts_minute"))
        .drop("ts_minute", "max_ts_minute")
    )

    circuit_agg = (
        latest_routes_df
        .groupBy("circuit_id").agg(
            F.sum("total_distance_m").alias("opt_dist_m"),
            F.sum("total_duration_s").alias("opt_dur_s"),
            F.sum("optimized_stops").alias("opt_stops"),
            F.max("baseline_distance_m").alias("base_dist_m"),
            F.max("baseline_duration_s").alias("base_dur_s"),
            F.max("baseline_stops").alias("base_stops"),
        )
        .filter(F.col("base_dist_m") > 0)
        .withColumn("zone",  get_zone(F.col("circuit_id")))
        .withColumn("shift", get_shift(F.col("circuit_id")))
        .withColumn(
            "dist_pct",
            F.round(
                (F.col("base_dist_m") - F.col("opt_dist_m")) / F.col("base_dist_m") * 100, 1
            ),
        )
        .withColumn(
            "dur_pct",
            F.when(
                F.col("base_dur_s") > 0,
                F.round(
                    (F.col("base_dur_s") - F.col("opt_dur_s")) / F.col("base_dur_s") * 100, 1
                ),
            ).otherwise(0.0),
        )
        .withColumn(
            "stops_skipped",
            F.greatest(F.lit(0), F.col("base_stops") - F.col("opt_stops")),
        )
    )

    # Per-circuit rows (best → worst improvement)
    by_circuit_rows = circuit_agg.orderBy(F.desc("dist_pct")).collect()

    by_circuit: list[dict] = [
        {
            "circuit_id":               r["circuit_id"],
            "zone":                     r["zone"],
            "shift":                    r["shift"],
            "baseline_distance_km":     round(float(r["base_dist_m"]) / 1000, 2),
            "optimized_distance_km":    round(float(r["opt_dist_m"])  / 1000, 2),
            "distance_improvement_pct": float(r["dist_pct"]),
            "baseline_duration_min":    round(float(r["base_dur_s"]) / 60, 1),
            "optimized_duration_min":   round(float(r["opt_dur_s"])  / 60, 1),
            "duration_improvement_pct": float(r["dur_pct"]),
            "baseline_stops":           int(r["base_stops"]),
            "optimized_stops":          int(r["opt_stops"]),
            "stops_skipped":            int(r["stops_skipped"]),
        }
        for r in by_circuit_rows
    ]

    if not by_circuit:
        return _empty

    # Zone aggregation
    by_zone = [
        {
            "zone":                         r["zone"],
            "circuits":                     int(r["circuits"]),
            "avg_distance_improvement_pct": round(float(r["avg_dist_pct"]), 1),
            "total_baseline_km":            round(float(r["total_base_m"]) / 1000, 1),
            "total_optimized_km":           round(float(r["total_opt_m"])  / 1000, 1),
            "total_saved_km": round(
                (float(r["total_base_m"]) - float(r["total_opt_m"])) / 1000, 1
            ),
        }
        for r in circuit_agg.groupBy("zone").agg(
            F.count("circuit_id").alias("circuits"),
            F.avg("dist_pct").alias("avg_dist_pct"),
            F.sum("base_dist_m").alias("total_base_m"),
            F.sum("opt_dist_m").alias("total_opt_m"),
        ).orderBy("zone").collect()
    ]

    # Shift aggregation
    by_shift = [
        {
            "shift":                        r["shift"],
            "circuits":                     int(r["circuits"]),
            "avg_distance_improvement_pct": round(float(r["avg_dist_pct"]), 1),
            "total_baseline_km":            round(float(r["total_base_m"]) / 1000, 1),
            "total_optimized_km":           round(float(r["total_opt_m"])  / 1000, 1),
            "total_saved_km": round(
                (float(r["total_base_m"]) - float(r["total_opt_m"])) / 1000, 1
            ),
        }
        for r in circuit_agg.groupBy("shift").agg(
            F.count("circuit_id").alias("circuits"),
            F.avg("dist_pct").alias("avg_dist_pct"),
            F.sum("base_dist_m").alias("total_base_m"),
            F.sum("opt_dist_m").alias("total_opt_m"),
        ).orderBy("shift").collect()
    ]

    all_dist_pct = [c["distance_improvement_pct"] for c in by_circuit]
    all_dur_pct  = [c["duration_improvement_pct"]  for c in by_circuit]
    total_base_km  = sum(c["baseline_distance_km"]  for c in by_circuit)
    total_opt_km   = sum(c["optimized_distance_km"] for c in by_circuit)
    total_base_min = sum(c["baseline_duration_min"] for c in by_circuit)
    total_opt_min  = sum(c["optimized_duration_min"] for c in by_circuit)

    return {
        "summary": {
            "circuits_with_routes":         len(by_circuit),
            "avg_distance_improvement_pct": round(sum(all_dist_pct) / len(all_dist_pct), 1),
            "total_distance_saved_km":      round(total_base_km - total_opt_km, 1),
            "avg_duration_improvement_pct": round(sum(all_dur_pct) / len(all_dur_pct), 1),
            "total_duration_saved_min":     round(total_base_min - total_opt_min, 1),
            "total_stops_skipped":          sum(c["stops_skipped"] for c in by_circuit),
        },
        "by_circuit":      by_circuit,
        "by_zone":         by_zone,
        "by_shift":        by_shift,
        "top_improving":   by_circuit[:15],
        "needs_attention": sorted(by_circuit, key=lambda x: x["distance_improvement_pct"])[:15],
    }


# ─────────────────────────────────────────────────────────
# Step 5: Fill rate predictions (scipy, driver-side)
# Reads Silver Parquet already written in Step 3
# ─────────────────────────────────────────────────────────

def _logistic(t: np.ndarray, k: float, t0: float) -> np.ndarray:
    """Logistic growth curve: f(t) = 100 / (1 + e^{-k(t-t0)})."""
    return 100.0 / (1.0 + np.exp(-k * (t - t0)))


def compute_fill_rate_predictions(
    date: datetime,
    container_meta: dict[str, dict],
) -> list[dict]:
    """
    Read Silver Parquet for the given date (written earlier in the same job),
    collect per-container time series to the driver (limited sample),
    fit logistic curves, and return predictions sorted by urgency.
    """
    date_str = date.strftime("%Y-%m-%d")
    silver_path = f"s3://{DATA_LAKE_BUCKET}/sensor-readings-parquet/date={date_str}/"

    try:
        # Collect only containers with enough variation — avoids downloading everything
        ts_df = (
            spark.read.parquet(silver_path)
            .select("container_id", "timestamp", "fill_level")
            .filter(
                F.col("container_id").isNotNull()
                & F.col("timestamp").isNotNull()
                & F.col("fill_level").isNotNull()
            )
            .groupBy("container_id")
            .agg(
                F.collect_list(F.struct("timestamp", "fill_level")).alias("readings"),
                F.stddev("fill_level").alias("fill_std"),
                F.count("*").alias("cnt"),
            )
            .filter((F.col("cnt") >= 4) & (F.col("fill_std") > 1.0))
            .limit(500)
            .collect()
        )
    except Exception as exc:
        logger.warning("Could not read Silver Parquet for predictions: %s", exc)
        return []

    predictions: list[dict] = []

    for row in ts_df:
        cid = row["container_id"]
        readings = sorted(row["readings"], key=lambda r: r["timestamp"])
        points: list[tuple[float, float]] = []
        for r in readings:
            fill = r["fill_level"]
            ts_str = r["timestamp"]
            if fill is None or ts_str is None:
                continue
            try:
                dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                points.append((dt.hour + dt.minute / 60.0, float(fill)))
            except (ValueError, AttributeError):
                continue

        if len(points) < 4:
            continue

        t_arr = np.array([p[0] for p in points])
        f_arr = np.array([p[1] for p in points])

        if np.std(f_arr) < 1.0:
            continue

        try:
            popt, _ = curve_fit(
                _logistic, t_arr, f_arr,
                p0=[0.5, 12.0],
                bounds=([0.01, 0.0], [5.0, 48.0]),
                maxfev=2000,
            )
            k, t0 = popt
        except (RuntimeError, ValueError):
            continue

        current_fill = float(f_arr[-1])
        current_hour = float(t_arr[-1])

        if current_fill >= 80:
            hours_to_80 = 0.0
        else:
            try:
                t_80 = t0 - np.log(100.0 / 80.0 - 1.0) / k
                hours_to_80 = max(0.0, t_80 - current_hour)
            except (ValueError, ZeroDivisionError):
                continue

        fill_rate = k * current_fill * (1.0 - current_fill / 100.0)
        meta = container_meta.get(cid, {})

        predictions.append({
            "container_id":           cid,
            "circuit_id":             meta.get("circuit_id", ""),
            "current_fill":           round(current_fill, 1),
            "fill_rate_pct_per_hour": round(fill_rate, 2),
            "predicted_hours_to_80":  round(hours_to_80, 1),
        })

    predictions.sort(key=lambda x: x["predicted_hours_to_80"])
    logger.info("Computed %d fill rate predictions", len(predictions))
    return predictions


# ─────────────────────────────────────────────────────────
# Step 5b: Route efficiency trends (last 30 days, driver-side)
# Reads Silver Parquet already written in Steps 3+4
# ─────────────────────────────────────────────────────────

def compute_route_efficiency_trends(date: datetime) -> list[dict]:
    """
    Read last 30 days of route-results Silver Parquet.
    Returns per-circuit daily efficiency for historical trend charts.
    Written to analytics-results/route-efficiency-trends.json.
    """
    trends: list[dict] = []

    for days_back in range(0, 30):  # 0 = current day, up to 29 days back (30 days total)
        d = date - timedelta(days=days_back)
        date_str = d.strftime("%Y-%m-%d")
        silver_path = f"s3://{DATA_LAKE_BUCKET}/route-results-parquet/date={date_str}/"

        try:
            df = spark.read.parquet(silver_path)
            # Keep only the latest run per circuit (same dedup as in compute_route_efficiency_spark)
            w = Window.partitionBy("circuit_id")
            df_latest = (
                df.filter(F.col("circuit_id").isNotNull() & (F.col("baseline_distance_m") > 0))
                .withColumn("ts_minute", F.substring("created_at", 1, 16))
                .withColumn("max_ts_minute", F.max("ts_minute").over(w))
                .filter(F.col("ts_minute") == F.col("max_ts_minute"))
                .drop("ts_minute", "max_ts_minute")
            )
            agg_rows = (
                df_latest
                .groupBy("circuit_id")
                .agg(
                    F.avg("distance_improvement_pct").alias("avg_dist_pct"),
                    F.avg("duration_improvement_pct").alias("avg_dur_pct"),
                    # baseline_distance_m / baseline_duration_s are circuit-level values
                    # (same on every truck record). Use max() to get the circuit baseline.
                    # total_distance_m / total_duration_s are per-truck values — sum all trucks.
                    F.max("baseline_distance_m").alias("total_base_m"),
                    F.sum("total_distance_m").alias("total_opt_m"),
                    F.max("baseline_duration_s").alias("total_base_s"),
                    F.sum("total_duration_s").alias("total_opt_s"),
                    F.sum("stops_skipped").alias("stops_skipped"),
                    F.count("route_id").alias("routes"),
                )
                .collect()
            )
        except Exception as exc:
            logger.debug("No Silver Parquet for route trends on %s: %s", date_str, exc)
            continue

        for r in agg_rows:
            base_m    = float(r["total_base_m"] or 0)
            opt_m     = float(r["total_opt_m"]  or 0)
            base_s    = float(r["total_base_s"] or 0)
            opt_s     = float(r["total_opt_s"]  or 0)
            avg_dist_pct = float(r["avg_dist_pct"] or 0)
            avg_dur_pct  = float(r["avg_dur_pct"]  or 0)
            # For multi-truck circuits, sum(truck_opt_distances) can exceed the
            # circuit baseline (each truck makes a depot round-trip adding overhead).
            # max(baseline) - sum(opt) would be negative, which is misleading.
            # Instead, use avg_improvement_pct × baseline to get a per-truck-normalized
            # savings figure that is always positive and scales with circuit size.
            distance_saved_km = round(avg_dist_pct / 100.0 * base_m / 1000, 2)
            duration_saved_min = round(avg_dur_pct  / 100.0 * base_s / 60,   1)
            trends.append({
                "circuit_id":               str(r["circuit_id"]),
                "date":                     date_str,
                "distance_improvement_pct": round(avg_dist_pct, 1),
                "duration_improvement_pct": round(avg_dur_pct,  1),
                "distance_saved_km":        distance_saved_km,
                "duration_saved_min":       duration_saved_min,
                "baseline_distance_km":     round(base_m / 1000, 2),
                "optimized_distance_km":    round(opt_m  / 1000, 2),
                "stops_skipped":            int(r["stops_skipped"] or 0),
                "routes":                   int(r["routes"]),
            })

    logger.info("Computed %d circuit-day route efficiency trends (last 30 days)", len(trends))
    return sorted(trends, key=lambda x: (x["circuit_id"], x["date"]))


# ─────────────────────────────────────────────────────────
# Step 6: Assemble analytics JSON (driver-side)
# ─────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(val)) if val is not None else default
    except (ValueError, TypeError):
        return default


def build_analytics_json(
    date_str: str,
    container_meta: dict[str, dict],
    container_aggs_df: DataFrame,
    hourly_pattern_df: DataFrame,
    predictions: list[dict],
    route_efficiency: dict,
) -> dict:
    """Collect Spark DataFrames to driver and assemble the full analytics JSON."""

    # ── Collect container aggregations ──
    agg_rows = container_aggs_df.collect()
    enriched: list[dict] = []
    for row in agg_rows:
        cid = str(row["container_id"]) if row["container_id"] else ""
        meta = container_meta.get(cid, {})
        enriched.append({
            "container_id":  cid,
            "circuit_id":    meta.get("circuit_id", ""),
            "zone":          meta.get("zone", "unknown"),
            "shift":         meta.get("shift", ""),
            "latitude":      meta.get("latitude", 0),
            "longitude":     meta.get("longitude", 0),
            "readings":      _safe_int(row["readings"]),
            "avg_fill":      round(_safe_float(row["avg_fill"]), 1),
            "max_fill":      round(_safe_float(row["max_fill"]), 1),
            "min_fill":      round(_safe_float(row["min_fill"]), 1),
            "avg_battery":   round(_safe_float(row["avg_battery"]), 1),
            "min_battery":   round(_safe_float(row["min_battery"]), 1),
            "avg_temp":      round(_safe_float(row["avg_temp"]), 1),
            "max_temp":      round(_safe_float(row["max_temp"]), 1),
        })

    # ── Aggregate by circuit ──
    by_circuit_map: dict[str, dict] = {}
    for c in enriched:
        cid = c["circuit_id"]
        if not cid:
            continue
        if cid not in by_circuit_map:
            by_circuit_map[cid] = {
                "circuit_id": cid, "zone": c["zone"], "shift": c["shift"],
                "fills": [], "batteries": [], "containers_reporting": 0,
                "overflow_count": 0, "readings": 0,
            }
        entry = by_circuit_map[cid]
        entry["fills"].append(c["avg_fill"])
        entry["batteries"].append(c["avg_battery"])
        entry["containers_reporting"] += 1
        entry["readings"] += c["readings"]
        if c["max_fill"] > 90:
            entry["overflow_count"] += 1

    by_circuit: list[dict] = []
    for cid, entry in sorted(by_circuit_map.items()):
        fills    = entry["fills"]
        batteries = entry["batteries"]
        avg_fill     = round(sum(fills) / len(fills), 1)         if fills     else 0
        median_fill  = round(sorted(fills)[len(fills) // 2], 1)  if fills     else 0
        max_fill     = round(max(fills), 1)                      if fills     else 0
        avg_battery  = round(sum(batteries) / len(batteries), 1) if batteries else 0

        circuit_preds = [p for p in predictions if p["circuit_id"] == cid]
        avg_rate = avg_hours = 0.0
        if circuit_preds:
            avg_rate  = round(sum(p["fill_rate_pct_per_hour"] for p in circuit_preds) / len(circuit_preds), 2)
            avg_hours = round(sum(p["predicted_hours_to_80"]  for p in circuit_preds) / len(circuit_preds), 1)

        by_circuit.append({
            "circuit_id":               cid,
            "zone":                     entry["zone"],
            "shift":                    entry["shift"],
            "avg_fill_level":           avg_fill,
            "max_fill_level":           max_fill,
            "median_fill_level":        median_fill,
            "containers_reporting":     entry["containers_reporting"],
            "overflow_count":           entry["overflow_count"],
            "avg_battery":              avg_battery,
            "avg_fill_rate_pct_per_hour": avg_rate,
            "predicted_hours_to_full":  avg_hours,
        })

    # ── Aggregate by zone ──
    zone_map: dict[str, dict] = defaultdict(lambda: {"fills": [], "count": 0})
    for c in enriched:
        zone_map[c["zone"]]["fills"].append(c["avg_fill"])
        zone_map[c["zone"]]["count"] += 1
    by_zone = [
        {
            "zone":             z,
            "avg_fill_level":   round(sum(v["fills"]) / len(v["fills"]), 1) if v["fills"] else 0,
            "containers":       v["count"],
        }
        for z, v in sorted(zone_map.items())
    ]

    # ── Aggregate by shift ──
    shift_map: dict[str, dict] = defaultdict(lambda: {"circuits": set(), "fills": []})
    for c in enriched:
        shift_map[c["shift"]]["circuits"].add(c["circuit_id"])
        shift_map[c["shift"]]["fills"].append(c["avg_fill"])
    by_shift = [
        {
            "shift":          s,
            "avg_fill_level": round(sum(v["fills"]) / len(v["fills"]), 1) if v["fills"] else 0,
            "circuits":       len(v["circuits"]),
        }
        for s, v in sorted(shift_map.items())
    ]

    # ── Collect hourly pattern ──
    hourly = sorted(
        [
            {
                "hour":           _safe_int(r["hour_of_day"]),
                "avg_fill_level": round(_safe_float(r["avg_fill"]), 1),
                "containers":     _safe_int(r["containers"]),
            }
            for r in hourly_pattern_df.collect()
        ],
        key=lambda x: x["hour"],
    )

    hotspots = sorted(by_circuit, key=lambda x: x["avg_fill_level"], reverse=True)[:20]

    heatmap_data = [
        [c["latitude"], c["longitude"], round(c["avg_fill"] / 100.0, 2)]
        for c in enriched
        if c["latitude"] != 0 and c["longitude"] != 0
    ]

    battery_alerts = sorted(
        [
            {"container_id": c["container_id"], "circuit_id": c["circuit_id"], "min_battery": c["min_battery"]}
            for c in enriched if 0 < c["min_battery"] < 20
        ],
        key=lambda x: x["min_battery"],
    )
    temperature_alerts = sorted(
        [
            {"container_id": c["container_id"], "circuit_id": c["circuit_id"],
             "temperature": c["max_temp"], "lat": c["latitude"], "lon": c["longitude"]}
            for c in enriched if c["max_temp"] > 50
        ],
        key=lambda x: x["temperature"], reverse=True,
    )

    total_readings = sum(c["readings"] for c in enriched)
    all_fills = [c["avg_fill"] for c in enriched]
    avg_fill_level = round(sum(all_fills) / len(all_fills), 1) if all_fills else 0

    return {
        "generated_at":             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date":                     date_str,
        "summary": {
            "total_readings":           total_readings,
            "containers_reporting":     len(enriched),
            "avg_fill_level":           avg_fill_level,
            "containers_overflowing":   sum(1 for c in enriched if c["max_fill"] > 90),
            "containers_underutilized": sum(1 for c in enriched if c["avg_fill"] < 10),
            "battery_alerts":           len(battery_alerts),
            "temperature_alerts":       len(temperature_alerts),
        },
        "by_circuit":         by_circuit,
        "by_zone":            by_zone,
        "by_shift":           by_shift,
        "hourly_pattern":     hourly,
        "hotspots":           [{"circuit_id": h["circuit_id"], "avg_fill_level": h["avg_fill_level"], "overflow_count": h["overflow_count"]} for h in hotspots],
        "heatmap_data":       heatmap_data,
        "battery_alerts":     battery_alerts[:50],
        "temperature_alerts": temperature_alerts[:20],
        "predictions":        predictions[:100],
        "route_efficiency":   route_efficiency,
    }


# ─────────────────────────────────────────────────────────
# Step 7: Write results to S3 (driver-side)
# ─────────────────────────────────────────────────────────

def write_json_to_s3(key: str, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    s3.put_object(
        Bucket=DATA_LAKE_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Wrote s3://%s/%s (%d bytes)", DATA_LAKE_BUCKET, key, len(body))


def update_trends(date_str: str, by_circuit: list[dict]) -> None:
    """Append today's circuit averages to the rolling 30-day trends file."""
    key = "analytics-results/trends/latest-trends.json"
    existing: list[dict] = []
    try:
        obj = s3.get_object(Bucket=DATA_LAKE_BUCKET, Key=key)
        existing = json.loads(obj["Body"].read())
    except Exception as e:
        logger.warning("Could not read existing trends: %s", e)

    for c in by_circuit:
        existing.append({
            "circuit_id":     c["circuit_id"],
            "date":           date_str,
            "avg_fill_level": c["avg_fill_level"],
        })

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    existing = [t for t in existing if t["date"] >= cutoff]
    write_json_to_s3(key, existing)


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting daily analytics ETL (PySpark, Glue 4.0)")
    logger.info(
        "Config: bucket=%s, containers=%s, routes=%s",
        DATA_LAKE_BUCKET, CONTAINERS_TABLE, ROUTES_TABLE,
    )

    if _RUN_DATE_OVERRIDE:
        yesterday = datetime.strptime(_RUN_DATE_OVERRIDE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        logger.info("Processing date: %s (overridden via RUN_DATE arg)", _RUN_DATE_OVERRIDE)
    else:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    logger.info("Processing date: %s", date_str)

    # Step 1: Container metadata (driver-side boto3)
    container_meta = load_container_metadata()

    # Steps 2+3: Bronze → Silver Parquet + Spark aggregations
    container_aggs_df, hourly_pattern_df = process_sensor_data(yesterday)

    # Step 4: Route efficiency from S3 Bronze route-results (Spark)
    routes_df = load_route_results_spark(yesterday)
    route_efficiency = compute_route_efficiency_spark(routes_df, container_meta)
    logger.info(
        "Route efficiency: %d circuits, avg improvement %.1f%%",
        route_efficiency["summary"]["circuits_with_routes"],
        route_efficiency["summary"]["avg_distance_improvement_pct"],
    )

    # Step 5: Fill rate predictions (scipy on driver, reads Silver Parquet)
    predictions = compute_fill_rate_predictions(yesterday, container_meta)

    # Step 5b: Route efficiency trends (last 30 days, reads Silver Parquet per day)
    route_efficiency_trends = compute_route_efficiency_trends(yesterday)

    # Step 6: Assemble Gold JSON (collects Spark results to driver)
    analytics = build_analytics_json(
        date_str, container_meta,
        container_aggs_df, hourly_pattern_df,
        predictions, route_efficiency,
    )

    # Step 7: Write to S3
    write_json_to_s3("analytics-results/latest.json", analytics)
    write_json_to_s3(f"analytics-results/daily/{date_str}.json", analytics)
    write_json_to_s3("analytics-results/route-efficiency-trends.json", route_efficiency_trends)
    update_trends(date_str, analytics["by_circuit"])

    logger.info("Daily analytics ETL completed successfully for %s", date_str)


main()
job.commit()
