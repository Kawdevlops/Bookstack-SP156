FROM apache/airflow:3.2.2

USER root

RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    mariadb-client \
    libpq-dev \
    gcc \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# Instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar APENAS o código fonte (NÃO os dados)
COPY airflow/dags/ /opt/airflow/dags/
COPY airflow/include/ /opt/airflow/include/

# Criar diretórios para dados (montados como volume)
RUN mkdir -p /opt/airflow/dados /opt/airflow/logs /opt/airflow/backups