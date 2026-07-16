# Changelog

All notable changes to telomere-airflow-provider will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-16

Airflow 3 rewrite, built around a tracking guarantee: Telomere never reports
"completed" for a DAG run whose Airflow final state is failed; anything that
prevents explicit reporting degrades to a Telomere timeout alert, never to a
false success or silence.

### Changed

- **Requires Apache Airflow >= 3.0, < 4 and Python >= 3.10.** Airflow 2 (EOL
  2026-04-22) users should stay on 0.0.1.
- `enable_telomere_tracking` now injects a canary + finalize pair instead of
  the end/fail operator pair. The new shape reports **mid-graph failures**,
  which 0.0.1 silently missed (its `one_failed` rule on leaf tasks never fired
  when leaves ended `upstream_failed`), and mirrors Airflow's dag-run verdict
  in every terminal shape: branch skips, all-skipped runs, `all_done` recovery
  leaves, retried tasks, dynamic-mapped tasks, force-skipping operators.
- The injected tasks are Airflow teardowns: your own leaves keep deciding the
  dag-run state, and force-skips (e.g. default `ShortCircuitOperator`) can't
  skip the reporting tasks.
- Run-ID handoff between the injected tasks uses XCom instead of Airflow
  Variables (which leaked on unfinished runs and need metadata-DB access that
  Airflow 3 workers don't have).
- The dag-run failure message no longer lists failed task IDs (workers cannot
  read other task states in Airflow 3); it carries the run URL instead.
- Write requests (starting/ending runs) are no longer retried at the HTTP
  layer — a replayed `start_run` could double-start runs. Reads still retry;
  end/fail treat 409 ("already ended") as success so task retries stay
  idempotent.
- Schedule-deadline math is timezone-aware (0.0.1 compared naive/aware
  datetimes, so the precise deadline path never ran).

### Added

- `TelomereLifecycleOperator.on_kill`: externally stopped tasks (SIGTERM, UI
  mark-failed) report failure immediately instead of dangling to the timeout.
- Three-tier test suite: unit, a failure-mode matrix driving every terminal
  DAG shape through `dag.test()`, and an e2e tier against a real scheduler
  and the real Telomere API. CI runs all tiers on every push.
- Docker compose stack on Airflow 3.3 (api-server, scheduler, dag-processor,
  SimpleAuthManager with fixed dev credentials).

### Removed

- `TelomereDAGEndOperator` and `TelomereDAGFailOperator` (replaced by the
  injected canary/finalize pair).
- `get_connection_form_widgets` (depended on flask-appbuilder, which left
  Airflow core in 3.0). Connection extras are still honored.
- `setup.py` (pyproject-only builds).

## [0.0.1] - 2024-01-30

### Added
- Initial release of telomere-airflow-provider
