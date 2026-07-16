"""
Example 1: Simple DAG-level tracking for scheduled DAGs (least invasive).

This example shows how to add Telomere monitoring to an existing DAG
with just one line of code. Perfect for teams wanting to monitor their
DAGs without modifying task code.
"""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

from telomere_provider.utils import enable_telomere_tracking

# Standard DAG setup
default_args = {
    "owner": "data-team",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

# Your existing DAG - no changes needed
dag = DAG(
    "simple_dag_tracking",
    default_args=default_args,
    description="Generate daily customer analytics report",
    schedule="0 2 * * *",  # Daily at 2 AM
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["analytics", "daily"],
)


# Existing tasks - no changes needed
def extract_customer_data():
    """Extract customer data from database."""
    print("Extracting customer data...")
    # Your existing extraction logic
    return {"customer_count": 15000}


def generate_report(**kwargs):
    """Generate analytics report."""
    ti = kwargs["ti"]
    data = ti.xcom_pull(task_ids="extract_data", key="return_value")
    print(f"Generating report for {data['customer_count']} customers...")
    # Your existing report generation logic


# Standard task definitions
extract = PythonOperator(
    task_id="extract_data",
    python_callable=extract_customer_data,
    dag=dag,
)

generate = PythonOperator(
    task_id="generate_report",
    python_callable=generate_report,
    dag=dag,
)

send = BashOperator(
    task_id="send_report",
    bash_command="echo 'Report sent to stakeholders'",
    dag=dag,
)

# Task dependencies
extract >> generate >> send

# ✨ Add Telomere monitoring with one line! ✨
enable_telomere_tracking(
    dag,
    lifecycle_name="customer_report",
    track_schedule=True,  # Monitors if DAG runs on schedule
    tags={"team": "analytics", "priority": "high"},
)

# That's it! Telomere will now:
# 1. Create "simple_dag_tracking.customer_report.dag" lifecycle
#    - Every run is created with a timeout up front, so even a run that
#      dies without a trace (killed workers, dagrun_timeout) raises an alert
#    - The run is reported completed or failed the moment it finishes,
#      mirroring Airflow's own final state — including mid-graph failures
# 2. Create "simple_dag_tracking.customer_report.schedule" lifecycle
#    - Uses respawn pattern to monitor schedule compliance
#    - Alerts if the next run doesn't start on time (+5 min grace)
# 3. Send alerts via webhooks, email, or integrations you configure in Telomere
