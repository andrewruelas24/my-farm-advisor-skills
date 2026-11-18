"""Field-level USDA CDL composition summaries and static plots."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch
import numpy as np
import pandas as pd
from rasterstats import zonal_stats


CDL_NAMES = {
    0: "No Data",
    1: "Corn",
    5: "Soybeans",
    24: "Winter Wheat",
    28: "Alfalfa",
    36: "Forest",
    38: "Grassland",
    43: "Open Water",
    61: "Fallow/Idle",
    63: "Other",
    176: "Grass/Pasture",
}
COLORS = {
    "Corn": "#e6ab02",
    "Soybeans": "#1b9e77",
    "Winter Wheat": "#a6761d",
    "Alfalfa": "#66a61e",
    "Forest": "#1f78b4",
    "Grassland": "#b2df8a",
    "Grass/Pasture": "#33a02c",
    "Fallow/Idle": "#cab2d6",
    "Open Water": "#6baed6",
    "Other": "#999999",
}


def raster_path(shared_cdl: Path, year: int, state_fips: str) -> Path:
    if year in (2019, 2020):
        return shared_cdl / "rasters" / f"CDL_{year}_{state_fips.zfill(2)}.tif"
    return shared_cdl / "rasters" / f"CDL_{year}_CONUS.tif"


def _table_for_year(tables_dir: Path, year: int) -> Path | None:
    matches = sorted(tables_dir.glob(f"*_{year}_cdl.csv"))
    if len(matches) > 1:
        raise ValueError(f"Multiple CDL tables found for {year} under {tables_dir}")
    return matches[0] if matches else None


def _percent(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 4) if denominator else float("nan")


def _class_name(code: int, observed_names: dict[int, str]) -> str:
    if code == 0:
        return "No Data"
    return observed_names.get(code, CDL_NAMES.get(code, f"CDL code {code}"))


def analyze_cdl(
    fields: pd.DataFrame,
    *,
    data_root: Path,
    output_dir: Path,
    years: list[int],
    common_year: int,
) -> list[str]:
    """Compute per-field/year pixel composition, plots, and comparison CSV."""
    warnings: list[str] = []
    shared_cdl = data_root / "data-pipeline" / "shared" / "cdl"
    farm_data: dict[tuple[str, str], dict[str, Any]] = {}
    code_names = dict(CDL_NAMES)

    for (grower, farm), group in fields.groupby(["grower_slug", "farm_slug"], sort=False):
        farm_dir = data_root / "data-pipeline" / "growers" / grower / "farms" / farm
        tables_dir = farm_dir / "derived" / "tables"
        yearly_tables: dict[int, pd.DataFrame] = {}
        for year in years:
            table_path = _table_for_year(tables_dir, year)
            if table_path is None:
                warnings.append(f"{grower}/{farm}: CDL table for {year} is missing")
                continue
            table = pd.read_csv(table_path)
            required = {"field_id", "year", "crop_code", "crop_name", "pixel_count", "pct"}
            if not required.issubset(table.columns):
                warnings.append(
                    f"{table_path}: missing CDL columns {sorted(required - set(table.columns))}"
                )
                continue
            # Multiple classes per field-year are expected; duplicate class keys are not.
            if table.duplicated(["field_id", "year", "crop_code"]).any():
                warnings.append(f"{table_path}: duplicate field-year-class rows")
            for row in table[["crop_code", "crop_name"]].dropna().itertuples(index=False):
                try:
                    code_names[int(row.crop_code)] = str(row.crop_name)
                except (TypeError, ValueError):
                    continue
            yearly_tables[year] = table
        farm_data[(grower, farm)] = {
            "farm_dir": farm_dir,
            "tables": yearly_tables,
            "fields": group.copy(),
        }

    results: list[dict[str, Any]] = []
    for (grower, farm), content in farm_data.items():
        boundary_path = content["farm_dir"] / "boundary" / "field_boundaries.geojson"
        boundaries = gpd.read_file(boundary_path)
        boundaries["field_id"] = boundaries["field_id"].astype(str)
        records = content["fields"].set_index("field_id", drop=False)
        for year in years:
            table = content["tables"].get(year)
            table_groups = (
                {str(key): value for key, value in table.groupby("field_id", sort=False)}
                if table is not None
                else {}
            )
            for _, boundary in boundaries.iterrows():
                field_id = str(boundary["field_id"])
                if field_id not in records.index:
                    warnings.append(f"{grower}/{farm}: boundary field {field_id} not in audit table")
                    continue
                field_record = records.loc[field_id]
                state_fips = str(field_record.get("state_fips", "")).zfill(2)
                raster = raster_path(shared_cdl, year, state_fips)
                base = {
                    "grower_slug": grower,
                    "farm_slug": farm,
                    "field_id": field_id,
                    "year": year,
                    "state": field_record.get("state_group", ""),
                    "county": field_record.get("county_group", ""),
                    "field_area_acres": field_record.get("area_acres", np.nan),
                    "raster_path": str(raster),
                    "crop_source": "cdl",
                    "status": "ok",
                }
                if table is None:
                    base.update({"status": "cdl_table_missing"})
                if not raster.is_file():
                    warnings.append(f"{grower}/{farm}: CDL raster for {year} is missing: {raster}")
                    base.update({"status": "cdl_raster_missing"})
                    results.append(base)
                    continue

                crop_counts: dict[int, int] = {}
                total_pixels = 0
                try:
                    from rasterio import open as rasterio_open

                    with rasterio_open(raster) as src:
                        projected = gpd.GeoSeries([boundary.geometry], crs=boundaries.crs).to_crs(src.crs)
                        stats = zonal_stats(
                            projected.iloc[0],
                            str(raster),
                            categorical=True,
                            nodata=-999,
                        )
                        crop_counts = {
                            int(code): int(count) for code, count in (stats[0] if stats else {}).items()
                        }
                except Exception as exc:  # Preserve per-field/year missingness in output.
                    warnings.append(
                        f"{grower}/{farm}/{field_id}/{year}: raster summary failed: {exc}"
                    )
                    base.update({"status": "cdl_zonal_summary_failed"})
                    results.append(base)
                    continue

                total_pixels = sum(crop_counts.values())
                valid_counts = {code: count for code, count in crop_counts.items() if code != 0}
                valid_pixels = sum(valid_counts.values())
                nodata_pixels = crop_counts.get(0, 0)
                corn_pixels = valid_counts.get(1, 0)
                soybean_pixels = valid_counts.get(5, 0)
                other_pixels = valid_pixels - corn_pixels - soybean_pixels
                dominant_codes: list[int] = []
                dominant_count = 0
                if valid_counts:
                    dominant_count = max(valid_counts.values())
                    dominant_codes = sorted(
                        code for code, count in valid_counts.items() if count == dominant_count
                    )
                dominant_names = [_class_name(code, code_names) for code in dominant_codes]
                if table is not None and field_id not in table_groups:
                    warnings.append(
                        f"{grower}/{farm}/{field_id}/{year}: CDL table has no rows for field-year"
                    )
                table_rows = table_groups.get(field_id)
                reported_pct = (
                    float(pd.to_numeric(table_rows["pct"], errors="coerce").fillna(0).sum())
                    if table_rows is not None
                    else float("nan")
                )
                known_table_counts = (
                    {
                        int(row.crop_code): int(row.pixel_count)
                        for row in table_rows[["crop_code", "pixel_count"]].itertuples(index=False)
                        if pd.notna(row.crop_code) and pd.notna(row.pixel_count)
                    }
                    if table_rows is not None
                    else {}
                )
                table_count_mismatches = [
                    code
                    for code, count in known_table_counts.items()
                    if crop_counts.get(code) != count
                ]
                if table_count_mismatches:
                    warnings.append(
                        f"{grower}/{farm}/{field_id}/{year}: raster/table pixel counts differ "
                        f"for codes {table_count_mismatches}"
                    )
                if reported_pct == reported_pct and total_pixels:
                    known_pct = _percent(
                        sum(crop_counts.get(code, 0) for code in known_table_counts), total_pixels
                    )
                    if abs(reported_pct - known_pct) > 0.15:
                        warnings.append(
                            f"{grower}/{farm}/{field_id}/{year}: reported table pct sum "
                            f"{reported_pct:.2f} differs from known raster classes {known_pct:.2f}"
                        )

                base.update(
                    {
                        "total_boundary_pixels": total_pixels,
                        "valid_cdl_pixels": valid_pixels,
                        "nodata_pixels": nodata_pixels,
                        "valid_coverage_pct": _percent(valid_pixels, total_pixels),
                        "corn_pixels": corn_pixels,
                        "soybean_pixels": soybean_pixels,
                        "other_class_pixels": other_pixels,
                        "corn_pct_valid_pixels": _percent(corn_pixels, valid_pixels),
                        "soybean_pct_valid_pixels": _percent(soybean_pixels, valid_pixels),
                        "other_pct_valid_pixels": _percent(other_pixels, valid_pixels),
                        "corn_pct_full_boundary": _percent(corn_pixels, total_pixels),
                        "soybean_pct_full_boundary": _percent(soybean_pixels, total_pixels),
                        "other_pct_full_boundary": _percent(other_pixels, total_pixels),
                        "nodata_pct_full_boundary": _percent(nodata_pixels, total_pixels),
                        "dominant_crop": " / ".join(dominant_names) if dominant_names else "",
                        "dominant_tied": len(dominant_names) > 1,
                        "dominant_pixels": dominant_count,
                        "dominant_pct_valid_pixels": _percent(dominant_count, valid_pixels),
                        "dominant_pct_full_boundary": _percent(dominant_count, total_pixels),
                        "reported_pct_sum": reported_pct,
                        "table_row_count": len(table_rows) if table_rows is not None else 0,
                    }
                )
                results.append(base)

    result = pd.DataFrame(results)
    if result.empty:
        warnings.append("No CDL field-year rows could be processed")
        return warnings
    result.to_csv(output_dir / "cdl_field_year_comparison.csv", index=False)

    common = result.loc[result["year"] == common_year].copy()
    common = common.loc[common.get("status", pd.Series(index=common.index, dtype="object")) == "ok"]
    common = common.loc[common["dominant_crop"].fillna("").ne("")]
    fig, ax = plt.subplots(figsize=(12, 6))
    growers = list(fields["grower_slug"].drop_duplicates())
    classes = sorted(common["dominant_crop"].dropna().unique())
    x = np.arange(len(growers))
    width = 0.8 / max(1, len(classes))
    for class_index, crop_name in enumerate(classes):
        counts = []
        pcts = []
        for grower in growers:
            group = common.loc[common["grower_slug"] == grower]
            denominator = group["field_id"].nunique()
            count = group.loc[group["dominant_crop"] == crop_name, "field_id"].nunique()
            counts.append(count)
            pcts.append(100 * count / denominator if denominator else 0)
        offset = (class_index - (len(classes) - 1) / 2) * width
        bars = ax.bar(
            x + offset,
            counts,
            width=width * 0.92,
            color=COLORS.get(crop_name, plt.get_cmap("tab20")(class_index % 20)),
            edgecolor="#344054",
            linewidth=0.4,
            label=crop_name,
        )
        for bar, count, pct in zip(bars, counts, pcts):
            if count:
                ax.annotate(
                    f"{count}\n{pct:.0f}%",
                    (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 2),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    fontsize=7,
                )
    ax.set_xticks(x, growers, rotation=15, ha="right")
    ax.set_ylabel("Fields with this dominant mapped class")
    ax.set_title(f"Mapped dominant CDL class by field count ({common_year})")
    ax.set_ylim(0, max(1, int(common.groupby("grower_slug")["field_id"].nunique().max())) * 1.35)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(title="Dominant class", bbox_to_anchor=(1.02, 1), loc="upper left")
    fig.text(
        0.01,
        0.01,
        "Labels show field count and percent of valid field-years; not area share or ground truth.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 0.82, 1))
    fig.savefig(output_dir / "cdl_2023_dominant_crop_counts.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    all_dominants = result.loc[result["status"] == "ok"].copy()
    field_order = (
        fields.sort_values(["grower_slug", "field_id"])["field_id"].astype(str).tolist()
    )
    grower_lookup = fields.drop_duplicates("field_id").set_index("field_id")["grower_slug"]
    categories = sorted(all_dominants["dominant_crop"].dropna().unique())
    category_to_index = {value: i for i, value in enumerate(categories)}
    palette = [COLORS.get(cat, plt.get_cmap("tab20")(i % 20)) for i, cat in enumerate(categories)]
    missing_color = "#d9d9d9"
    cmap = ListedColormap(palette + [missing_color])
    values = np.full((len(field_order), len(years)), len(categories), dtype=float)
    annotations: dict[tuple[int, int], str] = {}
    row_lookup = {field_id: row for row, field_id in enumerate(field_order)}
    col_lookup = {year: col for col, year in enumerate(years)}
    for row in all_dominants.itertuples(index=False):
        field_id = str(row.field_id)
        year = int(row.year)
        if field_id not in row_lookup or year not in col_lookup or not row.dominant_crop:
            continue
        y = row_lookup[field_id]
        x_col = col_lookup[year]
        values[y, x_col] = category_to_index[row.dominant_crop]
        share = getattr(row, "dominant_pct_valid_pixels", np.nan)
        annotations[(y, x_col)] = f"{float(share):.0f}%" if pd.notna(share) else ""
    fig, ax = plt.subplots(figsize=(12, max(7, 0.38 * len(field_order))))
    mesh = ax.imshow(values, aspect="auto", interpolation="none", cmap=cmap, vmin=-0.5, vmax=len(categories) + 0.5)
    del mesh
    ax.set_xticks(np.arange(len(years)), [str(year) for year in years])
    ax.set_yticks(np.arange(len(field_order)), field_order, fontsize=8)
    ax.set_xlabel("CDL year")
    ax.set_ylabel("Field ID (grouped by grower)")
    ax.set_title("Dominant mapped CDL class by field and year")
    for (y, x_col), label in annotations.items():
        ax.text(x_col, y, label, ha="center", va="center", fontsize=7, color="#111827")
    previous = None
    for index, field_id in enumerate(field_order):
        grower = grower_lookup.get(field_id, "")
        if previous is not None and grower != previous:
            ax.axhline(index - 0.5, color="#111827", linewidth=1.5)
        previous = grower
    legend_handles = [
        Patch(facecolor=palette[i], edgecolor="none", label=category)
        for i, category in enumerate(categories)
    ]
    legend_handles.append(Patch(facecolor=missing_color, label="Missing / unavailable"))
    ax.legend(
        handles=legend_handles,
        title="Dominant class\n(labels = % valid pixels)",
        bbox_to_anchor=(1.02, 1),
        loc="upper left",
    )
    fig.text(
        0.01,
        0.005,
        "Dominant class is the largest mapped pixel class, not proof of the whole-field crop.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.025, 0.82, 1))
    fig.savefig(output_dir / "cdl_dominant_crop_by_field_year.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    if common.empty:
        warnings.append(f"No valid CDL field-year observations for common year {common_year}")
    elif common.groupby("grower_slug")["field_id"].nunique().lt(
        fields.groupby("grower_slug")["field_id"].nunique()
    ).any():
        warnings.append(f"At least one grower has missing valid CDL fields in {common_year}")
    return warnings
