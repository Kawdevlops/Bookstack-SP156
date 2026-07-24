#!/bin/bash

set -e

if [ ! -f .env ]; then
    echo ".env não encontrado - copiando de .env.example..."
    cp .env.example .env
fi

FORCAR_REGERACAO="false"
if [ "$1" = "--regerar-segredos" ]; then
    FORCAR_REGERACAO="true"
    echo "⚠️  Modo --regerar-segredos ativado: valores existentes serão sobrescritos."
fi

preencher_se_vazio() {
    local nome_var="$1"
    local comando_geracao="$2"
    local valor_atual
    valor_atual="$(grep -E "^${nome_var}=" .env 2>/dev/null | cut -d= -f2-)"

    if [ -n "$valor_atual" ] && [ "$FORCAR_REGERACAO" != "true" ]; then
        echo "  $nome_var já preenchido, mantendo."
        return
    fi

    local novo_valor
    novo_valor="$(eval "$comando_geracao")"
    if grep -q "^${nome_var}=" .env; then
        sed -i "s|^${nome_var}=.*|${nome_var}=${novo_valor}|" .env
    else
        echo "${nome_var}=${novo_valor}" >> .env
    fi
    echo "  $nome_var $([ "$FORCAR_REGERACAO" = "true" ] && echo "regerado" || echo "gerado")."
}

echo "Conferindo segredos do .env..."

preencher_se_vazio "AIRFLOW_FERNET_KEY" \
    "python3 -c \"import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
preencher_se_vazio "AIRFLOW_SECRET_KEY" \
    "python3 -c \"import secrets; print(secrets.token_hex(32))\""
preencher_se_vazio "AIRFLOW_JWT_SECRET" \
    "python3 -c \"import secrets; print(secrets.token_hex(32))\""
preencher_se_vazio "BOOKSTACK_APP_KEY" \
    "python3 -c \"import secrets, base64; print('base64:' + base64.b64encode(secrets.token_bytes(32)).decode())\""
preencher_se_vazio "MYSQL_ROOT_PASSWORD" \
    "python3 -c \"import secrets; print(secrets.token_urlsafe(24))\""
preencher_se_vazio "BOOKSTACK_APP_KEY" \
     "python3 -c \"import secrets, base64; print('base64:' + base64.b64encode(secrets.token_bytes(32)).decode())""
     
UID_ATUAL="$(id -u)"

if grep -q '^AIRFLOW_UID=' .env; then
    sed -i "s|^AIRFLOW_UID=.*|AIRFLOW_UID=${UID_ATUAL}|" .env
else
    echo "AIRFLOW_UID=${UID_ATUAL}" >> .env
fi
AIRFLOW_UID="$UID_ATUAL"

echo "Usando AIRFLOW_UID=$AIRFLOW_UID"

mkdir -p airflow/logs airflow/dags airflow/plugins book_cartas_servicos/dados backups nginx/certs nginx/logs

sudo chown -R "$AIRFLOW_UID:0" airflow book_cartas_servicos backups
chmod -R 775 airflow book_cartas_servicos backups

echo "Permissões ajustadas. Subindo os containers..."
docker compose up -d --build