#!/usr/bin/env python3
"""Create a lightweight, standalone interactive map for one or all growers."""

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds
from shapely import transform as transform_geometry
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


SAFE_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")
TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
    "MapServer/tile/{z}/{y}/{x}"
)
LEAFLET_VERSION = "1.9.4"
NDVI_COLORS = [
    (128, 72, 35),
    (196, 129, 54),
    (232, 205, 91),
    (177, 204, 83),
    (91, 166, 74),
    (29, 112, 59),
]


def _runtime_base() -> Path:
    raw = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if not raw:
        raise ValueError("DATA_PIPELINE_DATA_ROOT must point to the runtime data root")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("DATA_PIPELINE_DATA_ROOT must be an absolute path")
    return root.resolve() / "data-pipeline"


def _slug(value: str, label: str) -> str:
    if value in {"", ".", ".."} or not SAFE_SLUG.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _read_geojson(path: Path) -> list[dict[str, Any]]:
    value = _json_file(path)
    features = value.get("features")
    if not isinstance(features, list):
        raise ValueError(f"Boundary GeoJSON has no feature list: {path}")
    return features


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _read_soil_components(farm_dir: Path, field_id: str) -> dict[str, list[dict[str, Any]]]:
    """Read the available component names, shares, and drainage classes by MUKEY."""
    records: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for path in sorted((farm_dir / "derived" / "tables").glob("*.csv")):
        if not any(word in path.name.lower() for word in ("soil", "ssurgo")):
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not {"field_id", "mukey", "compname"}.issubset(reader.fieldnames or []):
                    continue
                for row in reader:
                    if str(row.get("field_id", "")) != field_id:
                        continue
                    mukey = str(row.get("mukey", "")).strip()
                    name = str(row.get("compname", "")).strip()
                    if not mukey or not name:
                        continue
                    try:
                        share = float(row.get("comppct_r") or 0)
                    except ValueError:
                        share = 0.0
                    component = records[mukey].setdefault(
                        name, {"name": name, "shares": [], "drainage_classes": set()}
                    )
                    if share:
                        component["shares"].append(share)
                    drainage = str(row.get("drainagecl") or "").strip()
                    if drainage:
                        component["drainage_classes"].add(drainage)
        except (OSError, UnicodeError, csv.Error):
            continue
    output: dict[str, list[dict[str, Any]]] = {}
    for mukey, components in records.items():
        output[mukey] = []
        for component in components.values():
            shares = component.pop("shares")
            component["share"] = max(shares) if shares else None
            component["drainage_classes"] = sorted(component["drainage_classes"])
            output[mukey].append(component)
        output[mukey].sort(key=lambda item: (-(item["share"] or 0), item["name"]))
    return output


def _soil_features(
    field_dir: Path,
    field_id: str,
    field_name: str,
    field_geometry: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = field_dir / "soil" / "ssurgo_soil_types.geojson"
    if not path.is_file():
        return [], _empty_soil_insight("No SSURGO polygons are available for this field.")
    source_features = _read_geojson(path)
    components_by_key = _read_soil_components(field_dir.parents[1], field_id)
    result: list[dict[str, Any]] = []
    grouped_geometries: dict[str, list[Any]] = defaultdict(list)
    projected_geometries: list[Any] = []
    to_area_crs = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform
    field_shape = shape(field_geometry)
    field_area = transform_geometry(field_shape, to_area_crs, interleaved=False).area
    for source in source_features:
        geometry = source.get("geometry")
        props = source.get("properties") or {}
        if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        mukey = str(props.get("mukey") or "").strip()
        clipped = shape(geometry).intersection(field_shape)
        if clipped.is_empty:
            continue
        projected = transform_geometry(clipped, to_area_crs, interleaved=False)
        if projected.area <= 0:
            continue
        projected_geometries.append(projected)
        if mukey:
            grouped_geometries[mukey].append(projected)
        attrs: dict[str, Any] = {"field_id": field_id, "field": field_name}
        if mukey:
            attrs["mukey"] = mukey
        for key in ("musym", "muname", "mapunit_symbol", "mapunit_name"):
            if props.get(key) not in (None, ""):
                attrs[key] = props[key]
        unit_components = components_by_key.get(mukey, [])
        if unit_components:
            attrs["dominant_component"] = unit_components[0]["name"]
        result.append({"type": "Feature", "geometry": mapping(clipped), "properties": attrs})
    if not result:
        return [], _empty_soil_insight("No SSURGO map-unit polygons are available for this field.")

    coverage_percent: float | None = None
    unit_percentages: dict[str, float] = {}
    if field_area > 0 and projected_geometries:
        mapped_union_area = unary_union(projected_geometries).area
        if mapped_union_area > 0:
            coverage_ratio = mapped_union_area / field_area
            if coverage_ratio <= 1.01:
                coverage_percent = min(100.0, 100.0 * coverage_ratio)
            unit_areas = {
                key: unary_union(geometries).area
                for key, geometries in grouped_geometries.items()
            }
            unit_area_sum = sum(unit_areas.values())
            keyed_union_area = unary_union(
                [geometry for geometries in grouped_geometries.values() for geometry in geometries]
            ).area if grouped_geometries else 0.0
            # Do not report per-unit shares if polygons assigned to different
            # MUKEYs materially overlap or some polygons have no MUKEY; union
            # coverage remains valid in either case.
            if (
                unit_area_sum > 0
                and abs(unit_area_sum - mapped_union_area) / mapped_union_area <= 0.01
                and abs(keyed_union_area - mapped_union_area) / mapped_union_area <= 0.01
            ):
                unit_percentages = {
                    key: 100.0 * area / unit_area_sum for key, area in unit_areas.items()
                }

    ordered_keys = sorted(
        grouped_geometries,
        key=lambda key: -unary_union(grouped_geometries[key]).area,
    )
    units: list[dict[str, Any]] = []
    for key in ordered_keys[:3]:
        unit_feature = next(
            (feature for feature in result if feature["properties"].get("mukey") == key),
            None,
        )
        unit_props = unit_feature["properties"] if unit_feature else {}
        unit_components = components_by_key.get(key, [])
        dominant = unit_components[0] if unit_components else None
        units.append(
            {
                "mukey": key,
                "mapunit_name": unit_props.get("muname") or unit_props.get("mapunit_name"),
                "mapunit_symbol": unit_props.get("musym") or unit_props.get("mapunit_symbol"),
                "area_percent": round(unit_percentages[key], 1) if key in unit_percentages else None,
                "dominant_component": dominant["name"] if dominant else None,
                "component_share": round(dominant["share"], 1)
                if dominant and dominant["share"] is not None
                else None,
                "drainage_classes": dominant["drainage_classes"] if dominant else [],
            }
        )
    insight = _soil_insight(
        coverage_percent=round(coverage_percent, 1) if coverage_percent is not None else None,
        units=units,
        has_any_mapunit_labels=any(u["mapunit_name"] or u["mapunit_symbol"] for u in units),
    )
    return result, insight


def _empty_soil_insight(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "coverage_percent": None,
        "units": [],
        "shows": "Colors distinguish soil map units identified in the USDA soil survey.",
        "observes": reason.rstrip(".") + "; soil names, characteristics, and coverage cannot be reported.",
        "explains": "This map has no soil result to interpret; check existing soil records or field observations.",
    }


def _soil_insight(
    *,
    coverage_percent: float | None,
    units: list[dict[str, Any]],
    has_any_mapunit_labels: bool,
) -> dict[str, Any]:
    if not units:
        return _empty_soil_insight("No usable SSURGO map-unit polygons are available")
    unit_parts: list[str] = []
    for unit in units[:2]:
        name = unit["mapunit_name"] or unit["mapunit_symbol"]
        label = f"{name} (MUKEY {unit['mukey']})" if name else f"MUKEY {unit['mukey']}"
        if unit["area_percent"] is not None:
            label += f", about {unit['area_percent']:g}% of mapped area"
        if unit["dominant_component"]:
            label += f", major component {unit['dominant_component']}"
            if unit["component_share"] is not None:
                label += f" ({unit['component_share']:g}% component share)"
        if unit["drainage_classes"]:
            label += f", {unit['drainage_classes'][0].lower()}"
        unit_parts.append(label)
    if coverage_percent is None:
        observation_parts = ["Mapped polygon coverage could not be calculated reliably"]
    else:
        observation_parts = [f"Mapped polygons cover an estimated {coverage_percent:g}% of this field"]
    if unit_parts:
        observation_parts.append("leading mapped units include " + " and ".join(unit_parts))
    if not has_any_mapunit_labels:
        observation_parts.append("MUKEY values are survey map-unit IDs, not soil names")
    observes = "; ".join(observation_parts) + "."
    drainage = next((u["drainage_classes"][0] for u in units if u["drainage_classes"]), None)
    if drainage:
        explains = (
            f"The survey rates a leading component {drainage.lower()}, meaning water may leave it "
            "more slowly than a well-drained soil; ask whether that mapped area tends to stay wet after rain."
        )
    else:
        explains = (
            "These are mapped survey characteristics, not a measure of current field moisture. "
            "Check the field or soil records before drawing a management conclusion."
        )
    return {
        "status": "available",
        "coverage_percent": coverage_percent,
        "units": units,
        "shows": "Colors show different areas identified in the USDA soil survey.",
        "observes": observes,
        "explains": explains,
    }


def _manifest_scene_info(field_dir: Path, year: int) -> dict[str, Any]:
    sources: list[str] = []
    dates: list[str] = []
    counts: dict[str, int] = {}
    for sensor, label in (("sentinel", "Sentinel-2"), ("landsat", "Landsat")):
        manifest = field_dir / "satellite" / sensor / "manifest.json"
        if not manifest.is_file():
            continue
        document = _json_file(manifest)
        year_entry = next(
            (entry for entry in document.get("years", []) if entry.get("year") == year),
            None,
        )
        if not year_entry:
            continue
        sensor_dates = [
            str(scene.get("scene_date"))
            for scene in year_entry.get("scenes", [])
            if scene.get("ndvi_tif") and scene.get("scene_date")
        ]
        if sensor_dates:
            sources.append(label)
            dates.extend(sensor_dates)
            counts[label] = len(sensor_dates)
    return {
        "source": " + ".join(sources) if sources else None,
        "date_start": min(dates) if dates else None,
        "date_end": max(dates) if dates else None,
        "scene_counts": counts,
        "scene_count": sum(counts.values()),
    }


def _ndvi_png(
    field_dir: Path, year: int, field_geometry: dict[str, Any]
) -> dict[str, Any]:
    path = field_dir / "derived" / "features" / f"ndvi_year_{year}_composite.tif"
    if not path.is_file():
        return {"status": "unavailable", "reason": "Yearly NDVI composite is missing."}
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                return {"status": "unavailable", "reason": "NDVI raster has no coordinate reference system."}
            values = src.read(1).astype(np.float32, copy=False)
            transform = src.transform
            crs = src.crs
            if crs.to_epsg() != 4326:
                raise ValueError(f"Expected EPSG:4326 NDVI composite, got {crs}")
            inside = geometry_mask(
                [field_geometry],
                out_shape=values.shape,
                transform=transform,
                invert=True,
                all_touched=True,
            )
            valid = inside & np.isfinite(values) & (values >= -1.0) & (values <= 1.0)
            inside_count = int(inside.sum())
            valid_count = int(valid.sum())
            fraction = valid_count / max(inside_count, 1)
            if valid_count < 100 or fraction < 0.75:
                return {
                    "status": "unavailable",
                    "reason": (
                        f"Only {valid_count} valid pixels ({fraction * 100:.0f}% of the field mask) "
                        "fall inside this field, below the reporting threshold"
                    ),
                    "valid_fraction": round(fraction, 4),
                    "valid_pixels": valid_count,
                }
            in_field_values = values[valid]
            mean = float(np.mean(in_field_values))
            p10, median, p90 = (float(value) for value in np.percentile(in_field_values, [10, 50, 90]))
            # A transparent RGBA image keeps the standalone HTML compact and
            # leaves satellite imagery visible beneath the classified NDVI.
            safe_values = np.where(np.isfinite(values), values, -0.1)
            scaled = np.clip((safe_values + 0.1) / 1.0, 0, 0.9999)
            indices = (scaled * len(NDVI_COLORS)).astype(np.uint8)
            rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
            palette = np.asarray(NDVI_COLORS, dtype=np.uint8)
            rgba[valid, :3] = palette[indices[valid]]
            rgba[valid, 3] = 190
            image = Image.fromarray(rgba, mode="RGBA")
            if max(image.size) > 512:
                ratio = 512 / max(image.size)
                image = image.resize(
                    (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                    Image.Resampling.BILINEAR,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            west, south, east, north = transform_bounds(
                crs, "EPSG:4326", *src.bounds, densify_pts=21
            )
            bounds = [[south, west], [north, east]]
    except (OSError, ValueError, rasterio.errors.RasterioError):
        return {"status": "unavailable", "reason": "NDVI raster could not be read or mapped to the field."}
    return {
        "status": "ready" if fraction >= 0.95 else "partial",
        "png": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "bounds": bounds,
        "valid_fraction": round(fraction, 4),
        "valid_pixels": valid_count,
        "mean": round(mean, 4),
        "p10": round(p10, 4),
        "median": round(median, 4),
        "p90": round(p90, 4),
    }


def _ndvi_insight(
    *, year: int, product: dict[str, Any], scene_info: dict[str, Any]
) -> dict[str, str]:
    shows = "NDVI is a satellite-based measure of vegetation greenness, not a crop diagnosis."
    if product.get("status") == "unavailable":
        reason = product.get("reason", "No usable in-field NDVI data is available.")
        date_source = " Scene source and dates are not recorded in the runtime manifests."
        if scene_info.get("scene_count"):
            date_source = (
                f" Scene records list {scene_info['scene_count']} {scene_info.get('source') or 'imagery'} "
                f"scenes dated {scene_info.get('date_start') or 'unknown'} to {scene_info.get('date_end') or 'unknown'}."
            )
        return {
            "shows": shows,
            "observes": f"The {year} layer is unavailable: {reason.rstrip('.')}." + date_source,
            "explains": "This data cannot support a field-specific reading; check crop records or another imagery date.",
        }
    sources = scene_info.get("source")
    count = scene_info.get("scene_count")
    dates = (
        f"dated {scene_info['date_start']} to {scene_info['date_end']}"
        if scene_info.get("date_start") and scene_info.get("date_end")
        else "with scene dates not recorded in the runtime manifests"
    )
    if sources and count:
        composition = f"a per-pixel mean composite from {count} {sources} scenes, {dates}"
    elif sources:
        composition = f"a per-pixel mean composite from {sources}, {dates}"
    else:
        composition = f"a per-pixel mean annual composite; source details are unavailable and {dates}"
    observation = (
        f"The {year} layer is {composition}; field mean {product['mean']:.3f}, with the middle 80% of valid "
        f"pixel values from {product['p10']:.3f} to {product['p90']:.3f} (median {product['median']:.3f})"
    )
    if product.get("status") == "partial":
        observation += f", and {product['valid_fraction'] * 100:.0f}% of in-field pixels were valid"
    observation += "."
    explains = (
        "This describes satellite greenness, not its cause; crop stage, bare ground, harvest timing, "
        "and image quality can affect it. Check whether the mapped pattern matches field history or "
        "the grower's field observations."
    )
    return {"shows": shows, "observes": observation, "explains": explains}


def collect_grower(grower_dir: Path) -> dict[str, Any]:
    grower_slug = _slug(grower_dir.name, "grower slug")
    grower_meta = _json_file(grower_dir / "grower.json")
    farms_dir = grower_dir / "farms"
    farms = sorted(path for path in farms_dir.iterdir() if path.is_dir()) if farms_dir.exists() else []
    if not farms:
        raise ValueError(f"Grower has no farm directories: {grower_dir}")

    output_farms: list[dict[str, Any]] = []
    all_fields: list[dict[str, Any]] = []
    soils: list[dict[str, Any]] = []
    ndvi: dict[str, dict[str, Any]] = {str(year): {} for year in range(2021, 2026)}
    for farm_dir in farms:
        farm_slug = _slug(farm_dir.name, "farm slug")
        farm_meta = _json_file(farm_dir / "farm.json")
        farm_name = str(farm_meta.get("display_name") or farm_slug)
        boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
        if not boundary_path.is_file():
            raise FileNotFoundError(f"Canonical farm boundary is missing: {boundary_path}")
        farm_fields: list[str] = []
        acres = 0.0
        has_acres = False
        for source_feature in _read_geojson(boundary_path):
            geometry = source_feature.get("geometry")
            if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                raise ValueError(f"Expected a polygon field boundary in {boundary_path}")
            source_props = source_feature.get("properties") or {}
            field_id = str(source_props.get("field_id") or source_props.get("field_slug") or "").strip()
            if not field_id:
                raise ValueError(f"Field feature lacks field_id in {boundary_path}")
            field_slug = str(source_props.get("field_slug") or field_id)
            field_dir = farm_dir / "fields" / field_slug
            field_name = str(source_props.get("field_name") or source_props.get("name") or field_id)
            properties: dict[str, Any] = {
                "grower": str(grower_meta.get("display_name") or grower_slug),
                "grower_slug": grower_slug,
                "farm": farm_name,
                "farm_slug": farm_slug,
                "field": field_name,
                "field_id": field_id,
                "field_slug": field_slug,
            }
            for key in ("county", "state", "crop", "area_acres", "area_ha_est", "source"):
                value = source_props.get(key)
                if value not in (None, ""):
                    properties[key] = value
            if source_props.get("area_acres") not in (None, ""):
                try:
                    acres += float(source_props["area_acres"])
                    has_acres = True
                except (TypeError, ValueError):
                    pass
            field_feature = {"type": "Feature", "geometry": geometry, "properties": properties}
            all_fields.append(field_feature)
            farm_fields.append(field_id)

            soil_features, soil_insight = _soil_features(
                field_dir, field_id, field_name, geometry
            )
            soils.extend(soil_features)
            properties["soil_status"] = soil_insight["status"]
            properties["soil_insight"] = soil_insight

            yearly_summary_path = field_dir / "derived" / "summaries" / "ndvi_yearly_summary.json"
            yearly_summary = _json_file(yearly_summary_path) if yearly_summary_path.is_file() else {}
            yearly_entries = yearly_summary.get("years", [])
            by_year = {str(entry.get("year")): entry for entry in yearly_entries}
            for year_text in ndvi:
                entry = by_year.get(year_text, {})
                product = _ndvi_png(field_dir, int(year_text), geometry)
                scene_info = _manifest_scene_info(field_dir, int(year_text))
                scene_info["scene_count"] = entry.get("scene_count") or scene_info["scene_count"]
                product.update(
                    {
                        "year": int(year_text),
                        "source": scene_info["source"],
                        "date_start": scene_info["date_start"],
                        "date_end": scene_info["date_end"],
                        "scene_counts": scene_info["scene_counts"],
                        "scene_count": scene_info["scene_count"],
                        "insight": _ndvi_insight(
                            year=int(year_text), product=product, scene_info=scene_info
                        ),
                    }
                )
                ndvi[year_text][field_id] = product
        output_farms.append(
            {
                "slug": farm_slug,
                "name": farm_name,
                "field_ids": farm_fields,
                "field_count": len(farm_fields),
                "acres": round(acres, 1) if has_acres else None,
            }
        )

    if not all_fields:
        raise ValueError(f"No field polygons found for grower {grower_slug}")
    return {
        "grower": str(grower_meta.get("display_name") or grower_slug),
        "grower_slug": grower_slug,
        "farm_count": len(output_farms),
        "field_count": len(all_fields),
        "farms": output_farms,
        "fields": all_fields,
        "soils": soils,
        "ndvi": ndvi,
    }


def render_html(data: dict[str, Any]) -> str:
    title = html.escape(f"{data['grower']} — Field Map", quote=True)
    payload = _safe_json(data)
    template = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    :root { color-scheme: light; --ink:#18251d; --muted:#66756a; --line:#d7dfd8; --panel:#f7f9f7; }
    * { box-sizing:border-box; }
    html,body { height:100%; margin:0; color:var(--ink); font:14px system-ui,-apple-system,"Segoe UI",sans-serif; }
    #layout { display:grid; grid-template-columns:minmax(270px,330px) minmax(0,1fr); height:100%; }
    #sidebar { overflow:auto; padding:15px; background:var(--panel); border-right:1px solid var(--line); }
    #map { min-height:360px; height:100%; }
    h1 { margin:0; font-size:19px; line-height:1.2; }
    h2 { font-size:13px; margin:0 0 9px; }
    .muted,.small { color:var(--muted); font-size:12px; }
    .summary { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; margin:12px 0; }
    .stat { background:white; border:1px solid var(--line); border-radius:7px; padding:8px; }
    .stat strong { display:block; font-size:16px; }
    .stat span { color:var(--muted); font-size:11px; }
    .panel { background:white; border:1px solid var(--line); border-radius:8px; margin:10px 0; padding:11px; }
    .farm-group { margin:10px 0; }
    .farm-title { font-weight:700; font-size:12px; margin:0 0 5px; }
    .field-button { display:block; width:100%; text-align:left; padding:8px 9px; border:1px solid transparent; border-radius:5px; background:transparent; cursor:pointer; color:inherit; }
    .field-button:hover { background:#edf3ee; }
    .field-button[aria-current="true"] { background:#e3f0e5; border-color:#4c8a59; box-shadow:inset 3px 0 #347444; }
    .field-sub { display:block; color:var(--muted); font-size:11px; margin-top:2px; }
    .control-row { display:flex; align-items:center; gap:8px; margin:7px 0; }
    .control-row input { accent-color:#387348; }
    select, .action { border:1px solid #b9c6bb; border-radius:5px; background:white; padding:6px 8px; color:inherit; }
    .action { width:100%; cursor:pointer; font-weight:600; }
    .legend-bar { height:11px; border-radius:8px; margin:9px 0 4px; background:linear-gradient(90deg,#804823,#c48136,#e8cd5b,#b1cc53,#5ba64a,#1d703b); }
    .legend-scale { display:flex; justify-content:space-between; color:var(--muted); font-size:10px; }
    #ndvi-status { line-height:1.45; }
    #field-insights h3 { font-size:12px; margin:10px 0 5px; }
    #field-insights p { margin:5px 0; font-size:12px; line-height:1.4; }
    #field-insights p strong { color:#34483a; }
    details summary { cursor:pointer; font-size:12px; font-weight:600; }
    #soil-legend { display:grid; grid-template-columns:1fr 1fr; gap:4px 8px; margin-top:8px; max-height:190px; overflow:auto; }
    .soil-key { display:flex; align-items:flex-start; gap:5px; font-size:10px; overflow-wrap:anywhere; }
    .swatch { flex:0 0 10px; height:10px; border:1px solid #415246; margin-top:1px; }
    .leaflet-popup-content { line-height:1.45; }
    .popup-title { font-weight:700; }
    @media(max-width:760px) { #layout { grid-template-columns:1fr; grid-template-rows:minmax(220px,34vh) minmax(0,1fr); } #sidebar { order:2; border-right:0; border-top:1px solid var(--line); } #map { order:1; min-height:220px; } }
  </style>
  <script src="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
</head>
<body>
<div id="layout">
  <aside id="sidebar">
    <h1>__TITLE__</h1>
    <div class="summary">
      <div class="stat"><strong id="farm-count"></strong><span>Farms</span></div>
      <div class="stat"><strong id="field-count"></strong><span>Fields</span></div>
      <div class="stat"><strong id="acreage"></strong><span>Acres</span></div>
    </div>
    <section class="panel"><h2>Layers</h2>
      <div class="control-row"><input id="soil-toggle" type="checkbox"><label for="soil-toggle">SSURGO soils</label></div>
      <div class="control-row"><input id="ndvi-toggle" type="checkbox"><label for="ndvi-toggle">NDVI</label>
        <select id="ndvi-year" aria-label="NDVI composite year"></select></div>
      <div id="ndvi-legend" hidden><div class="small">NDVI: lower to higher</div><div class="legend-bar"></div><div class="legend-scale"><span>−0.1</span><span>0.9</span></div><div id="ndvi-status" class="small"></div></div>
      <div id="soil-status" class="small"></div>
      <details id="soil-legend-details" hidden><summary>Soil map-unit legend / MUKEY</summary><div id="soil-legend"></div></details>
      <div class="small" style="margin-top:8px">Soils: USDA NRCS SSURGO. NDVI imagery: Sentinel-2 © ESA/Copernicus and Landsat © USGS/NASA. Basemap: Esri World Imagery.</div>
    </section>
    <section class="panel"><h2>Fields by farm</h2><div id="field-list"></div>
      <button class="action" id="full-view" type="button" style="margin-top:10px">Show full grower</button>
    </section>
    <section id="field-details" class="panel" hidden></section>
    <section id="field-insights" class="panel" hidden aria-live="polite"></section>
  </aside>
  <main id="map" aria-label="Interactive grower field map"></main>
</div>
<script id="grower-data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('grower-data').textContent);
  const map = L.map('map', { zoomControl:true });
  map.createPane('ndviPane'); map.getPane('ndviPane').style.zIndex = 330;
  map.createPane('soilPane'); map.getPane('soilPane').style.zIndex = 350;
  map.createPane('fieldPane'); map.getPane('fieldPane').style.zIndex = 410;
  L.tileLayer(__TILE__, { maxZoom:19, attribution:'Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community' }).addTo(map);
  const fieldLayers = new Map(), soilLayers = [], ndviLayers = [];
  const fieldById = new Map(data.fields.map(f => [f.properties.field_id, f]));
  let selectedId = null;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fullBounds = L.featureGroup();
  const list = document.getElementById('field-list');
  document.getElementById('farm-count').textContent = data.farm_count;
  document.getElementById('field-count').textContent = data.field_count;
  const totalAcres = data.farms.every(f => f.acres !== null) ? data.farms.reduce((sum,f) => sum + f.acres,0) : null;
  document.getElementById('acreage').textContent = totalAcres === null ? '—' : Math.round(totalAcres).toLocaleString();
  const details = document.getElementById('field-details');
  const insightCard = document.getElementById('field-insights');
  function updateFieldInsights(id=selectedId) {
    if (!id || !fieldById.has(id)) { insightCard.hidden=true; return; }
    const props=fieldById.get(id).properties, sections=[];
    if (document.getElementById('soil-toggle').checked) sections.push({title:'SSURGO soils',insight:props.soil_insight});
    if (document.getElementById('ndvi-toggle').checked) {
      const product=(data.ndvi[document.getElementById('ndvi-year').value]||{})[id];
      if (product) sections.push({title:`NDVI · ${product.year}`,insight:product.insight});
    }
    insightCard.replaceChildren();
    const heading=document.createElement('h2');heading.textContent=`Field Insights · ${props.field}`;insightCard.appendChild(heading);
    if (!sections.length) {
      const prompt=document.createElement('p');prompt.className='small';prompt.textContent='Turn on SSURGO soils or NDVI to see a field-specific explanation.';insightCard.appendChild(prompt);
    }
    for (const section of sections) {
      const sectionHeading=document.createElement('h3');sectionHeading.textContent=section.title;insightCard.appendChild(sectionHeading);
      const insight=section.insight||{};
      for (const [label,key] of [['What this layer shows','shows'],['What we see in this field','observes'],["How I’d explain it to a grower",'explains']]) {
        const row=document.createElement('p'), strong=document.createElement('strong'), text=document.createElement('span');
        strong.textContent=label+': ';text.textContent=insight[key]||'This result is not available.';
        row.append(strong,text);insightCard.appendChild(row);
      }
    }
    insightCard.hidden=false;
  }
  function selectField(id, openPopup=true) {
    selectedId = id;
    document.querySelectorAll('.field-button').forEach(b => b.setAttribute('aria-current', String(b.dataset.fieldId === id)));
    for (const [key, layer] of fieldLayers) layer.setStyle(key === id
      ? {color:'#fff',weight:4,opacity:1,fillColor:'#ffe45e',fillOpacity:0.16}
      : {color:'#18231d',weight:3,opacity:1,fillColor:'#ffdc48',fillOpacity:0.025});
    const feature = fieldById.get(id), layer = fieldLayers.get(id);
    if (!feature || !layer) return;
    const p = feature.properties;
    const rows = [['Grower',p.grower],['Farm',p.farm],['Field',p.field],['Field ID',p.field_id],['County',p.county],['State',p.state],['Crop',p.crop],['Area (acres)',p.area_acres],['Boundary source',p.source],['SSURGO',p.soil_status]];
    details.hidden = false;
    details.innerHTML = '<h2>'+esc(p.field)+'</h2>'+rows.filter(([,v]) => v !== undefined && v !== null && v !== '').map(([k,v]) => '<div class="small"><b>'+esc(k)+':</b> '+esc(v)+'</div>').join('');
    updateFieldInsights(id);
    if (openPopup) { map.fitBounds(layer.getBounds(),{padding:[26,26],maxZoom:16}); layer.openPopup(); }
  }
  for (const farm of data.farms) {
    const group = document.createElement('div'); group.className = 'farm-group';
    const heading = document.createElement('div'); heading.className='farm-title'; heading.textContent = `${farm.name} · ${farm.field_count} field${farm.field_count===1?'':'s'}${farm.acres===null?'':` · ${farm.acres.toLocaleString()} ac`}`;
    group.appendChild(heading);
    for (const id of farm.field_ids) {
      const feature = fieldById.get(id); if (!feature) continue;
      const p = feature.properties, button = document.createElement('button');
      button.type='button'; button.className='field-button'; button.dataset.fieldId=id;
      button.innerHTML=esc(p.field)+'<span class="field-sub">'+esc(p.crop||'Field')+(p.area_acres?` · ${esc(p.area_acres)} ac`:'')+'</span>';
      button.addEventListener('click',()=>selectField(id)); group.appendChild(button);
    }
    list.appendChild(group);
  }
  data.fields.forEach(feature => {
    const id=feature.properties.field_id;
    const layer=L.geoJSON(feature,{pane:'fieldPane',style:{color:'#18231d',weight:3,opacity:1,fillColor:'#ffdc48',fillOpacity:0.025}}).addTo(map);
    layer.on('click',()=>selectField(id,false));
    const p=feature.properties;
    layer.bindPopup('<div class="popup-title">'+esc(p.field)+'</div><div>'+esc(p.grower)+' · '+esc(p.farm)+'</div><div>'+esc(p.area_acres??'')+(p.area_acres?' acres':'')+'</div>');
    fieldLayers.set(id,layer); fullBounds.addLayer(layer);
  });
  function soilColor(key) {
    let hash=0; for (const c of String(key)) hash=(hash*31+c.charCodeAt(0))|0;
    return `hsl(${Math.abs(hash)%360} 58% 58%)`;
  }
  const soilLabels=new Map();
  data.soils.forEach(f=>{const p=f.properties,key=String(p.mukey||'unknown');const name=p.muname||p.mapunit_name||p.musym||p.mapunit_symbol;soilLabels.set(key,name?`MUKEY ${key} · ${name}`:`MUKEY ${key}`);});
  const soilKeys=[...soilLabels.keys()].sort();
  const soilLegend=document.getElementById('soil-legend');
  soilKeys.forEach(key=>{
    const row=document.createElement('div');row.className='soil-key';
    const swatch=document.createElement('span');swatch.className='swatch';swatch.style.background=soilColor(key);
    const label=document.createElement('span');label.textContent=soilLabels.get(key);
    row.append(swatch,label);soilLegend.appendChild(row);
  });
  data.soils.forEach(feature=>{
    const key=String(feature.properties.mukey||'unknown');
    const layer=L.geoJSON(feature,{pane:'soilPane',style:{color:'#17251d',weight:0.7,fillColor:soilColor(key),fillOpacity:0.58}});
    layer.on('click',event=>{
      const p=feature.properties; selectField(p.field_id,false);
      const name=p.muname||p.mapunit_name||p.musym||p.mapunit_symbol;
      const rows=[`<b>SSURGO map unit</b>${name?' · '+esc(name):''}`,`MUKEY: ${esc(key)}`];
      if(p.dominant_component)rows.push(`Dominant component: ${esc(p.dominant_component)}`);
      layer.bindPopup(rows.join('<br>')).openPopup(event.latlng);
    });
    soilLayers.push(layer);
  });
  const soilToggle=document.getElementById('soil-toggle');
  soilToggle.addEventListener('change',()=>{
    soilLayers.forEach(layer=>soilToggle.checked?layer.addTo(map):map.removeLayer(layer));
    updateFieldInsights();
  });
  document.getElementById('soil-status').textContent = data.fields.map(f=>`${f.properties.field_id}: ${f.properties.soil_status}`).join(' · ');
  document.getElementById('soil-legend-details').hidden=!data.soils.length;
  if(!data.soils.length) soilToggle.disabled=true;
  const yearSelect=document.getElementById('ndvi-year');
  Object.keys(data.ndvi).sort().forEach(y=>{const option=document.createElement('option');option.value=y;option.textContent=y;yearSelect.appendChild(option);});
  yearSelect.value='2025';
  const ndviToggle=document.getElementById('ndvi-toggle');
  function refreshNdvi(){
    ndviLayers.forEach(layer=>map.removeLayer(layer));ndviLayers.length=0;
    const year=yearSelect.value, products=data.ndvi[year]||{};
    const summary=[];
    for(const field of data.fields){
      const p=field.properties, product=products[p.field_id];
      if(!product||product.status==='unavailable'){summary.push(`${p.field_id}: unavailable`);continue;}
      const image=L.imageOverlay('data:image/png;base64,'+product.png,product.bounds,{pane:'ndviPane',opacity:0.84,interactive:false});
      ndviLayers.push(image);if(ndviToggle.checked)image.addTo(map);
      const partial=product.status==='partial'?` (${Math.round(product.valid_fraction*100)}% valid pixels)`:'';
       const provenance=product.source&&product.date_start&&product.date_end?`${product.source}, ${product.date_start}–${product.date_end}`:'source/date details unavailable';
       summary.push(`${p.field_id}: ${provenance}${partial}`);
    }
    document.getElementById('ndvi-status').textContent=summary.join(' · ');
  }
  ndviToggle.addEventListener('change',()=>{ndviLayers.forEach(layer=>ndviToggle.checked?layer.addTo(map):map.removeLayer(layer));document.getElementById('ndvi-legend').hidden=!ndviToggle.checked;});
  ndviToggle.addEventListener('change',()=>updateFieldInsights());
  yearSelect.addEventListener('change',()=>{refreshNdvi();updateFieldInsights();});refreshNdvi();
  document.getElementById('full-view').addEventListener('click',()=>{selectedId=null;details.hidden=true;insightCard.hidden=true;document.querySelectorAll('.field-button').forEach(b=>b.setAttribute('aria-current','false'));for(const layer of fieldLayers.values())layer.setStyle({color:'#18231d',weight:3,opacity:1,fillColor:'#ffdc48',fillOpacity:0.025});if(fullBounds.getLayers().length)map.fitBounds(fullBounds.getBounds(),{padding:[32,32]});});
  if(fullBounds.getLayers().length)map.fitBounds(fullBounds.getBounds(),{padding:[32,32]});else map.setView([39.5,-98.35],4);
})();
</script>
</body>
</html>
'''
    return (
        template.replace("__TITLE__", title)
        .replace("__LEAFLET__", LEAFLET_VERSION)
        .replace("__DATA__", payload)
        .replace("__TILE__", json.dumps(TILE_URL))
    )


def generate(grower_dir: Path) -> Path:
    data = collect_grower(grower_dir)
    output = grower_dir / "derived" / "dashboards" / "grower_web_map.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(data), encoding="utf-8")
    print(f"{grower_dir.name}: {data['field_count']} fields, {len(data['soils'])} SSURGO polygons -> {output}")
    print(f"  HTML size: {output.stat().st_size:,} bytes")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--grower-slug", help="Generate one grower's map")
    selection.add_argument("--all", action="store_true", help="Generate maps for all growers with boundaries")
    args = parser.parse_args(argv)
    try:
        growers_root = _runtime_base() / "growers"
        if args.grower_slug:
            grower_dirs = [growers_root / _slug(args.grower_slug, "grower slug")]
            if not grower_dirs[0].is_dir():
                raise FileNotFoundError(f"Grower not found: {grower_dirs[0]}")
        else:
            grower_dirs = sorted(
                path for path in growers_root.iterdir()
                if path.is_dir() and any(path.glob("farms/*/boundary/field_boundaries.geojson"))
            ) if growers_root.exists() else []
        if not grower_dirs:
            raise FileNotFoundError(f"No growers found under {growers_root}")
        for grower_dir in grower_dirs:
            generate(grower_dir)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
