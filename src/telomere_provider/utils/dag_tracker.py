"""Opt a DAG into listener-based Telomere tracking."""

from __future__ import annotations

from airflow.sdk import DAG

TELOMERE_DAG_TAG = "telomere"


def enable_telomere_tracking(dag: DAG) -> None:
    """Enable DAG-run tracking by adding the Telomere opt-in tag."""
    dag.tags.add(TELOMERE_DAG_TAG)
