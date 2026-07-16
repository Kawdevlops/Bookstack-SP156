import os
import gzip
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

NOME_BANCO = "bookstack_sp156"
HOST_MARIADB = os.environ.get("MARIADB_HOST", "mariadb-bookstack")
PASTA_BACKUPS_PADRAO = "/opt/airflow/backups"
MANTER_ULTIMOS_PADRAO = 14  # ~2 semanas de backups diários

def _rodar_mariadb_dump(destino_sql: Path) -> None:
    comando = [
        "mariadb-dump",
        f"--host={HOST_MARIADB}",
        "--user=root",
        f"--password={os.environ['MYSQL_ROOT_PASSWORD']}",
        "--single-transaction",
        "--quick",
        "--routines",
        NOME_BANCO,
    ]
    with open(destino_sql, "wb") as arquivo_saida:
        resultado = subprocess.run(comando, stdout=arquivo_saida, stderr=subprocess.PIPE)

    if resultado.returncode != 0:
        destino_sql.unlink(missing_ok=True)
        raise RuntimeError(f"mariadb-dump falhou: {resultado.stderr.decode(errors='replace')}")

def _comprimir_e_apagar_original(caminho_sql: Path) -> Path:
    caminho_gz = caminho_sql.with_suffix(caminho_sql.suffix + ".gz")
    with open(caminho_sql, "rb") as origem, gzip.open(caminho_gz, "wb") as destino:
        shutil.copyfileobj(origem, destino)
    caminho_sql.unlink()
    return caminho_gz

def _apagar_backups_antigos(pasta: Path, manter_ultimos: int) -> int:
    backups = sorted(pasta.glob("bookstack_*.sql.gz"), key=lambda p: p.name, reverse=True)
    antigos = backups[manter_ultimos:]
    for arquivo in antigos:
        arquivo.unlink()
    return len(antigos)

def fazer_backup(pasta_destino: str = PASTA_BACKUPS_PADRAO, manter_ultimos: int = MANTER_ULTIMOS_PADRAO) -> str:
    pasta = Path(pasta_destino)
    pasta.mkdir(parents=True, exist_ok=True)

    agora = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho_sql = pasta / f"bookstack_{agora}.sql"

    _rodar_mariadb_dump(caminho_sql)
    caminho_gz = _comprimir_e_apagar_original(caminho_sql)
    apagados = _apagar_backups_antigos(pasta, manter_ultimos)

    tamanho_mb = caminho_gz.stat().st_size / (1024 * 1024)
    print(f"Backup salvo: {caminho_gz} ({tamanho_mb:.1f} MB). "
          f"Backups antigos removidos: {apagados} (mantendo os {manter_ultimos} mais recentes).")
    return str(caminho_gz)