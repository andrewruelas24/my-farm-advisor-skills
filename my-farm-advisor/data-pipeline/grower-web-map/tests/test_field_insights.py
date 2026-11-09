from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_grower_map as web_map


class SoilInsightTests(unittest.TestCase):
    def test_reports_mukey_component_drainage_and_measured_coverage(self) -> None:
        insight = web_map._soil_insight(
            coverage_percent=100.0,
            has_any_mapunit_labels=False,
            units=[
                {
                    "mukey": "175595",
                    "mapunit_name": None,
                    "mapunit_symbol": None,
                    "area_percent": 46.2,
                    "dominant_component": "Milford",
                    "component_share": 93.0,
                    "drainage_classes": ["Poorly drained"],
                }
            ],
        )
        self.assertIn("MUKEY", insight["observes"])
        self.assertIn("survey map-unit IDs, not soil names", insight["observes"])
        self.assertIn("46.2%", insight["observes"])
        self.assertIn("Milford", insight["observes"])
        self.assertIn("poorly drained", insight["explains"])
        self.assertIn("stay wet after rain", insight["explains"])

    def test_missing_soil_is_explicit_and_does_not_invent_coverage(self) -> None:
        insight = web_map._empty_soil_insight("No SSURGO polygons are available for this field.")
        self.assertEqual(insight["status"], "unavailable")
        self.assertIsNone(insight["coverage_percent"])
        self.assertIn("cannot be reported", insight["observes"])
        self.assertIn("no soil result", insight["explains"])


class NdviInsightTests(unittest.TestCase):
    def test_annual_mean_summary_includes_sources_dates_and_field_distribution(self) -> None:
        insight = web_map._ndvi_insight(
            year=2025,
            product={
                "status": "ready",
                "mean": 0.389,
                "p10": 0.358,
                "median": 0.389,
                "p90": 0.420,
            },
            scene_info={
                "source": "Sentinel-2 + Landsat",
                "scene_count": 16,
                "date_start": "2025-03-10",
                "date_end": "2025-11-16",
            },
        )
        self.assertIn("per-pixel mean composite", insight["observes"])
        self.assertIn("16 Sentinel-2 + Landsat scenes", insight["observes"])
        self.assertIn("2025-03-10 to 2025-11-16", insight["observes"])
        self.assertIn("0.358 to 0.420", insight["observes"])
        self.assertIn("not its cause", insight["explains"])
        self.assertIn("crop stage", insight["explains"])
        self.assertIn("field observations", insight["explains"])

    def test_unusable_composite_is_reported_without_statistics(self) -> None:
        insight = web_map._ndvi_insight(
            year=2024,
            product={"status": "unavailable", "reason": "Yearly NDVI composite is missing."},
            scene_info={"source": "Sentinel-2", "scene_count": 2, "date_start": "2024-05-01", "date_end": "2024-06-01"},
        )
        self.assertIn("2024 layer is unavailable", insight["observes"])
        self.assertIn("2 Sentinel-2 scenes", insight["observes"])
        self.assertIn("2024-05-01 to 2024-06-01", insight["observes"])
        self.assertIn("cannot support a field-specific reading", insight["explains"])


class InsightCardTests(unittest.TestCase):
    def test_card_refresh_is_wired_to_field_layers_and_year_control(self) -> None:
        html = web_map.render_html(
            {
                "grower": "Test grower",
                "grower_slug": "test-grower",
                "farm_count": 1,
                "field_count": 1,
                "farms": [],
                "fields": [],
                "soils": [],
                "ndvi": {},
            }
        )
        self.assertIn('id="field-insights"', html)
        self.assertIn("updateFieldInsights(id=selectedId)", html)
        self.assertIn("yearSelect.addEventListener('change',()=>{refreshNdvi();updateFieldInsights();})", html)
        self.assertIn("soilToggle.addEventListener('change'", html)
        self.assertIn("ndviToggle.addEventListener('change'", html)

    def test_each_ndvi_year_has_its_own_insight_for_the_year_selector(self) -> None:
        annual_insights = {
            str(year): {
                "field-1": {
                    "year": year,
                    "status": "ready",
                    "insight": {
                        "shows": "NDVI summarizes vegetation greenness.",
                        "observes": f"Verified year-specific observation {year}.",
                        "explains": "The color alone does not identify a cause.",
                    },
                }
            }
            for year in (2021, 2025)
        }
        html = web_map.render_html(
            {
                "grower": "Test grower",
                "grower_slug": "test-grower",
                "farm_count": 1,
                "field_count": 1,
                "farms": [],
                "fields": [],
                "soils": [],
                "ndvi": annual_insights,
            }
        )
        self.assertIn("Verified year-specific observation 2021.", html)
        self.assertIn("Verified year-specific observation 2025.", html)
        self.assertIn("data.ndvi[document.getElementById('ndvi-year').value]", html)
        self.assertIn("yearSelect.addEventListener('change',()=>{refreshNdvi();updateFieldInsights();})", html)


if __name__ == "__main__":
    unittest.main()
