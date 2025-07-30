FROM apache/airflow:2.8.0-python3.11

# Copy the package source as airflow user
COPY --chown=airflow:root . /opt/telomere-provider/

# Install the telomere provider in development mode as airflow user
USER airflow
RUN pip install --user -e /opt/telomere-provider/