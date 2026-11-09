---
name: grower-web-map
description: Generate a lightweight, interactive satellite-basemap HTML map from a grower's canonical farm field boundaries.
license: Apache-2.0
---

# Grower Web Map

Use this subskill to build one interactive HTML map for every farm belonging
to a grower. It reads canonical `boundary/field_boundaries.geojson` files and
writes maps under the runtime grower's `derived/dashboards/` directory. It can
include inline, clipped SSURGO and yearly NDVI layers from existing runtime
products, with short template-based field insights; it does not fetch data or
embed raw satellite imagery or GeoTIFFs.

Read [README.md](README.md) for installation, invocation, and output details.
