"""Matched-season summaries from NASA POWER field weather tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import pandas as pd


WEATHER_COLUMNS = [
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
    "RH2M",
    "WS10M",
]
UNITS = {
    "T2M": "°C",
    "T2M_MAX": "°C",
    "T2M_MIN": "°C",
    "PRECTOTCORR": "mm/day",
    "ALLSKY_SFC_SW_DWN": "MJ/m²/day",
    "RH2M": "%",
    "WS10M": "m/s",
}


def _weather_table(tables_dir: Path, farm_slug: str) -> Path | None:
    candidates = sorted(tables_dir.glob("*weather_*.csv"))
    return candidates[0] if len(candidates) == 1 else None


def _grid_ids(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["grid_lat"] = (result["lat"].astype(float) / 0.5).round() * 0.5
    result["grid_lon"] = (result["lon"].astype(float) / 0.625).round() * 0.625
    result["source_grid_id"] = (
        result["grid_lat"].map("{:.3f}".format)
        + ":"
        + result["grid_lon"].map("{:.3f}".format)
    )
    return result


def analyze_weather(
    fields: pd.DataFrame,
    *,
    data_root: Path,
    output_dir: Path,
    years: list[int],
    season_start: str,
    season_end: str,
) -> tuple[list[str], dict[str, Any]]:
    """Deduplicate by source grid and create seasonal summaries and plots."""
    warnings: list[str] = []
    growers_by_grid: dict[str, set[str]] = {}
    fields_by_grid: dict[str, set[str]] = {}
    rows: list[pd.DataFrame] = []
    field_lookup = fields.set_index("field_id")

    for (grower, farm), group in fields.groupby(["grower_slug", "farm_slug"], sort=False):
        tables_dir = (
            data_root
            / "data-pipeline"
            / "growers"
            / grower
            / "farms"
            / farm
            / "derived"
            / "tables"
        )
        path = _weather_table(tables_dir, farm)
        if path is None:
            warnings.append(f"{grower}/{farm}: farm weather CSV is missing or ambiguous")
            continue
        weather = pd.read_csv(path)
        required = {"field_id", "lat", "lon", "date", *WEATHER_COLUMNS}
        if not required.issubset(weather.columns):
            warnings.append(
                f"{path}: missing weather columns {sorted(required - set(weather.columns))}"
            )
            continue
        weather["field_id"] = weather["field_id"].astype(str)
        weather["date"] = pd.to_datetime(weather["date"], errors="coerce")
        for column in WEATHER_COLUMNS:
            weather[column] = pd.to_numeric(weather[column], errors="coerce").replace(-999.0, np.nan)
        weather = _grid_ids(weather)
        weather["grower_slug"] = grower
        weather["farm_slug"] = farm
        unknown_ids = sorted(set(weather["field_id"]) - set(group["field_id"].astype(str)))
        if unknown_ids:
            warnings.append(f"{grower}/{farm}: weather table has unrecognized field IDs {unknown_ids}")
        absent_ids = sorted(set(group["field_id"].astype(str)) - set(weather["field_id"]))
        if absent_ids:
            warnings.append(f"{grower}/{farm}: weather table missing fields {absent_ids}")
        for field_id, part in weather.groupby("field_id", sort=False):
            grid_ids = part["source_grid_id"].dropna().unique().tolist()
            if len(grid_ids) != 1:
                warnings.append(
                    f"{grower}/{farm}/{field_id}: centroid maps to multiple grid IDs {grid_ids}"
                )
                continue
            grid_id = grid_ids[0]
            growers_by_grid.setdefault(grid_id, set()).add(grower)
            fields_by_grid.setdefault(grid_id, set()).add(field_id)
        rows.append(weather)

    if not rows:
        return ["No usable weather tables found"], {}
    all_weather = pd.concat(rows, ignore_index=True)
    all_weather = all_weather.loc[all_weather["date"].notna()].copy()
    all_weather = all_weather.loc[all_weather["date"].dt.year.isin(years)].copy()

    group_keys = ["source_grid_id", "date"]
    conflicts: list[str] = []
    for (grid_id, date), group in all_weather.groupby(group_keys, sort=False, dropna=False):
        for column in WEATHER_COLUMNS:
            if group[column].dropna().nunique() > 1:
                conflicts.append(f"{grid_id}/{date.date()}/{column}")
    if conflicts:
        raise ValueError(
            "Fields assigned to the same NASA POWER grid disagree; refusing to average "
            f"duplicate measurements: {conflicts[:10]}"
        )
    unique_weather = (
        all_weather.sort_values(["source_grid_id", "date", "field_id"])
        .drop_duplicates(group_keys, keep="first")
        .copy()
    )
    unique_weather["grower_slugs"] = unique_weather["source_grid_id"].map(
        lambda key: ", ".join(sorted(growers_by_grid.get(key, set())))
    )
    unique_weather["field_ids_using_grid"] = unique_weather["source_grid_id"].map(
        lambda key: ", ".join(sorted(fields_by_grid.get(key, set())))
    )

    start_month, start_day = map(int, season_start.split("-"))
    end_month, end_day = map(int, season_end.split("-"))
    summaries: list[dict[str, Any]] = []
    seasonal_parts: list[pd.DataFrame] = []
    for grid_id, grid_frame in unique_weather.groupby("source_grid_id", sort=True):
        grid_frame = grid_frame.sort_values("date")
        for year in years:
            start = pd.Timestamp(year=year, month=start_month, day=start_day)
            end = pd.Timestamp(year=year, month=end_month, day=end_day)
            expected = len(pd.date_range(start, end, freq="D"))
            part = grid_frame.loc[grid_frame["date"].between(start, end)].copy()
            part["analysis_year"] = year
            seasonal_parts.append(part)
            precip = part["PRECTOTCORR"].dropna()
            temp = part["T2M"].dropna()
            valid_precip = int(precip.size)
            valid_temp = int(temp.size)
            rainfall_total = float(precip.sum()) if valid_precip == expected else np.nan
            if valid_precip != expected:
                warnings.append(
                    f"{grid_id}/{year}: rainfall total withheld; {valid_precip}/{expected} "
                    "valid seasonal days"
                )
            first = grid_frame.iloc[0]
            summaries.append(
                {
                    "source_grid_id": grid_id,
                    "grid_lat": first["grid_lat"],
                    "grid_lon": first["grid_lon"],
                    "grower_slugs": first["grower_slugs"],
                    "field_ids_using_grid": first["field_ids_using_grid"],
                    "year": year,
                    "season_start": start.date().isoformat(),
                    "season_end": end.date().isoformat(),
                    "expected_days": expected,
                    "valid_precip_days": valid_precip,
                    "rainfall_total_mm": round(rainfall_total, 2) if pd.notna(rainfall_total) else np.nan,
                    "valid_t2m_days": valid_temp,
                    "t2m_mean_c": round(float(temp.mean()), 2) if valid_temp else np.nan,
                    "t2m_median_c": round(float(temp.median()), 2) if valid_temp else np.nan,
                    "t2m_min_c": round(float(temp.min()), 2) if valid_temp else np.nan,
                    "t2m_max_c": round(float(temp.max()), 2) if valid_temp else np.nan,
                }
            )

    summary = pd.DataFrame(summaries)
    summary.to_csv(output_dir / "weather_seasonal_comparison.csv", index=False)
    seasonal = pd.concat(seasonal_parts, ignore_index=True) if seasonal_parts else pd.DataFrame()
    summary["grower_label"] = summary["grower_slugs"].replace("", "Unassigned")

    growers = list(fields["grower_slug"].drop_duplicates())
    fig, axes = plt.subplots(
        1, len(growers), figsize=(6 * len(growers), 5), squeeze=False, sharey=True
    )
    for ax, grower in zip(axes[0], growers):
        group = summary.loc[
            summary["grower_slugs"].str.split(", ").apply(lambda values: grower in values)
        ]
        if group.empty:
            ax.text(0.5, 0.5, "No source grid data", ha="center", va="center")
            ax.set_axis_off()
            continue
        for grid_id, grid_summary in group.groupby("source_grid_id", sort=True):
            grid_summary = grid_summary.sort_values("year")
            ax.plot(
                grid_summary["year"],
                grid_summary["rainfall_total_mm"],
                marker="o",
                linewidth=1.5,
                label=grid_id,
            )
        ax.set_title(grower)
        ax.set_xlabel("Year")
        ax.set_xticks(years)
        ax.grid(alpha=0.25)
    axes[0, 0].set_ylabel("April–October precipitation total (mm)")
    for ax in axes[0]:
        handles, _ = ax.get_legend_handles_labels()
        if handles:
            ax.legend(title="NASA POWER grid", fontsize=7, loc="best")
    fig.suptitle("Matched-season rainfall by unique source location")
    fig.text(
        0.01,
        0.01,
        "Totals shown only for complete seasons; repeated fields on a grid are counted once.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 0.94, 0.94))
    fig.savefig(output_dir / "weather_seasonal_rainfall_by_location_year.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(
        1, len(growers), figsize=(6 * len(growers), 6), squeeze=False, sharey=True
    )
    palette = plt.get_cmap("tab10")
    for ax, grower in zip(axes[0], growers):
        grid_ids = sorted(
            grid_id
            for grid_id, grower_set in growers_by_grid.items()
            if grower in grower_set
        )
        if not grid_ids:
            ax.text(0.5, 0.5, "No source grid data", ha="center", va="center")
            ax.set_axis_off()
            continue
        positions: list[float] = []
        box_data: list[np.ndarray] = []
        box_colors: list[Any] = []
        tick_positions: list[float] = []
        for year_index, year in enumerate(years):
            for grid_index, grid_id in enumerate(grid_ids):
                values = seasonal.loc[
                    (seasonal["source_grid_id"] == grid_id)
                    & (seasonal["analysis_year"] == year),
                    "T2M",
                ].dropna().to_numpy(dtype=float)
                if not len(values):
                    continue
                offset = (grid_index - (len(grid_ids) - 1) / 2) * 0.16
                position = year_index + offset
                positions.append(position)
                box_data.append(values)
                box_colors.append(palette(grid_index % 10))
            tick_positions.append(float(year_index))
        boxes = ax.boxplot(
            box_data,
            positions=positions,
            widths=0.13,
            patch_artist=True,
            showfliers=False,
            medianprops={"color": "#111827", "linewidth": 1.2},
        )
        for patch, color in zip(boxes["boxes"], box_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.65)
        ax.set_xticks(tick_positions, [str(year) for year in years])
        ax.set_title(grower)
        ax.set_xlabel("Year")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(
            handles=[
                Patch(facecolor=palette(index % 10), label=grid_id)
                for index, grid_id in enumerate(grid_ids)
            ],
            title="NASA POWER grid",
            fontsize=7,
            loc="best",
        )
    axes[0, 0].set_ylabel("Daily mean air temperature, T2M (°C)")
    fig.suptitle("Matched-season daily temperature distributions by location and year")
    fig.text(
        0.01,
        0.01,
        "One distribution per unique grid cell; daily values are temporally correlated.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    fig.savefig(
        output_dir / "weather_seasonal_temperature_by_location_year.png",
        dpi=180,
        bbox_inches="tight",
    )
    plt.close(fig)

    metadata = {
        "unique_grid_count": int(unique_weather["source_grid_id"].nunique()),
        "grid_fields": {
            key: sorted(value) for key, value in sorted(fields_by_grid.items())
        },
        "grid_growers": {
            key: sorted(value) for key, value in sorted(growers_by_grid.items())
        },
        "weather_columns": WEATHER_COLUMNS,
        "units": UNITS,
        "season_start": season_start,
        "season_end": season_end,
        "years": years,
        "valid_precip_days_min": int(summary["valid_precip_days"].min()) if not summary.empty else 0,
        "valid_precip_days_max": int(summary["valid_precip_days"].max()) if not summary.empty else 0,
        "expected_days": int(summary["expected_days"].max()) if not summary.empty else 0,
        "complete_rainfall_grid_years": int(summary["rainfall_total_mm"].notna().sum()),
    }
    return warnings, metadata
