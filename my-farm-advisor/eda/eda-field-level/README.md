# Field-Level EDA

Document-routed workflow for learning from selected farm-field datasets with
reproducible, static Python figures and tables. It emphasizes field identity,
source resolution, denominators, units, and limitations before interpretation.

The workflow audits field boundaries and produces acreage summaries, USDA
Cropland Data Layer (CDL) composition/history summaries, NASA POWER seasonal
weather comparisons, and a static geographic context map. It does not require
soil analysis and does not create an interactive dashboard, web app, or map.

Start with [GUIDE.md](GUIDE.md). The reusable script is
[`scripts/field_level_eda.py`](scripts/field_level_eda.py). Inputs are read from
the canonical runtime tree; generated outputs belong outside the skill
checkout, under the configured runtime root.

## Skill and output locations

The custom **Field-Level EDA** skill lives at
`my-farm-advisor/eda/eda-field-level/`. It generates static boundary, CDL,
weather, and geographic-context figures and tables. Reusable analysis outputs
are saved under
`${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/assignment-2-field-level/`;
one-time narrative HTML reports are assembled separately under
`my-farm-advisor/docs/`.
