# Provenance

- Boundaries: runtime canonical farm `boundary/field_boundaries.geojson` files.
- SSURGO map-unit polygons and tabular soil component attributes: existing
  runtime products produced from USDA NRCS SSURGO/Soil Data Access (public,
  no credentials). The web-map generator does not fetch them.
- NDVI: existing per-field yearly composites and Sentinel-2/Landsat scene
  manifests produced by the data pipeline. Transparent PNG previews are
  rendered locally during map generation; raw imagery is not bundled.
- NDVI source attribution: Sentinel-2 © ESA/Copernicus; Landsat © USGS/NASA.
- Basemap: Esri World Imagery public tile service, fetched by the browser.
- Map library: Leaflet public CDN, fetched by the browser.
- Generated HTML embeds field geometries, soil vectors, compact derived NDVI
  previews, and lightweight metadata; it does not bundle raw rasters or
  satellite imagery.
