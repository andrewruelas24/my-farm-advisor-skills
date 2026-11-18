#!/usr/bin/env python3
"""Audit selected farm boundaries and create static acreage summaries.

This is the boundary-first stage of the field-level EDA workflow. It reads
canonical runtime files and writes outputs only to an external runtime path.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DEFAULT_GROWERS = [
    "assignment-illinois-grower",
    "assignment-iowa-grower",
    "assignment-nebraska-grower",
]
ACRES_PER_HECTARE = 2.47105
BIN_WIDTH_ACRES = 50
REPO_ROOT = Path(__file__).resolve().parents[4]


class DataQualityError(RuntimeError):
    """Raised when canonical field identity or required data is unusable."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataQualityError(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataQualityError(f"Expected a JSON object in {path}")
    return value


def _coordinate_pairs(value: Any) -> Iterable[tuple[float, float]]:
    """Yield coordinate pairs from GeoJSON coordinates, without changing them."""
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _coordinate_pairs(child)


def _load_boundary(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    data = _read_json(path)
    if data.get("type") != "FeatureCollection":
        raise DataQualityError(f"{path} is not a GeoJSON FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise DataQualityError(f"{path} has no features")

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, feature in enumerate(features):
        properties = feature.get("properties") or {}
        field_id = str(properties.get("field_id", "")).strip()
        if not field_id:
            errors.append(f"Feature {index} has no field_id")
            continue
        acres = pd.to_numeric(properties.get("area_acres"), errors="coerce")
        hectares = pd.to_numeric(properties.get("area_ha_est"), errors="coerce")
        if pd.isna(acres) or not math.isfinite(float(acres)) or float(acres) <= 0:
            errors.append(f"{field_id}: area_acres is missing, non-finite, or non-positive")
        coordinates = list(_coordinate_pairs((feature.get("geometry") or {}).get("coordinates")))
        if not coordinates:
            errors.append(f"{field_id}: geometry has no coordinate pairs")
        elif any(
            not (-180 <= lon <= 180 and -90 <= lat <= 90)
            for lon, lat in coordinates
        ):
            errors.append(f"{field_id}: coordinates are outside lon/lat ranges")

        acres_value = float(acres) if not pd.isna(acres) else float("nan")
        hectares_value = float(hectares) if not pd.isna(hectares) else float("nan")
        conversion_delta = (
            acres_value - hectares_value * ACRES_PER_HECTARE
            if math.isfinite(acres_value) and math.isfinite(hectares_value)
            else float("nan")
        )
        records.append(
            {
                "field_id": field_id,
                "field_slug": "",
                "area_acres": acres_value,
                "area_ha_est": hectares_value,
                "area_conversion_delta_acres": conversion_delta,
                "country": properties.get("country", ""),
                "state": properties.get("state", ""),
                "state_fips": properties.get("state_fips", ""),
                "county": properties.get("county", ""),
                "crop_attribute": properties.get("crop", ""),
                "source": properties.get("source", ""),
                "csb_id": properties.get("csb_id", ""),
            }
        )

    ids = [record["field_id"] for record in records]
    duplicates = sorted(field_id for field_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"Duplicate field_id values: {', '.join(duplicates)}")
    if errors:
        raise DataQualityError("; ".join(errors))
    return data, records, errors


def _load_inventory(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        raise DataQualityError(f"Cannot read inventory {path}: {exc}") from exc
    if not rows or not {"field_id", "field_slug"}.issubset(rows[0]):
        raise DataQualityError(
            f"{path} must have field_id and field_slug columns and at least one row"
        )
    ids = [str(row["field_id"]).strip() for row in rows]
    if len(ids) != len(set(ids)):
        raise DataQualityError(f"{path} contains duplicate field_id values")
    return rows


def _farm_dirs(grower_dir: Path) -> list[Path]:
    farms_dir = grower_dir / "farms"
    if not farms_dir.is_dir():
        raise DataQualityError(f"Missing farms directory: {farms_dir}")
    return sorted(path for path in farms_dir.iterdir() if path.is_dir())


def collect_grower(
    data_root: Path, grower_slug: str
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    grower_dir = data_root / "data-pipeline" / "growers" / grower_slug
    if not grower_dir.is_dir():
        raise DataQualityError(f"Grower directory does not exist: {grower_dir}")
    farms = _farm_dirs(grower_dir)
    if len(farms) != 1:
        raise DataQualityError(
            f"{grower_slug} has {len(farms)} farms; this workflow expects exactly one"
        )
    farm_dir = farms[0]
    farm_json_path = farm_dir / "farm.json"
    farm_metadata = _read_json(farm_json_path)
    boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
    _, records, _ = _load_boundary(boundary_path)
    inventory_path = farm_dir / "manifests" / "field-inventory.csv"
    inventory = _load_inventory(inventory_path)
    boundary_ids = {record["field_id"] for record in records}
    inventory_by_id = {str(row["field_id"]).strip(): row for row in inventory}
    inventory_ids = set(inventory_by_id)
    if boundary_ids != inventory_ids:
        missing_inventory = sorted(boundary_ids - inventory_ids)
        missing_boundary = sorted(inventory_ids - boundary_ids)
        raise DataQualityError(
            f"{grower_slug}/{farm_dir.name}: boundary/inventory IDs differ "
            f"(missing inventory={missing_inventory}; missing boundary={missing_boundary})"
        )

    warnings: list[str] = []
    expected_fields = farm_metadata.get("field_count")
    if expected_fields is not None and int(expected_fields) != len(records):
        warnings.append(
            f"farm.json field_count={expected_fields}, but boundaries contain {len(records)} fields"
        )
    states = sorted({str(record["state"]).strip() for record in records if record["state"]})
    counties = sorted({str(record["county"]).strip() for record in records if record["county"]})
    metadata_state = str(farm_metadata.get("state", "")).strip()
    if len(states) > 1:
        warnings.append(f"Boundary features span multiple states: {states}")
    if metadata_state and states and metadata_state not in states:
        warnings.append(
            f"farm.json state={metadata_state!r} disagrees with boundary state(s) {states}; "
            "labels will use boundary properties"
        )
    metadata_farm_slug = str(farm_metadata.get("farm_slug", "")).strip()
    if metadata_farm_slug and metadata_farm_slug != farm_dir.name:
        warnings.append(
            f"farm.json farm_slug={metadata_farm_slug!r} differs from directory {farm_dir.name!r}"
        )
    display_name = str(farm_metadata.get("display_name", "")).strip()
    if display_name.lower() == "default farm":
        warnings.append("farm.json display_name is the generic value 'Default Farm'")

    provenance_path = farm_dir / "manifests" / "csb_fields_provenance.json"
    if provenance_path.exists():
        provenance = _read_json(provenance_path)
        recorded = {str(value) for value in provenance.get("fields", [])}
        if recorded and recorded != boundary_ids:
            warnings.append(
                "CSB provenance manifest does not enumerate exactly the current boundary IDs "
                f"(recorded {len(recorded)}, current {len(boundary_ids)})"
            )
    else:
        provenance = {}
        warnings.append("CSB field provenance manifest is missing")

    for record in records:
        field_id = record["field_id"]
        field_slug = str(inventory_by_id[field_id]["field_slug"]).strip()
        record["field_slug"] = field_slug
        record["grower_slug"] = grower_slug
        record["farm_slug"] = farm_dir.name
        record["display_name"] = display_name
        individual_path = (
            farm_dir / "fields" / field_slug / "boundary" / "field_boundary.geojson"
        )
        if not individual_path.is_file():
            warnings.append(f"{field_id}: individual boundary file is missing")
            continue
        _, individual_records, _ = _load_boundary(individual_path)
        individual_ids = {row["field_id"] for row in individual_records}
        if individual_ids != {field_id}:
            warnings.append(
                f"{field_id}: individual boundary has field IDs {sorted(individual_ids)}"
            )

    if not states:
        warnings.append("No boundary state property is available")
    if any(not str(record["source"]).strip() for record in records):
        warnings.append("One or more boundary features lack a source property")
    if any(not str(record["csb_id"]).strip() for record in records):
        warnings.append("One or more boundary features lack a csb_id property")

    for record in records:
        record["state_group"] = ", ".join(states) if states else "Unknown state"
        record["county_group"] = ", ".join(counties) if counties else "Unknown county"
    return records, warnings, {"metadata": farm_metadata, "provenance": provenance}


def _require_external_output(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    repo = REPO_ROOT.resolve()
    if resolved == repo or repo in resolved.parents:
        raise DataQualityError(
            f"Generated outputs must stay outside the repository: {resolved}"
        )


def _summary_table(fields: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (grower, farm, state), group in fields.groupby(
        ["grower_slug", "farm_slug", "state_group"], sort=False
    ):
        area = group["area_acres"].dropna()
        rows.append(
            {
                "grower_slug": grower,
                "farm_slug": farm,
                "boundary_state": state,
                "field_count": int(group["field_id"].nunique()),
                "total_acres": round(float(area.sum()), 2),
                "median_acres": round(float(area.median()), 2),
                "min_acres": round(float(area.min()), 2),
                "max_acres": round(float(area.max()), 2),
                "range_acres": round(float(area.max() - area.min()), 2),
            }
        )
    return pd.DataFrame(rows)


def _write_figures(fields: pd.DataFrame, output_dir: Path) -> None:
    groups = list(fields.groupby("grower_slug", sort=False))
    colors = plt.get_cmap("Set2").colors

    fig, ax = plt.subplots(figsize=(10, 6))
    values = [group["area_acres"].to_numpy(dtype=float) for _, group in groups]
    labels = []
    for _, group in groups:
        states = sorted(set(group["state_group"].astype(str)))
        labels.append(f"{group['grower_slug'].iloc[0]}\n{', '.join(states)}")
    box = ax.boxplot(
        values,
        tick_labels=labels,
        patch_artist=True,
        showfliers=False,
        medianprops={"color": "#1f2937", "linewidth": 1.7},
    )
    rng = np.random.default_rng(2026)
    for index, (patch, sample) in enumerate(zip(box["boxes"], values)):
        patch.set_facecolor(colors[index % len(colors)])
        patch.set_alpha(0.55)
        jitter = rng.uniform(-0.07, 0.07, size=len(sample))
        ax.scatter(
            np.full(len(sample), index + 1, dtype=float) + jitter,
            sample,
            s=35,
            color="#263238",
            edgecolor="white",
            linewidth=0.45,
            zorder=3,
        )
    ax.set_title("Field acreage in the selected grower samples")
    ax.set_ylabel("Reported field area (acres)")
    ax.set_xlabel("Grower sample (state from field boundaries)")
    ax.grid(axis="y", alpha=0.25)
    fig.text(
        0.01,
        0.01,
        "One point per unique field. Selected fields only; not statewide estimates.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(output_dir / "boundary_acreage_boxplot.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    min_area = float(fields["area_acres"].min())
    max_area = float(fields["area_acres"].max())
    bin_start = math.floor(min_area / BIN_WIDTH_ACRES) * BIN_WIDTH_ACRES
    bin_stop = math.ceil(max_area / BIN_WIDTH_ACRES) * BIN_WIDTH_ACRES
    if bin_stop <= max_area:
        bin_stop += BIN_WIDTH_ACRES
    edges = np.arange(bin_start, bin_stop + BIN_WIDTH_ACRES, BIN_WIDTH_ACRES)
    counts = {
        grower: np.histogram(group["area_acres"].to_numpy(dtype=float), bins=edges)[0]
        for grower, group in groups
    }
    fig, ax = plt.subplots(figsize=(11, 6))
    bar_width = BIN_WIDTH_ACRES / (len(groups) + 0.1)
    offsets = (np.arange(len(groups)) - (len(groups) - 1) / 2) * bar_width
    for index, (grower, _) in enumerate(groups):
        ax.bar(
            edges[:-1] + BIN_WIDTH_ACRES / 2 + offsets[index],
            counts[grower],
            width=bar_width * 0.92,
            color=colors[index % len(colors)],
            edgecolor="#344054",
            linewidth=0.45,
            label=grower,
        )
    ax.set_title("Field acreage in common 50-acre bins")
    ax.set_xlabel("Reported field area (acres)")
    ax.set_ylabel("Number of fields")
    ax.set_xticks(edges)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(title="Selected grower")
    fig.text(
        0.01,
        0.01,
        "Common bins and scale across growers; each field is counted once.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(
        output_dir / "boundary_acreage_histograms.png", dpi=180, bbox_inches="tight"
    )
    plt.close(fig)


def _write_quality_note(
    output_path: Path,
    fields: pd.DataFrame,
    audits: dict[str, dict[str, Any]],
    warnings: list[str],
    data_root: Path,
    output_dir: Path,
) -> None:
    lines = [
        "# Boundary EDA data-quality and provenance note",
        "",
        f"- Runtime root: `{data_root}`",
        f"- Output directory: `{output_dir}`",
        "- Stage: boundary-only; no downloads, weather aggregation, or CDL recalculation.",
        "- Boundary source: USDA CSB/FIBOA when recorded in field properties and manifests.",
        "- Reported area unit: acres (`area_acres`); hectares are retained for a conversion check.",
        "- CRS: GeoJSON coordinates are checked as longitude/latitude; farm metadata declares EPSG:4326.",
        "- Interpretation: this is a selected set of fields, not a representative statewide sample.",
        "",
        "## Per-grower audit",
        "",
        "| Grower | Farm directory | Fields | Boundary state(s) | farm.json state | Source CRS | Provenance fields / boundary fields |",
        "|---|---|---:|---|---|---|---:|",
    ]
    for grower, group in fields.groupby("grower_slug", sort=False):
        audit = audits[grower]
        metadata = audit["metadata"]
        provenance = audit["provenance"]
        source_crs = provenance.get("file_crs", "not recorded")
        provenance_count = len(provenance.get("fields", []))
        boundary_states = ", ".join(sorted(set(group["state_group"].astype(str))))
        lines.append(
            f"| `{grower}` | `{group['farm_slug'].iloc[0]}` | "
            f"{group['field_id'].nunique()} | {boundary_states} | "
            f"{metadata.get('state', 'not recorded')} | {source_crs} | "
            f"{provenance_count} / {group['field_id'].nunique()} |"
        )
    lines += [
        "",
        "## Warnings",
        "",
    ]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None detected by this stage.")
    lines += [
        "",
        "## Statistical meaning and limitations",
        "",
        "- Each point and histogram count represents one unique boundary `field_id`.",
        "- Summary totals and quantiles describe only the selected fields in this runtime.",
        "- Reported boundary areas are static context; no dated boundary versions were found or compared.",
        "- Source crop attributes and CSB boundaries do not verify current crop, farm ownership, or management.",
        "- Farm-level metadata mismatches are reported; this script does not edit runtime metadata.",
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit farm boundaries and create static field-acreage EDA outputs."
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DATA_PIPELINE_DATA_ROOT"),
        help="Absolute DATA_PIPELINE_DATA_ROOT; may also be set in the environment.",
    )
    parser.add_argument(
        "--growers",
        nargs="+",
        default=DEFAULT_GROWERS,
        help="Grower slugs to include; each must have exactly one farm.",
    )
    parser.add_argument(
        "--output-dir",
        help="External output directory. Defaults to <data-root>/data-pipeline/eda/assignment-2-field-level.",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        choices=["boundaries", "cdl", "weather", "map"],
        default=["boundaries", "cdl", "weather", "map"],
        help="Analysis stages to run, in workflow order.",
    )
    parser.add_argument(
        "--cdl-years",
        default="2019,2020,2021,2022,2023",
        help="Comma-separated CDL years to compare.",
    )
    parser.add_argument(
        "--common-cdl-year",
        type=int,
        default=2023,
        help="Year for the dominant-crop field-count chart.",
    )
    parser.add_argument(
        "--weather-years",
        default="2021,2022,2023,2024,2025",
        help="Comma-separated weather years to compare.",
    )
    parser.add_argument(
        "--season-start",
        default="04-01",
        help="Matched-season start in MM-DD form (default: 04-01).",
    )
    parser.add_argument(
        "--season-end",
        default="10-31",
        help="Matched-season end in MM-DD form (default: 10-31).",
    )
    return parser


def _parse_years(value: str, option: str) -> list[int]:
    try:
        years = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise DataQualityError(f"{option} must be comma-separated integer years") from exc
    if not years or len(years) != len(set(years)):
        raise DataQualityError(f"{option} must contain at least one unique year")
    return years


def _validate_season(start: str, end: str) -> tuple[int, int]:
    try:
        start_month, start_day = (int(part) for part in start.split("-"))
        end_month, end_day = (int(part) for part in end.split("-"))
        pd.Timestamp(year=2024, month=start_month, day=start_day)
        pd.Timestamp(year=2024, month=end_month, day=end_day)
    except (ValueError, TypeError) as exc:
        raise DataQualityError("Season dates must be valid MM-DD values") from exc
    if (start_month, start_day) > (end_month, end_day):
        raise DataQualityError(
            "This US field EDA currently requires a same-calendar-year season window"
        )
    return start_month, end_month


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.data_root:
        raise DataQualityError("Set DATA_PIPELINE_DATA_ROOT or pass --data-root")
    data_root = Path(args.data_root).expanduser().resolve()
    if not data_root.is_absolute() or not (data_root / "data-pipeline").is_dir():
        raise DataQualityError(
            f"Expected an absolute runtime root containing data-pipeline/: {data_root}"
        )
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else data_root / "data-pipeline" / "eda" / "assignment-2-field-level"
    ).resolve()
    _require_external_output(output_dir)
    cdl_years = _parse_years(args.cdl_years, "--cdl-years")
    weather_years = _parse_years(args.weather_years, "--weather-years")
    if args.common_cdl_year not in cdl_years:
        raise DataQualityError("--common-cdl-year must also be included in --cdl-years")
    _validate_season(args.season_start, args.season_end)

    rows: list[dict[str, Any]] = []
    audits: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for grower_slug in args.growers:
        grower_rows, grower_warnings, audit = collect_grower(data_root, grower_slug)
        rows.extend(grower_rows)
        warnings.extend(f"{grower_slug}: {warning}" for warning in grower_warnings)
        audits[grower_slug] = audit
    fields = pd.DataFrame(rows)
    if fields.empty:
        raise DataQualityError("No boundary records were loaded")

    output_dir.mkdir(parents=True, exist_ok=True)
    if "boundaries" in args.stages:
        fields.to_csv(output_dir / "boundary_field_sizes.csv", index=False)
        summary = _summary_table(fields)
        summary.to_csv(output_dir / "boundary_size_summary.csv", index=False)
        _write_figures(fields, output_dir)
    _write_quality_note(
        output_dir / "boundary_data_quality.md",
        fields,
        audits,
        warnings,
        data_root,
        output_dir,
    )
    stage_warnings: list[str] = []
    cdl_warnings: list[str] = []
    weather_warnings: list[str] = []
    weather_metadata: dict[str, Any] = {}
    if "cdl" in args.stages:
        from field_eda.cdl import analyze_cdl

        cdl_warnings = analyze_cdl(
            fields,
            data_root=data_root,
            output_dir=output_dir,
            years=cdl_years,
            common_year=args.common_cdl_year,
        )
        stage_warnings.extend(cdl_warnings)
    if "weather" in args.stages:
        from field_eda.weather import analyze_weather

        weather_warnings, weather_metadata = analyze_weather(
            fields,
            data_root=data_root,
            output_dir=output_dir,
            years=weather_years,
            season_start=args.season_start,
            season_end=args.season_end,
        )
        stage_warnings.extend(weather_warnings)
    if "map" in args.stages:
        from field_eda.context_map import create_context_map

        map_warnings = create_context_map(fields, data_root=data_root, output_dir=output_dir)
        stage_warnings.extend(map_warnings)
    note_lines = [
        "# Assignment 2 field-level EDA data-quality and provenance",
        "",
        f"- Runtime root: `{data_root}`",
        f"- Output directory: `{output_dir}`",
        f"- Requested stages: {', '.join(args.stages)}",
        f"- CDL years: {', '.join(map(str, cdl_years))}; common crop-count year: {args.common_cdl_year}",
        f"- Weather years: {', '.join(map(str, weather_years))}",
        f"- Matched weather season: {args.season_start} through {args.season_end} (inclusive).",
        "- Farm-level state/display-name disagreements are reported; boundary state properties drive labels.",
        "- Selected boundary samples are not statewide estimates or verified ownership parcels.",
    ]
    if weather_metadata:
        note_lines += [
            "",
            "## Weather sampling and units",
            "",
            f"- Unique NASA POWER source grids: {weather_metadata['unique_grid_count']}.",
            "- Source grid IDs are reconstructed with the pipeline's centroid-to-grid assignment; they are not station IDs.",
            "- Units: " + ", ".join(f"{key}={value}" for key, value in weather_metadata["units"].items()) + ".",
            "- Repeated fields on a grid share one weather series and are deduplicated before summaries.",
            f"- Valid precipitation days ranged from {weather_metadata['valid_precip_days_min']} "
            f"to {weather_metadata['valid_precip_days_max']} of {weather_metadata['expected_days']} expected "
            f"for {weather_metadata['complete_rainfall_grid_years']} grid-year summaries.",
        ]
        grid_counts = {
            grower: sum(
                1 for grid_growers in weather_metadata["grid_growers"].values() if grower in grid_growers
            )
            for grower in args.growers
        }
        note_lines.append(
            "- Unique source grids by grower: "
            + ", ".join(f"{grower}={count}" for grower, count in grid_counts.items())
            + "."
        )
        for grid_id, field_ids in weather_metadata["grid_fields"].items():
            note_lines.append(f"- `{grid_id}`: fields {', '.join(field_ids)}.")
    cdl_output = output_dir / "cdl_field_year_comparison.csv"
    if "cdl" in args.stages and cdl_output.exists():
        cdl_result = pd.read_csv(cdl_output)
        valid_rows = cdl_result.loc[cdl_result.get("status", pd.Series(dtype=str)).eq("ok")]
        note_lines += [
            "",
            "## CDL pixel coverage",
            "",
            "- `pct` in the source tables is pixel count divided by all rasterized in-boundary pixels. The source table filters No Data and unknown class labels after computing percentages, so retained class percentages can sum below 100.",
            "- The analysis calculates coverage directly as non-zero mapped pixels divided by all in-boundary raster pixels; class 0 is kept separate from valid classes.",
            f"- Valid CDL field-years: {len(valid_rows)} of {len(cdl_result)} rows.",
        ]
    all_warnings = warnings + stage_warnings
    if all_warnings:
        note_lines += ["", "## Warnings"]
        unique_warnings = list(dict.fromkeys(all_warnings))
        note_lines.extend(f"- {warning}" for warning in unique_warnings)
    else:
        note_lines += ["", "## Warnings", "", "- None detected by the selected stages."]
    (output_dir / "field_eda_data_quality.md").write_text(
        "\n".join(note_lines) + "\n", encoding="utf-8"
    )
    print(f"Audited {fields['field_id'].nunique()} unique fields across {len(audits)} growers.")
    print(f"Boundary outputs: {output_dir}")
    if "cdl" in args.stages:
        print(f"CDL outputs: {output_dir}")
    if "weather" in args.stages:
        print(f"Weather outputs: {output_dir}")
    if "map" in args.stages:
        print(f"Static map: {output_dir / 'grower_field_boundary_context.png'}")
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    for warning in stage_warnings:
        if warning not in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataQualityError as exc:
        print(f"DATA QUALITY ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
