# Telomere Apache Airflow Provider

Comprehensive Apache Airflow provider for [Telomere](https://telomere.modulecollective.com) lifecycle tracking. Monitor DAG execution, track task timeouts, and ensure scheduled jobs run on time. Check out our [blog post](https://www.modulecollective.com/posts/telomere-airflow-provider/) for some more details about why we created this.

## What is Telomere?

[Telomere](https://telomere.modulecollective.com) is a modern lifecycle management platform that ensures your critical processes complete on time. With powerful alerting via webhooks, email, and integrations, Telomere helps you:

- **Monitor Scheduled Jobs**: Know immediately when jobs fail to start or complete
- **Track Process Health**: Set expectations and get alerted when they're not met
- **Reduce MTTR**: Faster incident detection means faster resolution
- **Simple Integration**: Just HTTP API calls - works with any stack

Learn more and get started at [telomere.modulecollective.com](https://telomere.modulecollective.com).

## The tracking guarantee

The provider is built around one invariant:

> **Telomere never reports "completed" for a DAG run whose Airflow final state
> is failed. Any condition that prevents explicit reporting degrades to a
> Telomere timeout — an alert — never to a false success or to silence.**

Two layers deliver it:

1. **Explicit reporting (fast).** Tasks injected into your DAG report
   completed/failed the moment the run reaches its final state, mirroring
   Airflow's own verdict — including mid-graph failures, branch skips,
   dynamic-mapped tasks, and runs recovered by `all_done` fallback leaves.
2. **The timeout net (guaranteed).** Every Telomere run is created *with a
   timeout* at start. If explicit reporting never happens — killed workers,
   `dagrun_timeout`, a Telomere outage mid-run — the run times out server-side
   and alerts. For scheduled DAGs, a separate `.schedule` lifecycle acts as a
   deadman switch for runs that never start at all.

Known edges, documented honestly:

- A **manually triggered** run of a DAG with no schedule lifecycle, where the
  Telomere API is unreachable at run start, leaves no record to alert on. A
  push-based tracker cannot alert on a run it never heard about. Scheduled
  DAGs don't have this gap — the previous `.schedule` deadline still fires.
- If you **clear and re-run** the start task of an already-tracked run, the
  original Telomere run is orphaned and will raise a timeout alert (noise,
  not a miss); the re-run is tracked normally.

## Features

- 📊 **DAG-Level Tracking**: Monitor entire DAG execution lifecycles
- ⏱️ **Task-Level Tracking**: Track individual task execution with timeouts
- 📅 **Schedule Monitoring**: Ensure scheduled DAGs run on time using dual lifecycle approach
- 🔧 **Zero-Code Integration**: Enable tracking without modifying existing DAGs
- 🎯 **Flexible Configuration**: Dynamic lifecycle names, tags, and timeouts
- 🚨 **Intelligent Alerts**: Leverage Telomere's webhook and email notifications

## Installation

```bash
pip install telomere-airflow-provider
```

Requires Apache Airflow >= 3.0 and Python >= 3.10. (Airflow 2 users: pin
`telomere-airflow-provider==0.0.1`, which supports Airflow 2.5–2.x — note that
the Airflow 2 series is EOL.)

## Quick Start

### 1. Configure Telomere Connection

Add a Telomere connection in Airflow:

**Via Airflow UI:**
1. Go to Admin → Connections
2. Add a new connection:
   - Connection Id: `telomere_default`
   - Connection Type: `Telomere`
   - Password: Your Telomere API key

**Via CLI:**
```bash
airflow connections add telomere_default \
  --conn-type telomere \
  --conn-password YOUR_API_KEY
```

**Via Environment Variable:**
```bash
export AIRFLOW_CONN_TELOMERE_DEFAULT='telomere://:YOUR_API_KEY@'
```

### 2. Track Your DAGs

#### Option A: Zero-Code DAG Tracking

Enable Telomere tracking on existing DAGs without code changes:

```python
from telomere_provider.utils import enable_telomere_tracking

# Your existing DAG
dag = DAG("my_existing_dag", ...)

# ... existing tasks ...

# Enable tracking with one line (after all tasks are added)
enable_telomere_tracking(dag)
```

This injects three tasks around your graph:

```
telomere_dag_start >> [your roots]; [your leaves] >> telomere_canary >> telomere_finalize
```

- `telomere_dag_start` creates the Telomere run — with its timeout, so the
  timeout net is armed before your first task executes — and manages the
  `.schedule` lifecycle for scheduled DAGs.
- `telomere_canary` executes only if the dag run is succeeding (its
  `none_failed` trigger rule over your leaves is exactly Airflow's dag-run
  success predicate).
- `telomere_finalize` always runs and reports completed or failed based on
  the canary. Both downstream tasks are Airflow teardowns, so your own leaves
  keep deciding the dag-run state exactly as before injection.

#### Option B: Task-Level Tracking

Track individual critical tasks:

```python
from telomere_provider.operators.telomere import TelomereLifecycleOperator

critical_task = TelomereLifecycleOperator(
    task_id="process_payment",
    python_callable=process_payment_batch,
    lifecycle_name="payment_processing",
    timeout_seconds=300,  # 5 minutes
    tags={"priority": "high"},
    dag=dag,
)
```

The wrapped task reports success/failure when it finishes, reports failure
immediately when it is externally stopped (SIGTERM, UI mark-failed) via
`on_kill`, and falls back to its run timeout when nothing can report (e.g.
SIGKILL/OOM).

## How It Works

### Dual Lifecycle Approach for Scheduled DAGs

The provider uses two separate lifecycles for comprehensive monitoring:

1. **Execution Lifecycle** (`<dag_id>.dag`) - Tracks individual DAG runs
   - Every run is created with a timeout (defaults to the schedule interval)
   - Reported completed/failed explicitly; times out server-side otherwise

2. **Schedule Lifecycle** (`<dag_id>.schedule`) - Monitors schedule compliance
   - Uses Telomere's respawn pattern: every run start completes the previous
     schedule run and opens a new one whose deadline is the next expected run
   - Alerts if the next run doesn't start on time (5-minute grace period)
   - This is the deadman switch: it fires even if Airflow itself is down

### Example: Hourly DAG

For a DAG scheduled to run every hour:
- **Execution lifecycle** times out if a run takes more than 1 hour
- **Schedule lifecycle** times out if the next run hasn't started 65 minutes
  after the last one

## Advanced Features

### Dynamic Lifecycle Names (task-level operator)

```python
def get_lifecycle_name(**context):
    return f"{context['dag'].dag_id}_{context['ds']}"

task = TelomereLifecycleOperator(
    lifecycle_name=get_lifecycle_name,
    ...
)
```

### Custom Timeout Calculation

```python
def calculate_timeout(**context):
    # Base timeout on historical run times
    avg_duration = get_average_duration(context['task'].task_id)
    return int(avg_duration * 1.5)  # 50% buffer

task = TelomereLifecycleOperator(
    timeout_seconds=calculate_timeout,
    ...
)
```

### Namespace Organization

```python
# Telomere automatically namespaces lifecycles by DAG ID
# lifecycle_name="validate" becomes "my_dag.validate"

validation = TelomereLifecycleOperator(
    task_id="validate",
    lifecycle_name="validate",  # Becomes: my_dag.validate
    python_callable=validate_data,
    dag=dag
)
```

## Configuration

### Connection Extra Parameters

```json
{
  "timeout": 30,
  "max_retries": 3,
  "backoff_factor": 0.3
}
```

Note: retries apply to read requests only. Writes (starting/ending runs) are
never replayed automatically — a replayed `start_run` would double-start runs.

### Error Handling

By default, Telomere failures don't fail your tasks — a monitoring outage
degrades to the timeout net instead of blocking your pipeline. To change this:

```python
# Fail task if Telomere is unavailable
task = TelomereLifecycleOperator(
    fail_on_telomere_error=True,
    ...
)
```

## API Reference

### Hooks

- `TelomereHook`: Low-level API client for Telomere

### Operators

- `TelomereLifecycleOperator`: Track task execution
- `TelomereDAGStartOperator`: Start DAG tracking (injected by `enable_telomere_tracking`)
- `TelomereCanaryOperator` / `TelomereFinalizeOperator`: Verdict + reporting
  tasks injected by `enable_telomere_tracking`

Upgrading from 0.0.1: `TelomereDAGEndOperator` and `TelomereDAGFailOperator`
are gone — their leaf-based trigger rules missed mid-graph failures. Use
`enable_telomere_tracking(dag)`, which handles every terminal shape.

### Utilities

- `enable_telomere_tracking()`: Enable DAG-level tracking with one line

## Development

### Tests

Three tiers, all run by CI (`.github/workflows/ci.yml`):

```bash
uv sync                  # install with the dev group
uv run pytest            # tiers 1+2: unit + failure-mode matrix
```

The failure-mode matrix (`tests/matrix/`) is the heart of the suite: every
way a DAG run can end — mid-graph failure, branch skips, all-skipped,
recovery leaves, retries, mapped tasks, API outages at either end — runs
through Airflow's real trigger-rule engine via `dag.test()` and asserts the
exact Telomere calls. If you discover a new way for a run to end, add a row.

```bash
TELOMERE_API_KEY=... tests/e2e/run.sh   # tier 3: compose stack + real API
```

### Quick Start with Docker

```bash
# Clone the repository
git clone https://github.com/modulecollective/telomere-airflow-provider.git
cd telomere-airflow-provider

# Set up environment
cp .env.example .env
# Edit .env and add your TELOMERE_API_KEY

# Start Airflow (api-server, scheduler, dag-processor, postgres)
docker compose up

# Access Airflow at http://localhost:8080
# Username: airflow, Password: airflow
```

See [docker/README.md](docker/README.md) for detailed Docker instructions.

## Requirements

- Apache Airflow >= 3.0, < 4
- Python >= 3.10
- Telomere API key (get one at [telomere.modulecollective.com](https://telomere.modulecollective.com))

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Support

- 📧 Email: hello@modulecollective.com
- 🐛 Issues: [GitHub Issues](https://github.com/modulecollective/telomere-airflow-provider/issues)
- 📖 Telomere Documentation: [telomere.modulecollective.com](https://telomere.modulecollective.com/docs)
