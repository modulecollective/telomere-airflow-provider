FROM apache/airflow:3.3.0

# Copy the package source as airflow user
COPY --chown=airflow:root . /opt/telomere-provider/

# Install the telomere provider in development mode as airflow user
USER airflow
RUN pip install --no-cache-dir -e /opt/telomere-provider/
