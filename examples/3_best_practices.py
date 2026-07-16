"""
Example 3: Best practices - Combining DAG and task-level tracking.

This example shows the recommended approach for production pipelines:
- DAG-level tracking for overall pipeline health and schedule monitoring
- Task-level tracking for critical business operations
- Clear separation between what needs monitoring and what doesn't
"""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

from telomere_provider.operators.telomere import TelomereLifecycleOperator
from telomere_provider.utils import enable_telomere_tracking

# Standard DAG setup
default_args = {
    "owner": "data-team",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

dag = DAG(
    "best_practices",
    default_args=default_args,
    description="Production ETL pipeline with comprehensive monitoring",
    schedule="0 1 * * *",  # Daily at 1 AM
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["production", "etl", "critical"],
)


# Setup tasks (not individually tracked)
def check_dependencies():
    """Verify all upstream dependencies are ready."""
    print("Checking upstream data sources...")
    # Verify data is ready
    return True


dependency_check = PythonOperator(
    task_id="check_dependencies",
    python_callable=check_dependencies,
    dag=dag,
)


# Critical ETL operations (individually tracked)
def extract_data(**kwargs):
    """Extract data from multiple sources - critical for downstream."""
    print(f"Extracting data for run {kwargs['run_id']}")
    # Complex extraction logic
    return {"records": 2500000, "sources": 5}


extract = TelomereLifecycleOperator(
    task_id="extract_data",
    python_callable=extract_data,
    lifecycle_name="etl_extraction",
    timeout_seconds=3600,  # 1 hour
    tags={
        "stage": "extract",
        "priority": "critical",
        "data_volume": "high",
    },
    dag=dag,
)


def transform_data(**kwargs):
    """Apply business transformations - must complete for reporting."""
    ti = kwargs["ti"]
    data = ti.xcom_pull(task_ids="extract_data", key="return_value")
    print(f"Transforming {data['records']} records...")
    # Complex transformation logic
    return {"transformed": data["records"] * 0.95}


transform = TelomereLifecycleOperator(
    task_id="transform_data",
    python_callable=transform_data,
    lifecycle_name="etl_transformation",
    timeout_seconds=7200,  # 2 hours
    tags={
        "stage": "transform",
        "priority": "critical",
    },
    dag=dag,
)


# Regular task for loading (not individually tracked - covered by DAG tracking)
def load_to_warehouse(**kwargs):
    """Load to data warehouse."""
    ti = kwargs["ti"]
    data = ti.xcom_pull(task_ids="transform_data", key="return_value")
    print(f"Loading {data['transformed']} records to warehouse...")


load = PythonOperator(
    task_id="load_to_warehouse",
    python_callable=load_to_warehouse,
    dag=dag,
)


# Data quality check (tracked - business critical)
def quality_validation(**kwargs):
    """Validate data quality - blocks downstream if fails."""
    print("Running data quality checks...")
    # Quality validation logic
    # This could raise an exception if quality fails


quality_check = TelomereLifecycleOperator(
    task_id="data_quality_validation",
    python_callable=quality_validation,
    lifecycle_name="data_quality_check",
    timeout_seconds=1800,  # 30 minutes
    tags={
        "type": "validation",
        "blocks_downstream": "true",
    },
    fail_on_telomere_error=True,  # Must be able to track quality checks
    dag=dag,
)

# Post-processing tasks (not individually tracked)
update_metadata = BashOperator(
    task_id="update_metadata",
    bash_command="echo 'Updating pipeline metadata...'",
    dag=dag,
)

send_notifications = PythonOperator(
    task_id="send_notifications",
    python_callable=lambda: print("Sending completion notifications..."),
    dag=dag,
)

# Task dependencies
dependency_check >> extract >> transform >> load >> quality_check
quality_check >> [update_metadata, send_notifications]

# Enable DAG-level tracking for overall pipeline monitoring
enable_telomere_tracking(dag)

# What this gives you:
# 1. DAG-level monitoring in Telomere:
#    - "best_practices.dag" - tracks each pipeline run;
#      failures anywhere in the graph are reported the moment the run
#      finishes, and interrupted reporting still alerts via the run timeout
#    - "best_practices.schedule" - monitors schedule
#      compliance; alerts if the next run doesn't start on time
#
# 2. Task-level lifecycles for critical operations:
#    - "best_practices.etl_extraction" - Alert if takes >1 hour or fails
#    - "best_practices.etl_transformation" - Alert if takes >2 hours or fails
#    - "best_practices.data_quality_check" - Alert if validation fails
#
# 3. Configure alerts in Telomere:
#    - Webhooks to PagerDuty/Slack for critical failures
#    - Email summaries for pipeline health
#    - Integration with your incident management
