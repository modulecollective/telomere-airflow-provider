"""Helpers for building Airflow UI URLs to attach to Telomere runs."""

from __future__ import annotations


def build_airflow_url(dag_id: str, run_id: str, task_id: str | None = None) -> str | None:
    """
    Build a link into the Airflow UI for a dag run (or a task instance).

    Returns None when no base URL is configured ([api] base_url has no
    default in Airflow 3 unless set explicitly).
    """
    try:
        from airflow.configuration import conf

        base_url = conf.get("api", "base_url", fallback=None)
    except Exception:
        return None
    if not base_url:
        return None

    url = f"{base_url.rstrip('/')}/dags/{dag_id}/grid?dag_run_id={run_id}"
    if task_id:
        url += f"&task_id={task_id}"
    return url
