# Docker Development Environment

This Docker Compose setup provides a complete Apache Airflow environment for testing the telomere-airflow-provider.

## Quick Start

```bash
# Set up environment
cp .env.example .env
# Edit .env and add your TELOMERE_API_KEY

# Start Airflow
docker compose up

# Access UI at http://localhost:8080
# Username: airflow, Password: airflow
```

All example DAGs from the `examples/` directory are automatically loaded and ready to run.

## What's Included

- Apache Airflow 2.8.0 with LocalExecutor
- PostgreSQL database for Airflow metadata
- Automatic installation of telomere-airflow-provider from local source
- All example DAGs pre-loaded
- Telomere connection configured via environment variable

## Development Workflow

1. **Make changes to the provider code** in `src/`
2. **Restart the containers** to pick up changes:
   ```bash
   docker compose restart airflow-webserver airflow-scheduler
   ```
3. **Test your changes** using the example DAGs

## Monitoring

- **Airflow UI**: http://localhost:8080
- **DAG Runs**: View run history and logs in the UI
- **Telomere Dashboard**: Check https://telomere.modulecollective.com to see lifecycles created by the DAGs

## Troubleshooting

### Permission Issues (Linux)
If you see permission errors, set your user ID:
```bash
echo "AIRFLOW_UID=$(id -u)" >> .env
```

### Container Won't Start
Check logs:
```bash
docker compose logs airflow-init
docker compose logs airflow-webserver
```

### Clean Start
Remove all data and start fresh:
```bash
docker compose down -v
docker compose up
```

## Available DAGs

1. **simple_dag_tracking** - Simplest integration: DAG-level tracking with one line of code
2. **task_level_tracking** - Track specific critical tasks while leaving others unmonitored
3. **best_practices** - Production-ready example combining DAG and task-level tracking

## Tips

- The provider is installed in development mode (`pip install -e`), so code changes are reflected immediately
- Logs for each task can be viewed in the Airflow UI by clicking on the task instance
- The Telomere connection uses the Bearer token from your environment variable
- All DAGs are paused by default - unpause them in the UI to run