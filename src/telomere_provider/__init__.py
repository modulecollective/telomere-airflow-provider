"""Telomere provider for Apache Airflow."""

__version__ = "1.0.0"


def get_provider_info():
    """Return provider information for Airflow."""
    return {
        "package-name": "telomere-airflow-provider",
        "name": "Telomere",
        "description": "Telomere lifecycle tracking for Apache Airflow",
        "connection-types": [
            {
                "connection-type": "telomere",
                "hook-class-name": "telomere_provider.hooks.telomere.TelomereHook",
            }
        ],
        "versions": [__version__],
    }
