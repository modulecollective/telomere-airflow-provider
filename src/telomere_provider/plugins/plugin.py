"""Register the Telomere DAG-run listener with Airflow."""

from airflow.plugins_manager import AirflowPlugin

from telomere_provider.plugins import listener


class TelomerePlugin(AirflowPlugin):
    """Airflow plugin exposing Telomere's listener hooks."""

    name = "telomere"
    listeners = [listener]
