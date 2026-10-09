# Botkeep disposable qualification probe

Synthetic test application only. Contains no DMO source, data or credentials.
Start command: `python -u main.py`.

Supply dummy values and their SHA256 digests through Botkeep Environment:
`BOTKEEP_PROBE_SECRET`, `BOTKEEP_PROBE_SECRET_SHA256`, `BOTKEEP_PROBE_ENV`,
`BOTKEEP_PROBE_ENV_SHA256`. Values are never printed; only presence/match booleans.

Supply disposable PostgreSQL connection keys through the same Environment:
`PROBE_PG_HOST`, `PROBE_PG_PORT`, `PROBE_PG_USER`, `PROBE_PG_PASSWORD`,
`PROBE_PG_DATABASE`, `PROBE_PG_CA_PEM`. TLS certificate verification is mandatory.

Commands: `verify`, `reconnect`, `metrics`, `load 60 512`, `crash_once`.
The crash command exits once with code 17 and persists a marker to prevent repeats.
The load command is bounded to 60 seconds and 512 MiB; use only with sufficient RAM.
GitHub sync must merge files to preserve `qualification_state/` across revisions.
