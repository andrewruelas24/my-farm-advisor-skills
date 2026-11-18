"""Static geographic overview and local context panels."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd


STATE_FILE = "l1_states/states_usa.geojson"
COUNTY_FILE = "l2_counties/counties_usa.geojson"
STATE_COLORS = ["#2878b5", "#e07a5f", "#59a14f", "#b07aa1"]


def create_context_map(
    fields: pd.DataFrame, *, data_root: Path, output_dir: Path
) -> list[str]:
    """Write an overview plus state/county panels with actual field polygons."""
    runtime = data_root / "data-pipeline"
    states_path = runtime / "shared" / "geoadmin" / STATE_FILE
    counties_path = runtime / "shared" / "geoadmin" / COUNTY_FILE
    if not states_path.is_file() or not counties_path.is_file():
        return [f"Static map context files missing: {states_path} or {counties_path}"]

    all_states = gpd.read_file(states_path)
    counties = gpd.read_file(counties_path)
    growers = list(fields["grower_slug"].drop_duplicates())
    state_fips = sorted(set(fields["state_fips"].astype(str).str.zfill(2)))
    states = all_states.loc[
        all_states["state_fips"].astype(str).str.zfill(2).isin(state_fips)
    ].copy()
    counties = counties.loc[
        counties["state_fips"].astype(str).str.zfill(2).isin(state_fips)
    ].copy()

    boundary_frames = []
    for (grower, farm), group in fields.groupby(["grower_slug", "farm_slug"], sort=False):
        boundary_path = (
            runtime
            / "growers"
            / grower
            / "farms"
            / farm
            / "boundary"
            / "field_boundaries.geojson"
        )
        boundary = gpd.read_file(boundary_path)
        boundary["field_id"] = boundary["field_id"].astype(str)
        boundary["grower_slug"] = grower
        boundary["farm_slug"] = farm
        boundary["state_fips"] = boundary["state_fips"].astype(str).str.zfill(2)
        boundary_frames.append(boundary)
    all_fields = gpd.GeoDataFrame(
        pd.concat(boundary_frames, ignore_index=True),
        geometry="geometry",
        crs=boundary_frames[0].crs,
    )

    target_crs = "EPSG:5070"
    all_states = all_states.to_crs(target_crs)
    states = states.to_crs(target_crs)
    counties = counties.to_crs(target_crs)
    all_fields = all_fields.to_crs(target_crs)

    fig = plt.figure(figsize=(16, 11))
    grid = fig.add_gridspec(2, 3, height_ratios=[0.95, 1.2], hspace=0.24, wspace=0.12)
    overview = fig.add_subplot(grid[0, :])
    contiguous = all_states.loc[
        ~all_states["state_fips"].astype(str).str.zfill(2).isin(
            ["02", "15", "60", "66", "69", "72", "78"]
        )
    ]
    contiguous.plot(ax=overview, color="#eef1f4", edgecolor="#aab3bb", linewidth=0.35)
    colors = {
        grower: STATE_COLORS[index % len(STATE_COLORS)]
        for index, grower in enumerate(growers)
    }
    for grower in growers:
        group = all_fields.loc[all_fields["grower_slug"] == grower]
        state_codes = sorted(group["state_fips"].astype(str).str.zfill(2).unique())
        states.loc[states["state_fips"].astype(str).str.zfill(2).isin(state_codes)].plot(
            ax=overview,
            color=colors[grower],
            alpha=0.42,
            edgecolor="#374151",
            linewidth=0.65,
        )
        centroids = group.geometry.centroid
        overview.scatter(
            centroids.x,
            centroids.y,
            s=8,
            color=colors[grower],
            edgecolor="#111827",
            linewidth=0.2,
            label=grower,
            zorder=4,
        )
    overview.set_title("Selected field locations across the contiguous United States")
    overview.legend(loc="lower left", ncol=3, fontsize=8)
    overview.set_axis_off()

    for index, grower in enumerate(growers):
        ax = fig.add_subplot(grid[1, index])
        group = all_fields.loc[all_fields["grower_slug"] == grower]
        codes = sorted(group["state_fips"].astype(str).str.zfill(2).unique())
        states_for_grower = states.loc[
            states["state_fips"].astype(str).str.zfill(2).isin(codes)
        ]
        county_names = set(group["county"].astype(str).str.casefold())
        counties_for_grower = counties.loc[
            counties["state_fips"].astype(str).str.zfill(2).isin(codes)
            & counties["county_name"].astype(str).str.casefold().isin(county_names)
        ]
        if not counties_for_grower.empty:
            counties_for_grower.plot(
                ax=ax, color="#f2f3f5", edgecolor="#9aa4ad", linewidth=0.8
            )
        else:
            states_for_grower.plot(
                ax=ax, color="#f2f3f5", edgecolor="#9aa4ad", linewidth=0.8
            )
        group.plot(
            ax=ax,
            color=colors[grower],
            alpha=0.48,
            edgecolor="#111827",
            linewidth=0.8,
            zorder=3,
        )
        minx, miny, maxx, maxy = group.total_bounds
        padx = max((maxx - minx) * 0.16, 12000)
        pady = max((maxy - miny) * 0.16, 12000)
        ax.set_xlim(minx - padx, maxx + padx)
        ax.set_ylim(miny - pady, maxy + pady)
        state_names = sorted(set(group["state"].astype(str)))
        county_names = sorted(set(group["county"].astype(str)))
        ax.set_title(
            f"{grower}\n{', '.join(county_names)} County, {', '.join(state_names)}"
        )
        ax.set_axis_off()
    fig.suptitle(
        "Assignment 2 field-boundary context",
        fontsize=16,
        fontweight="bold",
        y=0.99,
    )
    fig.text(
        0.01,
        0.01,
        "USDA CSB crop-sequence boundaries; selected samples, not verified ownership parcels.",
        fontsize=9,
    )
    fig.savefig(output_dir / "grower_field_boundary_context.png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    return []
