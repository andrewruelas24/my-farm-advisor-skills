# Grower Web Map

Creates one standalone, interactive HTML map per grower from canonical farm
field boundaries and available runtime soil/vegetation products. The map keeps
the satellite basemap prominent and includes a grower summary, farm-grouped
field list, selection/highlighting, full-grower navigation, and layer controls.

## Runtime data used

- Field polygons: `growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson`.
- SSURGO: per-field `soil/ssurgo_soil_types.geojson`; available map-unit keys and
  soil component names from farm `derived/tables/*soil*.csv` or
  `*ssurgo*.csv`. Missing soil polygons are reported per field. Existing files
  may not contain SSURGO map-unit names or symbols; the map falls back to MUKEY.
- Yearly NDVI: per-field
  `derived/features/ndvi_year_<year>_composite.tif`, with scene/date details
  read from `derived/summaries/ndvi_yearly_summary.json` and
  `satellite/{sentinel,landsat}/manifest.json`. Missing rasters or rasters with
  no finite in-field pixels are explicitly marked unavailable.

The generator does not fetch data or require credentials. If a field lacks
soil polygons, source or process that field's SSURGO products through the
existing USDA NRCS SSURGO/SDA pipeline before expecting coverage. USDA SDA is
public and requires no credentials; this map generator does not call it.

## Runtime installation

The data-pipeline installer synchronizes its canonical `src/` tree, while this
subskill is maintained beside that tree. Install/update only this subskill into
the existing runtime without replacing runtime farm data:

```bash
export DATA_PIPELINE_DATA_ROOT="$HOME/my-farm-advisor-runtime"
rsync -r --no-times --checksum \
  my-farm-advisor/data-pipeline/grower-web-map/ \
  "$DATA_PIPELINE_DATA_ROOT/data-pipeline/src/grower-web-map/"
```

If a later `scripts/install.sh --force-refresh` replaces the whole runtime
`src/` tree, repeat this targeted sync. No Python dependency is added; the
runtime pipeline environment already supplies Rasterio, NumPy, and Pillow for
rendering compact NDVI overlays.

## Generate maps

```bash
export DATA_PIPELINE_DATA_ROOT="$HOME/my-farm-advisor-runtime"
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
  "$DATA_PIPELINE_DATA_ROOT/data-pipeline/src/grower-web-map/scripts/generate_grower_map.py" \
  --all
```

Use `--grower-slug <slug>` to generate one map. `--all` selects growers with at
least one canonical farm boundary and ignores empty scaffolding. Outputs are:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/derived/dashboards/grower_web_map.html
```

The HTML embeds the boundary and soil vectors plus transparent, field-clipped,
small PNG previews derived from yearly NDVI composites. It does not embed raw
GeoTIFFs or satellite imagery and needs no companion files or local server.
Open a downloaded HTML copy directly in Chrome. Internet access is required
for the Leaflet CDN and Esri World Imagery tiles; all grower geometries and
generated overlay previews remain in the HTML itself. Generated grower data
and credentials belong under the runtime root, never in the public fork.

## Field Insights

The selected-field card is generated locally from existing products using
short templates; it calls no AI service and downloads no data. Soil coverage is
the area of the union of field-clipped SSURGO polygons divided by the field
area in EPSG:5070. Leading map units are ranked by mapped area; the card shows
MUKEYs and, when matched, their dominant component and available drainage
class. A component name is not presented as a map-unit name. Missing or
overlapping geometry that prevents reliable coverage suppresses the percentage.

For NDVI, the generator masks each yearly per-pixel mean composite to the field
and summarizes its mean and 10th/50th/90th percentiles. The selected year,
contributing scene count, sources, and date range come from the existing yearly
summary and satellite manifests. Statistics are withheld when fewer than 100
in-field pixels are valid or valid pixels cover less than 75% of the field
mask; coverage from 75% to 95% is labeled partial. The text never treats NDVI
color as a diagnosis. Layer visibility and year changes refresh the selected
field's card in the browser.

Run the focused template tests with the runtime Python:

```bash
"$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" -m unittest discover \
  -s "$DATA_PIPELINE_DATA_ROOT/data-pipeline/src/grower-web-map/tests"
```

## Verification

After generation, verify each map's source polygon count, field and farm IDs,
soil availability by field, yearly NDVI availability/date/source, and HTML
size. Confirm the NDVI layer is transparent outside each field and that soil
unit colors, the NDVI scale, USDA source attribution, field selection, list
synchronization, and full-grower extent work in a browser.
