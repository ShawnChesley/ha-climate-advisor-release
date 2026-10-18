# Maintained CA release fork

This fork is based on gunkl/ClimateAdvisor v0.7.106, upstream commit cb6d340de82cb6fffc9ff8f7addb03603f7b7b75. The upstream MIT license and attribution are retained.

Version0.7.106.1 widens only the sleep-cooling input range to match the existing sleep-heating lower bound (55°F /12.5°C). Default72°F, scheduling, daytime comfort, and actuation are unchanged. Users must stay within their thermostat's supported bounds.

Release gates: regression tests for64/65°F and Celsius round-trips; bedtime and morning band regressions; upstream configuration and bedtime tests; HACS and hassfest; official HA2026.10.2 integration harness with maintained MXZ3.4.1, its full suite and CA/MXZ compatibility tests. Never publish a new tag until gates pass.

HACS must track ShawnChesley/ha-climate-advisor-release, integration domain climate_advisor. Preserve the existing HA config entry and entities during source changes. Keep automatic updates off; compare upstream changes, review/merge into a development branch, and pass the compatibility workflow before releasing. No unversioned edits on live HA.

Rollback: pause CA automation, install prior tested fork release or verified stock-compatible pair, restart, and verify targets before re-enabling. Values below stock CA's69°F sleep-cooling form minimum must be reviewed before returning to stock.
