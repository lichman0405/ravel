# 12 — POST Boundary

POST and RAVEL are separate projects with independent release schedules.

RAVEL V0:
- must be complete without POST
- must not import POST code/schema
- must not wait for POST API
- must not share POST database

Future integration should be adapter-based.

For future compatibility only, RAVEL domain objects should have:
- stable ID
- version
- provenance
- typed relations
- artifact references
- event representation

Do not define a "POST standard interface" before POST V0 is stable.

Potential future semantic mappings are tentative:
- Project
- Experiment
- Calculation
- Dataset
- Evidence
- Protocol
- Hypothesis
- Decision
- Review

RAVEL runtime remains owner of live execution even after future integration.
