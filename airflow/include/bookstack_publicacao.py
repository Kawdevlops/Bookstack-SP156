import os
import json
import html
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from airflow.sdk import Variable

from include.coleta import (
    MARCADOR_LINK_INICIO,
    MARCADOR_LINK_MEIO,
    MARCADOR_LINK_FIM,
    MARCADOR_PARAGRAFO,
)
from include.hash_bookstack import (
    hash_de_conteudo,
    obter_hashes_salvos,
    salvar_hashes,
    marcar_conflito,
    listar_conflitos,
    decidir_acao as _decidir_acao_pelo_hash,
    ACAO_CRIAR,
    ACAO_ATUALIZAR,
    ACAO_PULAR,
    ACAO_CONFLITO,
)

_SESSAO = requests.Session()
_ADAPTADOR = HTTPAdapter(
    max_retries=Retry(
        total=5, backoff_factor=1.5,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT"],
        respect_retry_after_header=True,
    ),
    pool_connections=10, pool_maxsize=10,
)
_SESSAO.mount("http://", _ADAPTADOR)
_SESSAO.mount("https://", _ADAPTADOR)

STATUS_CRIADA = "CRIADA"
STATUS_ATUALIZADA = "ATUALIZADA"
STATUS_PULADA = "PULADA (fonte sem mudanca)"
STATUS_CONFLITO = "CONFLITO (aguardando tag sp156_aprovado/sp156_rejeitado)"
STATUS_APROVADA = "APROVADA (edicao manual mantida)"
STATUS_REJEITADA = "REJEITADA (fonte oficial restaurada)"

TAG_APROVADO = "sp156_aprovado"
TAG_REJEITADO = "sp156_rejeitado"


def _headers() -> dict:
    return {
        "Authorization": f"Token {Variable.get('BOOKSTACK_TOKEN_ID')}:{Variable.get('BOOKSTACK_TOKEN_SECRET')}",
        "Content-Type": "application/json",
    }


def _request(metodo: str, endpoint: str, **kwargs) -> requests.Response:
    url = f"{os.environ['BOOKSTACK_URL']}/api/{endpoint}"
    return _SESSAO.request(metodo, url, headers=_headers(), timeout=20, **kwargs)


def _get(endpoint: str, params: dict | None = None) -> requests.Response:
    return _request("GET", endpoint, params=params or {})


def _post(endpoint: str, body: dict) -> requests.Response:
    return _request("POST", endpoint, json=body)


def _put(endpoint: str, body: dict) -> requests.Response:
    return _request("PUT", endpoint, json=body)


def _buscar_id_por_nome(endpoint: str, nome: str, filtro_extra: dict | None = None) -> int | None:
    resposta = _get(endpoint, params={"filter[name]": nome, "count": 1})
    if resposta.status_code != 200:
        return None
    for item in resposta.json().get("data", []):
        if item.get("name") != nome:
            continue
        if not filtro_extra or all(item.get(k) == v for k, v in filtro_extra.items()):
            return item["id"]
    return None


def _obter_pagina_por_id(pagina_id: int) -> dict | None:
    resposta = _get(f"pages/{pagina_id}")
    return resposta.json() if resposta.status_code == 200 else None


def _pagina_compativel_com_tipo(pagina: dict, tipo: str) -> bool:
    tipos_na_pagina = {t.get("value") for t in (pagina.get("tags") or []) if t.get("name") == "sp156_tipo"}
    return not tipos_na_pagina or tipo in tipos_na_pagina


def _pagina_compativel_com_codigo(pagina: dict, codigo_servico: str) -> bool:
    codigos_na_pagina = {t.get("value") for t in (pagina.get("tags") or []) if t.get("name") == "sp156_codigo_servico"}
    return not codigos_na_pagina or codigo_servico in codigos_na_pagina


def _obter_ou_criar(endpoint: str, nome: str, rotulo: str,
                     filtro_extra: dict | None = None,
                     corpo_extra: dict | None = None, indent: str = "") -> int:
    id_existente = _buscar_id_por_nome(endpoint, nome, filtro_extra)
    if id_existente:
        print(f"{indent}[{rotulo}] '{nome}' ja existe - ID {id_existente}")
        return id_existente

    resposta = _post(endpoint, {"name": nome, **(corpo_extra or {})})
    if resposta.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar {rotulo.lower()} '{nome}': {resposta.text}")

    novo_id = resposta.json()["id"]
    print(f"{indent}[{rotulo}] '{nome}' criado - ID {novo_id}")
    return novo_id


def obter_estante(nome: str) -> int:
    return _obter_ou_criar("shelves", nome, "Estante")


def obter_livro(nome: str, estante_id: int) -> int:
    livro_id = _obter_ou_criar("books", nome, "Livro")
    _vincular_livro_a_estante(estante_id, livro_id)
    return livro_id


def _vincular_livro_a_estante(estante_id: int, livro_id: int) -> None:
    resposta = _get(f"shelves/{estante_id}")
    livros_atuais = [b["id"] for b in resposta.json().get("books", [])] if resposta.status_code == 200 else []
    if livro_id not in livros_atuais:
        _put(f"shelves/{estante_id}", {"books": livros_atuais + [livro_id]})


def obter_capitulo(nome: str, livro_id: int) -> int:
    return _obter_ou_criar(
        "chapters", nome, "Capitulo",
        filtro_extra={"book_id": livro_id}, corpo_extra={"book_id": livro_id}, indent="  ",
    )


def agrupar_por_capitulo(itens: list[dict], categoria: str) -> dict[str, list[dict]]:
    grupos: dict[str, list[dict]] = defaultdict(list)
    for item in itens:
        caminho = item.get("caminho_servico", [None, item.get("nome", "")])
        nome_capitulo = caminho[1].strip() if len(caminho) > 1 and caminho[1] else (item.get("categoria") or categoria)
        grupos[nome_capitulo].append(item)
    return grupos


_PADRAO_LINK_MARCADO = re.compile(
    rf"{MARCADOR_LINK_INICIO}(.*?){MARCADOR_LINK_MEIO}(.*?){MARCADOR_LINK_FIM}"
)


def _linkificar(pedaco: str) -> str:
    resultado = []
    posicao = 0
    for m in _PADRAO_LINK_MARCADO.finditer(pedaco):
        resultado.append(html.escape(pedaco[posicao:m.start()]))
        texto_link = html.escape(m.group(1))
        href = html.escape(m.group(2), quote=True)
        resultado.append(f'<a href="{href}" target="_blank" rel="noopener noreferrer">{texto_link}</a>')
        posicao = m.end()
    resultado.append(html.escape(pedaco[posicao:]))
    return "".join(resultado)


def texto_de_campo_para_html(texto_campo: str) -> str:
    partes = [p.strip() for p in texto_campo.split(MARCADOR_PARAGRAFO)]
    partes = [p for p in partes if p]
    if not partes:
        return ""
    return "".join(f"<p>{_linkificar(p)}</p>" for p in partes)


def preparar_pagina(servico: dict, capitulo: str, rotulo_tipo: str) -> tuple[str, str]:
    nome = (servico.get("nome") or servico.get("categoria") or "Sem nome").strip()
    cor_badge = "#1d7a46" if rotulo_tipo == "Serviço Online" else "#8a6d00"

    cabecalho = (
        f'<p><strong><a href="{servico.get("link", "")}" target="_blank" rel="noopener noreferrer">{nome}</a></strong> '
        f'<span style="background:{cor_badge};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em;">{rotulo_tipo}</span></p>'
        f'<p><small>Capítulo: {capitulo} | Código: {servico.get("codigo_servico", "")} | '
        f'Extraído em: {servico.get("data_extracao", "")}</small></p>'
    )

    secoes = [
        f'<h4>{html.escape(campo)}</h4>{texto_de_campo_para_html(valor)}'
        for campo, valor in (servico.get("informacoes", {}) or {}).items()
        if (valor or "").strip()
    ]
    corpo = "".join(secoes) or "<p><em>Sem detalhes estruturados para este item.</em></p>"
    return nome, cabecalho + corpo


def tem_conteudo_real(servico: dict) -> bool:
    return any((v or "").strip() for v in (servico.get("informacoes", {}) or {}).values())


def localizar_pagina_existente(nome: str, capitulo_id: int, tipo: str, codigo_servico: str):
    salvo = obter_hashes_salvos(tipo, codigo_servico)
    if salvo is not None:
        pagina = _obter_pagina_por_id(salvo["bookstack_page_id"]) if salvo.get("bookstack_page_id") else None
        if pagina is None:
            return None, salvo, None, None
        return pagina["id"], salvo, hash_de_conteudo(pagina.get("html", "")), pagina

    candidato_id = _buscar_id_por_nome("pages", nome, filtro_extra={"chapter_id": capitulo_id})
    if candidato_id is not None:
        pagina = _obter_pagina_por_id(candidato_id)
        if (
            pagina is not None
            and _pagina_compativel_com_tipo(pagina, tipo)
            and _pagina_compativel_com_codigo(pagina, codigo_servico)
        ):
            return candidato_id, None, None, pagina
    return None, None, None, None


def _resolucao_de_conflito(pagina: dict) -> str | None:
    nomes_tags = {(t.get("name") or "").strip().lower() for t in (pagina.get("tags") or [])}
    if TAG_REJEITADO in nomes_tags:
        return "rejeitado"
    if TAG_APROVADO in nomes_tags:
        return "aprovado"
    return None


def _resolver_conflito_marcado(
    resolucao: str, pagina_atual: dict, pagina_id: int, capitulo_id: int, livro_id: int,
    nome: str, html_pagina: str, tipo: str, codigo_servico: str,
) -> str:
    if resolucao == "aprovado":
        hash_publicado_atual = hash_de_conteudo(pagina_atual.get("html", ""))
        hash_fonte_novo = hash_de_conteudo(html_pagina)
        salvar_hashes(tipo, codigo_servico, hash_fonte_novo, hash_publicado_atual, pagina_id, em_conflito=False)
        return STATUS_APROVADA

    pagina_id, html_salvo = criar_atualizar(pagina_id, capitulo_id, livro_id, nome, html_pagina, tipo, codigo_servico)
    hash_fonte_novo = hash_de_conteudo(html_pagina)
    hash_publicado_real = hash_de_conteudo(html_salvo)
    salvar_hashes(tipo, codigo_servico, hash_fonte_novo, hash_publicado_real, pagina_id, em_conflito=False)
    return STATUS_REJEITADA


def decidir_acao(
    html_novo: str, salvo: dict | None,
    hash_atual_no_bookstack: str | None, pagina_existe: bool,
) -> tuple[str, str]:
    hash_fonte_novo = hash_de_conteudo(html_novo)
    return _decidir_acao_pelo_hash(hash_fonte_novo, salvo, hash_atual_no_bookstack, pagina_existe), hash_fonte_novo


def criar_atualizar(pagina_id: int | None, capitulo_id: int, livro_id: int,
                     nome: str, html_pagina: str, tipo: str, codigo_servico: str) -> tuple[int, str]:
    tags = [
        {"name": "sp156_tipo", "value": tipo},
        {"name": "sp156_codigo_servico", "value": codigo_servico},
    ]
    if pagina_id is None:
        resposta = _post("pages", {"chapter_id": capitulo_id, "book_id": livro_id,
                                    "name": nome, "html": html_pagina, "tags": tags})
        if resposta.status_code not in (200, 201):
            raise RuntimeError(f"Erro ao criar pagina '{nome}': {resposta.text}")
        novo_id = resposta.json()["id"]
    else:
        _put(f"pages/{pagina_id}", {"name": nome, "html": html_pagina, "tags": tags})
        novo_id = pagina_id

    pagina_salva = _obter_pagina_por_id(novo_id)
    html_realmente_salvo = pagina_salva.get("html", "") if pagina_salva else html_pagina
    return novo_id, html_realmente_salvo


def publicar_pagina(servico: dict, capitulo_nome: str, capitulo_id: int, livro_id: int) -> str:
    tipo = servico.get("tipo", "servico")
    rotulo_tipo = "Serviço Online" if tipo == "servico" else "Informativo"
    codigo_servico = str(servico.get("codigo_servico", ""))

    nome, html_pagina = preparar_pagina(servico, capitulo_nome, rotulo_tipo)
    pagina_id, salvo, hash_atual, pagina_atual = localizar_pagina_existente(nome, capitulo_id, tipo, codigo_servico)

    if salvo and salvo.get("em_conflito") and pagina_atual is not None:
        resolucao = _resolucao_de_conflito(pagina_atual)
        if resolucao is not None:
            return _resolver_conflito_marcado(
                resolucao, pagina_atual, pagina_id, capitulo_id, livro_id, nome, html_pagina, tipo, codigo_servico,
            )
        return STATUS_CONFLITO

    acao, hash_novo = decidir_acao(html_pagina, salvo, hash_atual, pagina_atual is not None)

    if acao == ACAO_PULAR:
        return STATUS_PULADA
    if acao == ACAO_CONFLITO:
        marcar_conflito(tipo, codigo_servico)
        return STATUS_CONFLITO

    pagina_id, html_salvo = criar_atualizar(pagina_id, capitulo_id, livro_id, nome, html_pagina, tipo, codigo_servico)
    hash_publicado_real = hash_de_conteudo(html_salvo)
    salvar_hashes(tipo, codigo_servico, hash_novo, hash_publicado_real, pagina_id)
    return STATUS_CRIADA if acao == ACAO_CRIAR else STATUS_ATUALIZADA


def _secao_instrucoes() -> str:
    return (
        "<h4>Como resolver um conflito (pra quem edita)</h4>"
        "<p>Quando uma página aparece na lista \"Editado manualmente — aguardando revisão\" abaixo, está as atualizações feitas por automação "
        "> Precisa ir até a página que foi editada manualmente, pegue o código e coloque na barra de pesquisa para facilitar a procura."
        "> Clicar em editar "
        "> Ao lado direita vera um tag, colocar sp156_aprovado` / `sp156_rejeitado e rodar a dag novamente."
        "> Quando coloca essa tag o robô de atulização entenderá que você autorizou ele manter a edição manual e caso não queira a edição manual só colocar a tag sp156_rejeitado </p>"
    )


def _secao_automatica(eventos: list[dict], agora: str) -> str:
    titulo = f"<h4>Atualizado automaticamente (execução de {agora})</h4>"
    if not eventos:
        return titulo + "<p>Nenhuma página foi criada ou atualizada nesta execução.</p>"
    itens = "".join(
        f"<li><strong>{e['nome']}</strong> ({e['rotulo_tipo']}) — {e['acao']} — "
        f"livro: {e['livro']} › capítulo: {e['capitulo']}</li>" for e in eventos
    )
    return f"{titulo}<ul>{itens}</ul>"


def _secao_manual(conflitos: list[dict]) -> str:
    titulo = "<h4>Editado manualmente — aguardando revisão</h4>"
    if not conflitos:
        return titulo + "<p>Nenhuma página em conflito no momento.</p>"
    itens = "".join(
        f"<li><strong>Código {c['codigo_servico']}</strong> "
        f"({'Serviço Online' if c['tipo'] == 'servico' else 'Informativo'}) — editado manualmente.</li>"
        for c in conflitos
    )
    return f"{titulo}<ul>{itens}</ul>"


def publicar_pagina_de_atualizacoes(estante_id: int, eventos_desta_execucao: list[dict]) -> None:
    NOME_LIVRO = "Atualizações"
    conflitos = listar_conflitos()
    livro_id = obter_livro(NOME_LIVRO, estante_id)
    capitulo_id = obter_capitulo("Acompanhamento", livro_id)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")

    html_pagina = _secao_instrucoes() + _secao_automatica(eventos_desta_execucao, agora) + _secao_manual(conflitos)
    nome_pagina = "Histórico de atualizações"
    pagina_id = _buscar_id_por_nome("pages", nome_pagina, filtro_extra={"chapter_id": capitulo_id})
    if pagina_id:
        _put(f"pages/{pagina_id}", {"name": nome_pagina, "html": html_pagina})
    else:
        _post("pages", {"chapter_id": capitulo_id, "book_id": livro_id, "name": nome_pagina, "html": html_pagina})


CONTADOR_POR_STATUS = {
    STATUS_CRIADA: "paginas_criadas", STATUS_ATUALIZADA: "paginas_atualizadas",
    STATUS_PULADA: "paginas_puladas", STATUS_CONFLITO: "paginas_em_conflito",
    STATUS_APROVADA: "paginas_aprovadas", STATUS_REJEITADA: "paginas_rejeitadas",
}

ROTULO_ACAO_EVENTO = {
    STATUS_CRIADA: "página criada", STATUS_ATUALIZADA: "conteúdo atualizado",
    STATUS_APROVADA: "conflito resolvido — edição manual aprovada",
    STATUS_REJEITADA: "conflito resolvido — conteúdo oficial restaurado",
}


def carregar_json(caminho_arquivo: str) -> dict:
    return json.loads(Path(caminho_arquivo).read_text(encoding="utf-8"))


def filtrar_smsub(servicos: list[dict]) -> list[dict]:
    return [s for s in servicos if s]


def _registrar_evento(nome_bruto: str, rotulo_tipo: str, nome_livro: str, capitulo_nome: str,
                       status: str, resultado: dict, eventos: list) -> None:
    chave = CONTADOR_POR_STATUS.get(status)
    if chave:
        resultado[chave] += 1
    rotulo_acao = ROTULO_ACAO_EVENTO.get(status)
    if rotulo_acao:
        eventos.append({
            "nome": nome_bruto, "rotulo_tipo": rotulo_tipo, "livro": nome_livro, "capitulo": capitulo_nome,
            "acao": rotulo_acao,
        })
    print(f"    [{status}] {nome_bruto} ({rotulo_tipo}) — {nome_livro} › {capitulo_nome}")


def publicar_no_bookstack(arquivo: str, apenas_um: bool = False) -> dict:
    NOME_ESTANTE = "SP156"
    dados = carregar_json(arquivo)
    estante_id = obter_estante(NOME_ESTANTE)

    resultado = {
        "livros": 0, "capitulos": 0, "paginas_criadas": 0, "paginas_atualizadas": 0,
        "paginas_puladas": 0, "paginas_em_conflito": 0, "paginas_aprovadas": 0, "paginas_rejeitadas": 0,
        "paginas_sem_conteudo": 0, "paginas_erro": 0,
    }
    eventos_desta_execucao: list[dict] = []

    for categoria, servicos in dados.items():
        filtrados = filtrar_smsub(servicos)
        if not filtrados:
            continue

        try:
            livro_id = obter_livro(categoria, estante_id)
            resultado["livros"] += 1
        except Exception as erro:
            print(f"[ERRO] Livro '{categoria}': {erro}")
            resultado["paginas_erro"] += len(filtrados)
            continue

        print(f"[Livro] {categoria} - {len(filtrados)} item(ns)")

        for capitulo_nome, itens in agrupar_por_capitulo(filtrados, categoria).items():
            capitulo_id = None  

            for servico in itens:
                nome_bruto = (servico.get("nome") or servico.get("categoria") or "Sem nome").strip()
                tipo = servico.get("tipo", "servico")
                rotulo_tipo = "Serviço Online" if tipo == "servico" else "Informativo"

                if not tem_conteudo_real(servico):
                    resultado["paginas_sem_conteudo"] += 1
                    continue

                if capitulo_id is None:
                    try:
                        capitulo_id = obter_capitulo(capitulo_nome, livro_id)
                        resultado["capitulos"] += 1
                    except Exception as erro:
                        print(f"    [ERRO] Capítulo '{capitulo_nome}': {erro}")
                        resultado["paginas_erro"] += 1
                        break  

                try:
                    status = publicar_pagina(servico, capitulo_nome, capitulo_id, livro_id)
                except Exception as erro:
                    print(f"    [ERRO] {nome_bruto} ({rotulo_tipo}): {erro}")
                    resultado["paginas_erro"] += 1
                    continue

                _registrar_evento(nome_bruto, rotulo_tipo, categoria, capitulo_nome, status, resultado, eventos_desta_execucao)

        if apenas_um:
            break

    publicar_pagina_de_atualizacoes(estante_id, eventos_desta_execucao)
    print(f"\n'{NOME_ESTANTE}' concluido: {resultado}")
    return resultado