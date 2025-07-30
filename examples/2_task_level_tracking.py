"""
Example 2: Task-level tracking for specific critical tasks.

This example shows how to add Telomere monitoring to specific tasks
that are critical to your business. Other tasks remain unchanged.
Perfect for when you need granular monitoring of individual operations.
"""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from telomere_provider.operators.telomere import TelomereLifecycleOperator

# Standard DAG setup
default_args = {
    "owner": "data-team",
    "depends_on_past": False,
    "start_date": datetime(2024, 1, 1),
    "email_on_failure": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

dag = DAG(
    "task_level_tracking",
    default_args=default_args,
    description="Process daily payment transactions",
    schedule_interval="0 */4 * * *",  # Every 4 hours
    catchup=False,
    tags=["payments", "critical"],
)

# Helper tasks (not tracked - they're not critical)
setup = BashOperator(
    task_id="setup_environment",
    bash_command="echo 'Setting up processing environment...'",
    dag=dag,
)

validate_input = PythonOperator(
    task_id="validate_input_files",
    python_callable=lambda: print("Validating input files..."),
    dag=dag,
)

# Critical task 1: Payment processing (must complete in 5 minutes)
def process_payments(**kwargs):
    """Process payment transactions - critical business logic."""
    print(f"Processing payments for {kwargs['ds']}")
    # Your payment processing logic here
    # This is where you'd call your payment gateway, update database, etc.
    return {"processed": 5420, "failed": 3}

payment_processing = TelomereLifecycleOperator(
    task_id="process_payments",
    python_callable=process_payments,
    lifecycle_name="payment_processing",  # Telomere lifecycle name
    timeout_seconds=1800,  # 30 minutes - alert if it takes longer
    tags={
        "type": "payment",
        "critical": "true",
        "sla": "30min",
    },
    dag=dag,
)

# Critical task 2: Reconciliation (tracked)
def reconcile_transactions(**kwargs):
    """Reconcile transactions with bank - must complete."""
    ti = kwargs["ti"]
    result = ti.xcom_pull(task_ids="process_payments", key="return_value")
    print(f"Reconciling {result['processed']} transactions...")
    # Your reconciliation logic here

reconciliation = TelomereLifecycleOperator(
    task_id="reconcile_transactions",
    python_callable=reconcile_transactions,
    lifecycle_name="payment_reconciliation",
    timeout_seconds=900,  # 15 minutes
    tags={
        "type": "reconciliation",
        "critical": "true",
    },
    fail_on_telomere_error=True,  # Fail if we can't track this critical task
    dag=dag,
)

# Non-critical tasks (not tracked)
generate_report = PythonOperator(
    task_id="generate_report",
    python_callable=lambda: print("Generating summary report..."),
    dag=dag,
)

cleanup = BashOperator(
    task_id="cleanup",
    bash_command="echo 'Cleaning up temporary files...'",
    dag=dag,
)

# Task dependencies
setup >> validate_input >> payment_processing >> reconciliation
reconciliation >> [generate_report, cleanup]

# Benefits of this approach:
# - Only critical tasks are monitored (payment processing & reconciliation)
# - Each task has its own lifecycle in Telomere:
#   - "task_level_tracking.payment_processing" (30 min timeout)
#   - "task_level_tracking.payment_reconciliation" (15 min timeout)
# - Get alerts via Telomere if critical tasks fail or exceed timeout
# - Non-critical tasks (setup, cleanup) don't clutter your monitoring
# - fail_on_telomere_error=True ensures reconciliation fails if monitoring fails