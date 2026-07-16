import os
import re
import hashlib
import psycopg2
import psycopg2.extras

def normalizar_para_hash(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()

def calcular_hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def hash_de_conteudo(texto_bruto: str) -> str:
    return calcular_hash(normalizar_para_hash(texto_bruto))

def _conectar():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )

def _executar(comando: str, parametros: tuple = (), *, buscar_um: bool = False, buscar_todos: bool = False):
    usa_dict = buscar_um or buscar_todos
    with _conectar() as conexao:
        with conexao.cursor(cursor_factory=psycopg2.extras.RealDictCursor if usa_dict else None) as cursor:
            cursor.execute(comando, parametros)
            if buscar_um:
                linha = cursor.fetchone()
                return dict(linha) if linha else None
            if buscar_todos:
                return [dict(linha) for linha in cursor.fetchall()]
    return None

def garantir_tabela() -> None:
    _executar("""
        CREATE TABLE IF NOT EXISTS pagina_hash (
            tipo               TEXT NOT NULL,
            codigo_servico     TEXT NOT NULL,
            bookstack_page_id  INTEGER,
            hash_fonte         TEXT NOT NULL,
            hash_publicado     TEXT NOT NULL,
            em_conflito        BOOLEAN NOT NULL DEFAULT FALSE,
            atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
            verificado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tipo, codigo_servico)
        );
    """)

def obter_hashes_salvos(tipo: str, codigo_servico: str) -> dict | None:
    return _executar(
        """
        SELECT bookstack_page_id, hash_fonte, hash_publicado, em_conflito
        FROM pagina_hash WHERE tipo = %s AND codigo_servico = %s;
        """,
        (tipo, codigo_servico), buscar_um=True,
    )

def salvar_hashes(
    tipo: str, codigo_servico: str, hash_fonte: str, hash_publicado: str,
    bookstack_page_id: int, em_conflito: bool = False,
) -> None:
    _executar(
        """
        INSERT INTO pagina_hash
            (tipo, codigo_servico, bookstack_page_id, hash_fonte,
             hash_publicado, em_conflito, atualizado_em, verificado_em)
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (tipo, codigo_servico) DO UPDATE SET
            bookstack_page_id = EXCLUDED.bookstack_page_id,
            hash_fonte        = EXCLUDED.hash_fonte,
            hash_publicado    = EXCLUDED.hash_publicado,
            em_conflito       = EXCLUDED.em_conflito,
            atualizado_em     = now(),
            verificado_em     = now();
        """,
        (tipo, codigo_servico, bookstack_page_id, hash_fonte, hash_publicado, em_conflito),
    )

def marcar_conflito(tipo: str, codigo_servico: str) -> None:
    _executar(
        "UPDATE pagina_hash SET em_conflito = TRUE, verificado_em = now() "
        "WHERE tipo = %s AND codigo_servico = %s;",
        (tipo, codigo_servico),
    )

def listar_conflitos() -> list[dict]:
    return _executar(
        """
        SELECT tipo, codigo_servico, bookstack_page_id, verificado_em
        FROM pagina_hash WHERE em_conflito = TRUE ORDER BY verificado_em DESC;
        """,
        buscar_todos=True,
    )

ACAO_CRIAR = "CRIAR"
ACAO_ATUALIZAR = "ATUALIZAR"
ACAO_PULAR = "PULAR"
ACAO_CONFLITO = "CONFLITO"

def decidir_acao(
    hash_fonte_novo: str, salvo: dict | None,
    hash_atual_no_bookstack: str | None, pagina_existe: bool = True,
) -> str:
    """
    CRIAR      - nunca vimos essa página antes, OU tínhamos um registro
                 dela mas a página não existe mais no BookStack de
                 verdade (apagada, ou o BookStack foi resetado sem
                 resetar esta tabela de hash) - sem checar isso, o robô
                 "lembra" de uma página fantasma e pula pra sempre,
                 achando que já publicou.
    PULAR      - a fonte não mudou desde a última execução E a página
                 ainda existe de verdade no BookStack.
    CONFLITO   - alguém editou a página no BookStack por fora do robô
    ATUALIZAR  - a fonte mudou e o BookStack ainda está como o robô deixou
    """
    if salvo is None or not pagina_existe:
        return ACAO_CRIAR
    if hash_fonte_novo == salvo["hash_fonte"]:
        return ACAO_PULAR
    if hash_atual_no_bookstack is not None and hash_atual_no_bookstack != salvo["hash_publicado"]:
        return ACAO_CONFLITO
    return ACAO_ATUALIZAR