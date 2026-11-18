# Field-Level Exploratory Data Analysis

## Purpose and routing

Use this workflow to compare field size, mapped crop composition/history, and
weather across selected fields and growers. It is a document-routed workflow
under the My Farm Advisor EDA area, not a separate interactive application.
The runnable workflow handles boundary identity, acreage summaries, CDL
composition/history, weather summaries by unique source grid, and a static
geographic context figure. It uses static Python outputs only.

The population is the selected field sample, not a state or national census.
Fields nested in one grower are not automatically independent observations.
The workflow requires no soil analysis.

## Runtime and output contract

Set `DATA_PIPELINE_DATA_ROOT` to an absolute path outside the repository, or
pass `--data-root`. The script reads:

```text
<root>/data-pipeline/growers/<grower>/farms/<farm>/
├── farm.json
├── boundary/field_boundaries.geojson
├── manifests/field-inventory.csv
├── manifests/csb_fields_provenance.json
└── fields/<field_slug>/boundary/field_boundary.geojson
```

For this assignment, the default grower slugs are
`assignment-illinois-grower`, `assignment-iowa-grower`, and
`assignment-nebraska-grower`. The script requires exactly one farm directory
per selected grower, but accepts `--growers` to select another set. It does
not infer state from a grower slug: boundary properties are the field-level
location evidence.

Unless overridden with `--output-dir`, outputs go to:

```text
<root>/data-pipeline/eda/assignment-2-field-level/
```

Generated figures and tables must remain outside Git. Nothing from the runtime
data tree is copied into the repository.

## Boundary stage (implemented)

Run from any directory with the installed data-pipeline environment:

```bash
export DATA_PIPELINE_DATA_ROOT="$HOME/my-farm-advisor-runtime"
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  /path/to/my-farm-advisor-skills/my-farm-advisor/eda/eda-field-level/scripts/field_level_eda.py \
  --data-root "$DATA_PIPELINE_DATA_ROOT"
```

The script validates the farm boundary GeoJSON, inventory, field IDs, area
attributes, per-field boundary files, source properties, and farm metadata.
It records metadata mismatches as warnings rather than silently rewriting
runtime files. For example, a farm-level state that disagrees with all of its
boundary state values is reported; charts use the boundary state and grower
slug as labels.

The default run executes stages in order: boundaries, CDL, weather, then map.
Select a subset or configure paths and periods as follows:

```bash
python scripts/field_level_eda.py \
  --data-root "$HOME/my-farm-advisor-runtime" \
  --growers assignment-illinois-grower assignment-iowa-grower assignment-nebraska-grower \
  --stages boundaries cdl weather map \
  --cdl-years 2019,2020,2021,2022,2023 \
  --common-cdl-year 2023 \
  --weather-years 2021,2022,2023,2024,2025 \
  --season-start 04-01 \
  --season-end 10-31 \
  --output-dir "$HOME/my-farm-advisor-runtime/data-pipeline/eda/assignment-2-field-level"
```

Use `--stages boundaries` to run only the acreage stage. The workflow does not
download source data or install dependencies.

The outputs are:

| File | Contents |
|---|---|
| `boundary_acreage_boxplot.png` | One acreage value and point per field, grouped by grower |
| `boundary_acreage_histograms.png` | Field counts in common 50-acre bins |
| `boundary_field_sizes.csv` | One row per field with grower, farm, state/county, and acreage |
| `boundary_size_summary.csv` | Field count, total, median, minimum, maximum, and range in acres |
| `boundary_data_quality.md` | Identity, metadata, area, coordinate, CRS, and provenance checks and caveats |
| `cdl_2023_dominant_crop_counts.png` | Dominant CDL class counts among fields with valid 2023 coverage |
| `cdl_dominant_crop_by_field_year.png` | Categorical dominant-class heatmap for fields and configured years |
| `cdl_field_year_comparison.csv` | Per-field/year Corn, Soybean, other-class percentages and coverage |
| `weather_seasonal_rainfall_by_location_year.png` | Matched-season precipitation totals by unique source grid and year |
| `weather_seasonal_temperature_by_location_year.png` | Daily temperature distributions by source grid and year |
| `weather_seasonal_comparison.csv` | Per-grid/year seasonal precipitation, temperature summaries, and valid-day counts |
| `grower_field_boundary_context.png` | Static overview and local panels with actual field boundaries |
| `field_eda_data_quality.md` | Cross-stage inputs, source-grid assignments, missingness, and warnings |

### What the boundary calculations mean

Each unique `field_id` contributes one observation. `area_acres` is the
boundary source's reported field area; the summary reports its count, sum,
median, minimum, maximum, and range. The median is the middle acreage after
sorting fields (or the mean of the two middle values for an even field count).
It describes a typical field in this selected sample and is less sensitive to
one very large field than the mean.

The boxplot shows the median and interquartile range, with the individual
fields overlaid so a reader can see the small sample. The histogram uses the
same 50-acre bins and x-range for every grower. These are descriptive
summaries, not tests of statistically representative grower populations.

Quality checks include:

- exactly one farm per requested grower;
- unique IDs and agreement between the farm boundary, inventory, and
  per-field boundary files;
- positive finite `area_acres`, with a consistency comparison to
  `area_ha_est × 2.47105`;
- longitude/latitude coordinate ranges and the CRS recorded in farm metadata;
- field-level state/county consistency and any disagreement with `farm.json`;
- source and `csb_id` coverage, plus whether the source provenance manifest
  names every current field.

GeoJSON from the inspected runtime has no explicit `crs` member. Farm metadata
declares EPSG:4326 and the coordinates are longitude/latitude; the source
provenance identifies the upstream CSB data CRS as Albers Conic Equal Area.
Those are different stages in the source-to-output path and must not be
confused.

## CDL and crop-history analysis

Read yearly tables under each farm's `derived/tables/`, not the preassembled
farm report. The expected schema is `field_id`, `year`, `crop_code`,
`crop_name`, `pixel_count`, `pct`, and `crop_source`. Use only common available
years for direct comparisons. A dominant class is the class with the largest
reported percentage for that field-year; report its share and mapped coverage
alongside the label.

The current source implementation computes `pct` using all rasterized pixels
inside the field geometry, including class 0 (“No Data”), then drops No Data
rows and unrecognized `Code_*` labels from the summary table. Consequently,
the remaining reported percentages may sum to less than 100. The analysis
rechecks pixel counts from the local CDL rasters and reports:

- `valid_coverage_pct`: non-zero CDL pixels divided by all rasterized
  in-boundary pixels;
- `*_pct_valid_pixels`: the class-group share among valid non-zero pixels;
- `*_pct_full_boundary`: the class-group share among all in-boundary pixels.

Corn is code 1 and Soybeans is code 5. “Other” includes all remaining
non-zero codes; class 0 is kept as unmapped coverage, not “Other.” The
comparison CSV also preserves pixel counts, dominant class/share, and source
table percentage sums. Ties are identified. A dominant label is not evidence
that the whole field has that crop. Rotation-prediction columns are heuristic
and are not used as observed crop history.

The dominant-crop count chart uses the configured common year (default 2023).
The field-year heatmap uses all configured CDL years. Its observation is one
field-year; a missing field-year remains visually missing.

### Weather analysis

Read farm-level daily CSVs under `derived/tables/` and per-field weather
metadata. Expected variables include `T2M`, `T2M_MAX`, `T2M_MIN`,
`PRECTOTCORR`, `ALLSKY_SFC_SW_DWN`, `RH2M`, and `WS10M`. Documented units are
°C, mm/day, MJ/m²/day, %, and m/s respectively. Preserve valid-day counts.
Never replace missing precipitation with zero.

The proposed shared comparison window is April 1–October 31 (214 days).
It broadly covers a warm-season interval for these Corn Belt corn/soy samples,
but is not an observed planting-to-harvest window. The data contains no
field-specific operation dates.

NASA POWER is sampled on a gridded basis. Derive source grid keys using the
pipeline's assignment method and count fields per grid. For analysis, one
weather observation is a unique grid-cell/date, not a field/date when several
fields share the same cell. Report source-cell counts and valid-day counts;
do not treat repeated field series as independent observations.

The current comparison CSV has one row per unique grid/year. Rainfall totals
are emitted only when every expected seasonal date has a valid
`PRECTOTCORR` value; missing rainfall is never treated as zero. Temperature
summaries use available `T2M` days and report their count. The daily
temperature boxplots are descriptive: days are temporally correlated and
NASA POWER grid cells are not independent weather stations.

### Static context map

Use the locally available state/county geoadmin boundaries and farm GeoJSON.
Create a static overview with state/local insets as needed. Map polygons are
CSB crop-sequence boundaries, not verified ownership parcels; selected
locations do not establish representativeness. Do not use an interactive map
or web map for this deliverable.

## Review and interpretation checklist

1. Review boundary summaries before adding CDL.
2. Verify CDL percentage denominators and field-year coverage before crop
   composition claims.
3. Check weather grid sharing and date coverage before summarizing seasons.
4. Review the static map against field IDs and human-readable state/county
   labels.
5. Compare every chart with its underlying CSV and retain unsupported-data
   caveats in the quality/provenance note.

The reusable subskill produces analysis assets only. Assembly of a one-time
narrative EDA report is a separate step.
