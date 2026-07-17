# Changelog

All notable changes to telomere-airflow-provider will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.1] - 2026-07-17

Telomere now enforces the documented running-only contract: `end`/`fail` on a
non-running run returns 409 instead of overwriting the prior resolution. The
provider already treated that 409 as success, so there is no runtime behavior
change — this release updates docs and test mocks to describe the enforced
contract as current fact.

### Changed

- Hook docstrings, the session-retry comment, and the tolerated-409 log line
  now state the enforced running-only contract instead of the old
  200-overwrite behavior.
- The matrix test fake (`FakeTelomere`) returns 409 on `end`/`fail` for
  non-running runs, matching prod; no DAG-flow test depended on overwrite
  semantics.

## [2.0.0] - 2026-07-16

DAG-level tracking now uses Airflow's listener API. The scheduler reports its
own terminal DAG-run verdict, including `dagrun_timeout` and deadlocked runs,
without adding tasks to the DAG or consuming worker slots.

### Changed

- `enable_telomere_tracking(dag)` is now an opt-in shim that adds the
  `telomere` DAG tag. DAGs may add that tag directly instead.
- Execution lifecycle names are fixed at `<dag_id>.dag`; schedule lifecycles
  are fixed at `<dag_id>.schedule`; the listener uses `telomere_default`.
- Run timeouts use `dagrun_timeout`, then the data-interval length, then one
  hour. Schedule deadlines prefer Airflow's `next_dagrun_create_after`.
- Terminal events correlate to running Telomere runs by the Airflow `run_id`
  tag, so scheduler and API-server events survive process restarts.

### Removed

- `enable_telomere_tracking` options `lifecycle_name`, `track_schedule`,
  `timeout_seconds`, `tags`, `telomere_conn_id`, and
  `fail_on_telomere_error`. Passing them now raises `TypeError`.
- `TelomereDAGStartOperator`, `TelomereCanaryOperator`, and
  `TelomereFinalizeOperator`. The task-level `TelomereLifecycleOperator` is
  unchanged.

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
