"""
DAG que roda o pipeline completo do SP156: permissões -> menu -> varredura
de IDs extras -> extração de conteúdo -> publicação no BookStack -> backup
mensal. Cada etapa é uma task separada, encadeada em sequência (uma runa
só, sem paralelismo entre elas).

Por que a varredura de IDs extras (etapa 3) continua existindo:
o controle de hash (etapa 5) só compara conteúdo de páginas que o pipeline
já conhece. Ele não descobre páginas que nunca apareceram no menu (link
"órfão", sem entrada na navegação). A única forma de achar essas páginas é
testar a faixa de IDs diretamente - não dá pra substituir isso por
comparação de datas.
"""
from datetime import datetime, timedelta

from airflow.sdk import dag, task, get_current_context, Variable
from airflow.providers.standard.operators.bash import BashOperator

from src.coleta import pegar_menu, varrer_ids, completar_dados
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
        "Coleta o menu e a faixa de IDs 700-7000 do SP156, extrai o "
        "conteúdo completo e publica tudo no BookStack (serviços e "
        "informativos juntos, rotulados por tipo)."
    ),
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["sp156", "bookstack", "coleta"],
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    params={"forcar_varredura_completa": False},
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

        # Guarda contra coleta quebrada: se o site mudou de estrutura (os
        # seletores de HTML pararam de bater) ou a coleta parou pela
        # metade, o número de itens despenca - melhor falhar a DAG aqui
        # do que publicar um BookStack incompleto.
        minimo_esperado = _var_int("sp156_menu_minimo_esperado", 0)
        if minimo_esperado and quantidade < minimo_esperado:
            raise ValueError(
                f"Coleta do menu trouxe {quantidade} itens, abaixo do minimo "
                f"esperado ({minimo_esperado}). Verifique os logs desta task "
                f"antes de prosseguir."
            )
        return quantidade

    @task
    def buscar_ids_extras():
        forcar_varredura_completa = bool(get_current_context()["params"]["forcar_varredura_completa"])
        quantidade = varrer_ids(
            menu_arq=ARQ_MENU,
            saida=ARQ_IDS_EXTRAS,
            id_inicio=_var_int("sp156_id_inicio", 700),
            id_fim=_var_int("sp156_id_fim", 7000),
            workers=_var_int("sp156_varredura_workers", 4),
            tamanho_mini_lote=100,
            forcar_varredura_completa=forcar_varredura_completa,
        )
        print(f"Modo de varredura: {'completa' if forcar_varredura_completa else 'retomada por checkpoint'}")
        print(f"Total de extras encontrados nesta execução: {quantidade}")
        return quantidade

    @task
    def extrair_dados_completos():
        quantidade = completar_dados(
            menu_arq=ARQ_MENU,
            extras_arq=ARQ_IDS_EXTRAS,
            saida=ARQ_DADOS_COMPLETOS,
            limite=None,
            checkpoint_a_cada=100,
            workers=_var_int("sp156_completar_dados_workers", 4),
        )
        print(f"Total de registros completos: {quantidade}")
        return quantidade

    @task
    def publicar_no_bookstack_task():
        return publicar_no_bookstack(arquivo=ARQ_DADOS_COMPLETOS, apenas_um=False)

    @task
    def backup_bookstack_task():
        # Roda por último, depois da publicação: assim o backup já sai com o
        # conteúdo mais recente. Falhar aqui não deve mascarar uma publicação
        # que já deu certo, então esta task fica isolada das anteriores.
        #
        # Backup é mensal (não a cada execução da DAG): guardamos o
        # "YYYY-MM" do último backup numa Airflow Variable e só rodamos de
        # novo se o mês mudou. Simples e sobrevive a DAG rodando mais de
        # uma vez no mesmo mês.
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
    extras = buscar_ids_extras()
    completos = extrair_dados_completos()
    publicar = publicar_no_bookstack_task()
    backup = backup_bookstack_task()

    ajustar_permissoes >> tabela >> menu >> extras >> completos >> publicar >> backup


fluxo_atualizar_servicos()