# SP156 → BookStack

Coleta os serviços/informativos do portal SP156 (SMSUB/SELIMP) e publica
tudo automaticamente num BookStack, via Airflow.

## Como rodar

1. Clone o repositório.
2. Copie `.env.example` para `.env` e preencha os valores (veja a seção
   [Gerando os segredos do `.env`](#gerando-os-segredos-do-env) abaixo).

3. Suba tudo com o script de setup — ele ajusta a permissão das pastas
   compartilhadas com o container antes de subir, evitando que o
   `dag-processor` falhe por não conseguir escrever em `airflow/logs/`:

   ```
   1º ajuste o UID do .env -> id -u o valor que aparecer coloque no AIRFLOW_UID=
   2ºconfira como esta a permissão da pasta 
   ls -la setup.sh -> se aparecer: -rw-r--r-- 1 usuario usuario 826 ... setup.sh -> corrija: chmod +x setup.sh -> deve ficar assim -rwxr-xr-x 1 usuario usuario 826 ... setup.sh
   3º bash ./setup.sh
   ```
   (equivalente a rodar `docker compose up -d --build` na mão, só que com
   as pastas já preparadas antes)

4. Acesse:
   - **Airflow**: http://localhost:8080 — usuário `admin`, senha gerada
     automaticamente a cada subida do container. Pra ver a senha:
     ```bash
     docker exec airflow-webserver_sp156 cat /opt/airflow/simple_auth_manager_passwords.json.generated
     ```
   - **BookStack**: http://localhost — login padrão na primeira vez:
     `admin@admin.com` / `password`. **Troque essa senha assim que
     entrar** (Configurações → Usuários).

5. No Airflow, ative e dispare a DAG `atualizar_servicos_sp156`.

6. Gere o token de API do BookStack: menu do usuário → **Meu perfil** →
   **Tokens de API** → criar token novo, e cole `BOOKSTACK_TOKEN_ID` /
   `BOOKSTACK_TOKEN_SECRET` no `.env`. Depois de adicionar, suba de novo
   (`docker compose up -d`) pra essas variáveis chegarem ao container.

## Gerando os segredos do `.env`

Nenhum desses vem preenchido no repositório — gere um valor único pra
cada:

| Variável | Como gerar |
|---|---|
| `AIRFLOW_FERNET_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `AIRFLOW_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `AIRFLOW_JWT_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` (gere de novo, não reaproveite o valor acima) |
| `BOOKSTACK_APP_KEY` | `python3 -c "import secrets, base64; print('base64:' + base64.b64encode(secrets.token_bytes(32)).decode())"` |
| `MYSQL_ROOT_PASSWORD` | `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | escolha um usuário (ex: `bookstack`) e gere uma senha do mesmo jeito acima |
| `DB_USERNAME` / `DB_PASSWORD` | **precisam ser idênticos** a `MYSQL_USER` / `MYSQL_PASSWORD` — é a mesma credencial vista pelo BookStack |

## O que a DAG faz

1. Ajusta permissões das pastas compartilhadas (dados/backups/logs
   usados *durante* a execução do pipeline — não confundir com o passo
   de setup do host acima, que resolve a permissão de `airflow/logs/`
   antes mesmo do Airflow subir).
2. Coleta o menu de serviços (Selenium).
3. Varre a faixa de IDs 700–7000 procurando serviços que não aparecem
   no menu (ver justificativa no início da DAG).
4. Extrai o conteúdo completo de cada serviço/informativo, já filtrado
   por órgão (SMSUB/SP156/SELIMP).
5. Publica no BookStack (Estante SP156 → Livro por categoria →
   Capítulo por grupo → Página por serviço), preservando edições
   manuais via controle de hash.
6. Faz backup do banco do BookStack uma vez por mês.

## Resetar o ambiente

```bash
docker compose down -v
./setup.sh
```

## Estrutura

```
setup.sh                 # prepara permissões e sobe o docker compose
airflow/dags/             # DAG única do pipeline
book_cartas_servicos/
  src/                    # código de coleta, publicação, hash e backup
  dados/                  # JSONs gerados em runtime (não versionados)
nginx/conf.d/              # proxy reverso na frente do BookStack + Airflow
backups/                   # dumps mensais do BookStack (não versionados)
docker-compose.yml
Dockerfile
```
