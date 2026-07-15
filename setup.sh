#!/bin/bash
# Prepara as pastas compartilhadas com o container ANTES de subir o Airflow.
#
# Sem isso, o dag-processor pode falhar ao tentar escrever seu próprio log
# em airflow/logs/ (a pasta pertence a outro dono no host) e a DAG nunca
# chega a aparecer na interface.
set -e

AIRFLOW_UID="$(grep -E '^AIRFLOW_UID=' .env 2>/dev/null | cut -d= -f2)"
AIRFLOW_UID="${AIRFLOW_UID:-$(id -u)}"

echo "Usando AIRFLOW_UID=$AIRFLOW_UID"

mkdir -p airflow/logs airflow/dags airflow/plugins book_cartas_servicos/dados backups nginx/certs nginx/logs

sudo chown -R "$AIRFLOW_UID:0" airflow/logs airflow/dags airflow/plugins book_cartas_servicos/dados backups
chmod -R 775 airflow/logs airflow/dags airflow/plugins book_cartas_servicos/dados backups

echo "Permissões ajustadas. Subindo os containers..."
docker compose up -d --build
