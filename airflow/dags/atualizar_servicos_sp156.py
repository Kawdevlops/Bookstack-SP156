from datetime import datetime, timedelta
from airflow.sdk import dag, task, Variable
from airflow.providers.standard.operators.bash import BashOperator
from src.coleta import pegar_menu, completar_dados
from src.bookstack_publicacao import publicar_no_bookstack
from src.hash_bookstack import garantir_tabela
from src.backup_bookstack import fazer_backup

DEFAULT_ARGS = {"retries": 2, "retry_delay": timedelta(minutes=5)}

PASTA_DADOS = "/opt/airflow/dados"
ARQ_MENU = f"{PASTA_DADOS}/menu_links.json"
ARQ_IDS_EXTRAS = f"{PASTA_DADOS}/ids_encontrados.json"
ARQ_DADOS_COMPLETOS = f"{PASTA_DADOS}/dados_completos.json"


def _var_int(nome: str, padrao: int) -> int:
    """Atalho pra ler uma Airflow Variable como inteiro, com valor padrão."""
    return int(Variable.get(nome, default=padrao))


@dag(
    dag_id="atualizar_servicos_sp156",
    description=(
        "Coleta o menu do SP156, filtra pelo órgão (SMSUB) e publica "
        "tudo no BookStack (serviços e informativos juntos, rotulados "
        "por tipo)."
    ),
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["sp156", "bookstack", "coleta"],
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
)
def fluxo_atualizar_servicos():

    ajustar_permissoes = BashOperator(
        task_id="ajustar_permissoes",
        bash_command=(
            "chmod -R u+rwX,g+rwX "
            f"{PASTA_DADOS} /opt/airflow/backups /opt/airflow/logs || true"
        ),
    )

    @task
    def preparar_tabela_hash():
        garantir_tabela()

    @task
    def coletar_menu():
        quantidade = pegar_menu(saida=ARQ_MENU)
        print(f"Total de registros coletados no menu: {quantidade}")
        minimo_esperado = _var_int("sp156_menu_minimo_esperado", 0)
        if minimo_esperado and quantidade < minimo_esperado:
            raise ValueError(
                f"Coleta do menu trouxe {quantidade} itens, abaixo do minimo "
                f"esperado ({minimo_esperado}). Verifique os logs desta task "
                f"antes de prosseguir."
            )
        return quantidade

    @task
    def extrair_dados_completos():
        quantidade = completar_dados(
            menu_arq=ARQ_MENU,
            extras_arq=ARQ_IDS_EXTRAS,
            saida=ARQ_DADOS_COMPLETOS,
            limite=None,
            checkpoint_a_cada=100,
        )
        print(f"Total de registros completos: {quantidade}")
        return quantidade

    @task
    def publicar_no_bookstack_task():
        return publicar_no_bookstack(arquivo=ARQ_DADOS_COMPLETOS, apenas_um=False)

    @task
    def backup_bookstack_task():
        mes_atual = datetime.now().strftime("%Y-%m")
        ultimo_backup = Variable.get("sp156_backup_ultimo_mes", default=None)
        if ultimo_backup == mes_atual:
            print(f"Backup deste mês ({mes_atual}) já foi feito. Pulando.")
            return None

        caminho = fazer_backup()
        Variable.set("sp156_backup_ultimo_mes", mes_atual)
        print(f"Backup do BookStack salvo em: {caminho}")
        return caminho

    tabela = preparar_tabela_hash()
    menu = coletar_menu()
    completos = extrair_dados_completos()
    publicar = publicar_no_bookstack_task()
    backup = backup_bookstack_task()

    ajustar_permissoes >> tabela >> menu >> completos >> publicar >> backup


fluxo_atualizar_servicos()