# Telomere Apache Airflow Provider

Comprehensive Apache Airflow provider for [Telomere](https://telomere.modulecollective.com) lifecycle tracking. Monitor DAG execution, track task timeouts, and ensure scheduled jobs run on time.

## What is Telomere?

[Telomere](https://telomere.modulecollective.com) is a modern lifecycle management platform that ensures your critical processes complete on time. With powerful alerting via webhooks, email, and integrations, Telomere helps you:

- **Monitor Scheduled Jobs**: Know immediately when jobs fail to start or complete
- **Track Process Health**: Set expectations and get alerted when they're not met
- **Reduce MTTR**: Faster incident detection means faster resolution
- **Simple Integration**: Just HTTP API calls - works with any stack

Learn more and get started at [telomere.modulecollective.com](https://telomere.modulecollective.com).

## Features

- 📊 **DAG-Level Tracking**: Monitor entire DAG execution lifecycles
- ⏱️ **Task-Level Tracking**: Track individual task execution with timeouts
- 📅 **Schedule Monitoring**: Ensure scheduled DAGs run on time using dual lifecycle approach
- 🔧 **Zero-Code Integration**: Enable tracking without modifying existing DAGs
- 🎯 **Flexible Configuration**: Dynamic lifecycle names, tags, and timeouts
- 🚨 **Intelligent Alerts**: Leverage Telomere's webhook and email notifications
- 🔄 **Automatic Retries**: Built-in retry logic with exponential backoff

## Installation

```bash
pip install telomere-airflow-provider
```

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
export AIRFLOW_CONN_TELOMERE_DEFAULT='telomere://YOUR_API_KEY@'
```

### 2. Track Your DAGs

#### Option A: Zero-Code Integration

Enable Telomere tracking on existing DAGs without code changes:

```python
from telomere_provider.utils import enable_telomere_tracking

# Your existing DAG
dag = DAG("my_existing_dag", ...)

# Enable tracking with one line!
enable_telomere_tracking(dag)
```

#### Option B: DAG-Level Operators

Track entire DAG execution:

```python
from telomere_provider.operators.dag import (
    TelomereDAGStartOperator,
    TelomereDAGEndOperator
)

with DAG("my_dag", ...) as dag:
    start = TelomereDAGStartOperator(task_id="telomere_start")

    # Your tasks here
    task1 = PythonOperator(...)
    task2 = BashOperator(...)

    end = TelomereDAGEndOperator(task_id="telomere_end")

    start >> [task1, task2] >> end
```

#### Option C: Task-Level Tracking

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

## How It Works

### Dual Lifecycle Approach for Scheduled DAGs

The provider uses two separate lifecycles for comprehensive monitoring:

1. **Execution Lifecycle** - Tracks individual DAG runs
   - Monitors if each run completes within timeout
   - Timeout defaults to schedule interval

2. **Schedule Lifecycle** - Monitors schedule compliance
   - Uses Telomere's respawn pattern
   - Alerts if next run doesn't start on time
   - Includes 5-minute grace period

### Example: Hourly DAG

For a DAG scheduled to run every hour:
- **Execution lifecycle** times out after 1 hour
- **Schedule lifecycle** times out if next run doesn't start within 65 minutes

This dual approach ensures you're alerted for both execution delays and scheduling issues.

## Examples

### Basic Task Tracking

```python
from telomere_provider.operators.telomere import TelomereLifecycleOperator

# Track a critical task
validate_task = TelomereLifecycleOperator(
    task_id="validate_data",
    python_callable=validate_data_batch,
    lifecycle_name="data_validation",
    timeout_seconds=300,
    dag=dag
)
```

### Automatic DAG Tracking

Add Telomere tracking to existing DAGs:

```python
from telomere_provider.utils import enable_telomere_tracking

# Your existing DAG
dag = DAG("my_dag", ...)

# ... existing tasks ...

# Enable DAG-level lifecycle tracking
enable_telomere_tracking(
    dag,
    track_schedule=True,  # Monitor schedule compliance
    tags={"team": "data-eng"}
)
```

### Monitor Critical Tasks

```python
from telomere_provider.operators.telomere import TelomereLifecycleOperator

# Monitor payment processing with strict timeout
payment_task = TelomereLifecycleOperator(
    task_id="process_payments",
    python_callable=process_payment_batch,
    lifecycle_name="payment_processing",
    timeout_seconds=300,  # Must complete in 5 minutes
    tags={"priority": "critical", "team": "finance"},
    fail_on_telomere_error=True,  # Fail task if monitoring fails
    dag=dag,
)
```

### Conditional Tracking

```python
from airflow.models import Variable

if Variable.get("environment") == "production":
    enable_telomere_tracking(
        dag,
        fail_on_telomere_error=True  # Fail DAG if Telomere unavailable
    )
```

### Environment-Based Configuration

```python
# Only enable in production
if Variable.get("environment", "dev") == "production":
    enable_telomere_tracking(
        dag,
        tags={"env": "production"},
        fail_on_telomere_error=True
    )
```

## Advanced Features

### Dynamic Lifecycle Names

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

### Error Handling

By default, Telomere failures don't fail your tasks. To change this:

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
- `TelomereDAGStartOperator`: Start DAG tracking
- `TelomereDAGEndOperator`: End DAG tracking
- `TelomereDAGFailOperator`: Mark DAG as failed

### Utilities

- `enable_telomere_tracking()`: Enable DAG-level tracking with one line

## Development

### Quick Start with Docker

The easiest way to test the provider is using our Docker development environment:

```bash
# Clone the repository
git clone https://github.com/modulecollective/telomere-airflow-provider.git
cd telomere-airflow-provider

# Set up environment
cp .env.example .env
# Edit .env and add your TELOMERE_API_KEY

# Start Airflow
docker compose up

# Access Airflow at http://localhost:8080
# Username: airflow, Password: airflow
```

The Docker environment includes:
- Apache Airflow with LocalExecutor
- PostgreSQL database
- All example DAGs pre-loaded
- Live code reloading

See [docker/README.md](docker/README.md) for detailed Docker instructions.

### Manual Development

1. Install in development mode:
   ```bash
   pip install -e .
   ```

2. Configure Telomere connection in Airflow

3. Copy example DAGs to your Airflow DAGs folder

## Requirements

- Apache Airflow >= 2.5.0, < 3.0.0
- Python >= 3.8
- Telomere API key (get one at [telomere.modulecollective.com](https://telomere.modulecollective.com))

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Support

- 📧 Email: hello@modulecollective.com
- 🐛 Issues: [GitHub Issues](https://github.com/modulecollective/telomere-airflow-provider/issues)
- 📖 Telomere Documentation: [telomere.modulecollective.com](https://telomere.modulecollective.com)
- 💬 Get Help: Contact us through the Telomere platform