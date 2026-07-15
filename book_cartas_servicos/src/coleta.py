
"""
Coleta os serviços/informativos do portal SP156.

Três etapas, cada uma com checkpoint próprio pra poder retomar se cair:
  1) pegar_menu       - Selenium: navega o menu e lista tudo que existe por lá
  2) varrer_ids       - HTTP simples: testa uma faixa de IDs em paralelo,
                        pra achar página que não aparece no menu
  3) completar_dados  - HTTP simples: abre cada página achada e extrai o
                        conteúdo (campos O QUE É, PRAZO MÁXIMO etc.)
"""
import json
import random
import re
import time
import tempfile
import threading
import unicodedata
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, parse_qs, urlparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from bs4 import BeautifulSoup as BS
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


URL_MENU = "https://sp156.prefeitura.sp.gov.br/portal/servicos-online"
URL_INFO = "https://sp156.prefeitura.sp.gov.br/portal/servicos/informacao"
# User-Agent genérico ("Mozilla/5.0" sozinho) tende a ser reconhecido como bot
# por proteções tipo Cloudflare/Akamai, que respondem 200 com uma página de
# desafio em vez do conteúdo real - daí o pipeline "roda" mas extrai nada.
# Um conjunto de headers mais parecido com navegador real reduz esse risco.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://sp156.prefeitura.sp.gov.br/portal/servicos-online",
}


# =============================================================================
# JSON em disco - usado pra checkpoint em todas as etapas
# =============================================================================

def _ler_json(caminho: Path, padrao):
    """Lê um JSON do disco; devolve `padrao` se o arquivo ainda não existe."""
    if not caminho.exists():
        return padrao
    return json.loads(caminho.read_text(encoding="utf-8"))


def _salvar_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


# =============================================================================
# Filtro de órgão (SMSUB) - decide se um item pertence à secretaria certa
# =============================================================================

def normalizar_orgao(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = texto.strip().lower()
    for caractere in ["-", "–", "(", ")", "/"]:
        texto = texto.replace(caractere, " ")
    return " ".join(texto.split())


ORGAOS = [normalizar_orgao(item) for item in [
    "smsub", "sp156", "selimp",
    "secretaria municipal das subprefeituras",
    "secretaria executiva de limpeza urbana",
    "secretaria executiva de limpeza urbana (selimp)",
    "secretaria municipal das subprefeituras – smsub",
]]


def orgao_e_da_smsub(texto_orgao: str) -> bool:
    texto_normalizado = normalizar_orgao(texto_orgao)
    return bool(texto_normalizado) and any(ancora in texto_normalizado for ancora in ORGAOS)


def extrair_classificacao(link: str) -> str:
    """Decide 'servico' ou 'conteudo' (Informativo) pelo parâmetro que
    aparece na URL final da página (depois de qualquer redirecionamento) -
    é o dado mais confiável que temos: 'conteudo=' no link é informativo,
    qualquer outra coisa é serviço."""
    resultado = _tipo_e_codigo_da_url(link)
    return resultado[0] if resultado else "servico"


# =============================================================================
# Extração de campos de uma página de serviço
# =============================================================================

# IMPORTANTE: o site cola o texto de fontes diferentes (Word/Google Docs) e
# isso às vezes fragmenta um único título em vários <span> aninhados, tipo
# <strong><span>ÓRGÃO</span><span> RESPONSÁVEL</span></strong>. Se a gente
# lesse linha a linha (get_text com separador de quebra de linha, ou o texto
# de cada tag isolada), isso vira "ÓRGÃO" e "RESPONSÁVEL" em linhas
# separadas - ou até "ÓRGÃORESPONSÁVEL" grudado, sem espaço - e o título
# nunca bate. Por isso a extração usa o texto CONTÍNUO do bloco inteiro
# (separador de espaço) e acha os títulos por posição no texto, não por
# linha exata.
CAMPOS = [
    "O QUE É", "QUANDO SOLICITAR", "PÚBLICO-ALVO",
    "REQUISITOS, DOCUMENTOS E INFORMAÇÕES", "PRAZO MÁXIMO",
    "TAXAS OU PREÇO PÚBLICO", "CANAIS PARA SOLICITAR O SERVIÇO",
    "CANAIS PARA SOLICITAR", "PRINCIPAIS ETAPAS", "LEGISLAÇÃO",
    "OBSERVAÇÕES", "ÓRGÃO RESPONSÁVEL",
    "MANIFESTAÇÃO SOBRE SERVIÇO", "MANIFESTAÇÃO SOBRE O SERVIÇO",
    "CRIADO EM", "ATUALIZADO EM",
]


def campos_da_pagina(soup):
    bloco = soup.find("div", id="servicos-texto-holder") or soup
    texto = re.sub(r"\s+", " ", bloco.get_text(" ", strip=True))

    posicoes = []
    for titulo in CAMPOS:
        m = re.search(re.escape(titulo), texto)
        if m:
            posicoes.append((m.start(), m.end(), titulo))
    posicoes.sort()

    dados = {}
    for indice, (_, fim_titulo, titulo) in enumerate(posicoes):
        fim_conteudo = posicoes[indice + 1][0] if indice + 1 < len(posicoes) else len(texto)
        conteudo = texto[fim_titulo:fim_conteudo].strip(" :-–.")
        if titulo not in dados:  # primeira ocorrência vence
            dados[titulo] = conteudo

    return dados


def extrair_nome_e_caminho(soup, codigo):
    nome = ""
    h2 = soup.find("h2", class_="titulo-servico")
    if h2:
        span = h2.find("span", class_="sp_red-text")
        if span:
            nome = span.get_text(strip=True)
    nome = nome or f"Conteúdo {codigo}"

    breadcrumb = soup.find("div", class_="sp_breadcrumb")
    partes = []
    if breadcrumb:
        for a in breadcrumb.find_all("a"):
            texto = a.get_text(strip=True)
            if texto and texto not in partes:
                partes.append(texto)

    categoria = partes[1] if len(partes) > 1 else "Sem Categoria"
    grupo = partes[2] if len(partes) > 2 else "Sem Grupo"
    caminho = partes if len(partes) >= 4 else ["Início", categoria, grupo, nome]
    if caminho and caminho[-1] != nome:
        caminho = caminho[:-1] + [nome]

    return nome, categoria, grupo, caminho


def _tipo_e_codigo_da_url(url: str) -> tuple[str, str] | None:
    parametros = parse_qs(urlparse(url).query)
    if "servico" in parametros:
        return "servico", parametros["servico"][0]
    if "conteudo" in parametros:
        return "conteudo", parametros["conteudo"][0]
    return None


# =============================================================================
# Etapa 1: pegar_menu (Selenium)
# =============================================================================

def criar_driver():
    options = Options()
    options.binary_location = "/usr/bin/chromium"
    for arg in [
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--window-size=1920,1080", "--remote-debugging-port=0", "--disable-extensions",
        "--disable-plugins", "--blink-settings=imagesEnabled=false",
        "--disable-background-networking", "--disable-default-apps", "--disable-sync",
        "--js-flags=--max-old-space-size=256",
    ]:
        options.add_argument(arg)
    options.add_argument(f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-perfil-')}")
    return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)


def pegar_menu(saida: str, checkpoint_a_cada_categorias: int = 5) -> int:
    caminho_saida = Path(saida)
    caminho_checkpoint = caminho_saida.with_suffix(".checkpoint.json")

    dados, categorias_concluidas = [], set()
    estado_salvo = _ler_json(caminho_checkpoint, {})
    if estado_salvo and not estado_salvo.get("concluido", False):
        dados = estado_salvo.get("dados", [])
        categorias_concluidas = set(estado_salvo.get("categorias_concluidas", []))
        print(f"Checkpoint de execucao interrompida encontrado: retomando com "
              f"{len(categorias_concluidas)} categoria(s) ja concluida(s) e {len(dados)} item(ns) salvos.")

    vistos = {f"{item['tipo']}:{item['codigo_servico']}" for item in dados}

    def _salvar_checkpoint(concluido: bool) -> None:
        _salvar_json(caminho_checkpoint, {
            "concluido": concluido,
            "categorias_concluidas": sorted(categorias_concluidas),
            "dados": dados,
        })

    driver = criar_driver()
    try:
        driver.get(URL_MENU)
        time.sleep(3)
        soup = BS(driver.page_source, "html.parser")

        categorias = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "servicos-online?id=" in href or href.startswith("?id="):
                link = urljoin(URL_MENU, href)
                nome = a.get_text(strip=True)
                if nome and not any(c["link"] == link for c in categorias):
                    categorias.append({"nome": nome, "link": link})

        print(f"Categorias encontradas: {len(categorias)}")

        for indice, categoria in enumerate(categorias, start=1):
            if categoria["nome"] in categorias_concluidas:
                print(f"[{indice}/{len(categorias)}] {categoria['nome']} -- ja concluida no checkpoint, pulando")
                continue

            print(f"\n[{indice}/{len(categorias)}] {categoria['nome']}")
            driver.get(categoria["link"])

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.collapsible-header"))
                )
                time.sleep(6)

                for cabecalho_grupo in driver.find_elements(By.CSS_SELECTOR, "div.collapsible-header"):
                    grupo = cabecalho_grupo.text.strip().split("\n")[0].strip()
                    if not grupo:
                        continue

                    driver.execute_script("arguments[0].click();", cabecalho_grupo)
                    time.sleep(1.5)

                    corpo = cabecalho_grupo.find_element(
                        By.XPATH, "following-sibling::div[contains(@class,'collapsible-body')]"
                    )
                    links_itens = corpo.find_elements(By.CSS_SELECTOR, "a[href*='servico='], a[href*='conteudo=']")

                    for item in links_itens:
                        nome_item = item.text.strip()
                        link_item = item.get_attribute("href")
                        resultado = _tipo_e_codigo_da_url(link_item)
                        if resultado is None or not nome_item:
                            continue

                        tipo, codigo = resultado
                        chave = f"{tipo}:{codigo}"
                        if not codigo or chave in vistos:
                            continue
                        vistos.add(chave)

                        dados.append({
                            "tipo": tipo, "codigo_servico": codigo, "nome": nome_item,
                            "titulo": categoria["nome"], "categoria": categoria["nome"], "grupo": grupo,
                            "caminho_servico": [categoria["nome"], grupo],
                            "caminho": ["Inicio", categoria["nome"], grupo, nome_item],
                            "link": link_item,
                        })

                coletados = sum(1 for item in dados if item["categoria"] == categoria["nome"])
                print(f"  coletados: {coletados}")
                categorias_concluidas.add(categoria["nome"])

            except Exception as erro:
                print(f"  erro ao processar a categoria '{categoria['nome']}': {erro}")

            if indice % checkpoint_a_cada_categorias == 0:
                _salvar_checkpoint(concluido=False)
                print(f"  [checkpoint salvo apos {indice}/{len(categorias)} categorias]")
    finally:
        driver.quit()

    _salvar_json(caminho_saida, dados)
    _salvar_checkpoint(concluido=True)
    print(f"\nMenu salvo: {len(dados)} registros em {caminho_saida}")
    return len(dados)


# =============================================================================
# Etapa 2: varrer_ids (HTTP simples, em paralelo)
# =============================================================================

def _criar_sessao_http() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update(HEADERS)
    adaptador = HTTPAdapter(
        # 403/429 entraram na lista: o site bloqueia por RATE (muita
        # requisição rápida), não é um "acesso negado" permanente - um
        # retry com backoff maior resolve na maioria das vezes. Se fosse
        # bloqueio definitivo de IP, os 13 itens que passaram nesta mesma
        # execução também teriam sido bloqueados.
        max_retries=Retry(
            total=5, backoff_factor=3,
            status_forcelist=[403, 429, 500, 502, 503, 504],
            allowed_methods=["GET"], respect_retry_after_header=True,
        ),
        pool_connections=20, pool_maxsize=20,
    )
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao


def _pausa_entre_requisicoes() -> None:
    """Pequena pausa com variação aleatória antes de cada requisição.
    Sem isso, N workers em paralelo martelam o site quase ao mesmo tempo
    e o WAF do site passa a bloquear com 403 em massa - foi exatamente
    isso que gerou 602/615 páginas bloqueadas numa execução real."""
    time.sleep(random.uniform(0.4, 0.9))


def testar_id(sessao: requests.Session, numero_id: int, timeout: int = 10) -> list[dict]:
    encontrados = []
    for tipo in ["servico", "conteudo"]:
        url = f"{URL_INFO}?{tipo}={numero_id}"
        try:
            _pausa_entre_requisicoes()
            resposta = sessao.get(url, timeout=timeout, allow_redirects=True)
            if resposta.status_code != 200:
                continue

            soup = BS(resposta.text, "html.parser")
            if not soup.find("div", id="servicos-texto-holder"):
                continue

            resultado = _tipo_e_codigo_da_url(resposta.url)
            tipo_real, codigo_real = resultado if resultado else (tipo, str(numero_id))
            encontrados.append({"tipo": tipo_real, "codigo_servico": str(codigo_real), "link": resposta.url})

        except requests.RequestException as erro:
            print(f"  erro ao testar ID {numero_id} ({tipo}): {erro}")
    return encontrados


def _testar_lote_de_ids(ids_do_lote: list[int]) -> list[dict]:
    resultados = []
    sessao = _criar_sessao_http()
    try:
        for indice, numero_id in enumerate(ids_do_lote, start=1):
            try:
                resultados.extend(testar_id(sessao, numero_id))
            except Exception as erro:
                print(f"  falha ao testar ID {numero_id}: {erro}")
            if indice % 20 == 0:
                print(f"  progresso do lote: {indice}/{len(ids_do_lote)} IDs testados")
    finally:
        sessao.close()
    return resultados


def _carregar_checkpoint_varredura(caminho_checkpoint: Path) -> set:
    return set(_ler_json(caminho_checkpoint, {}).get("mini_lotes_concluidos", []))


def _salvar_checkpoint_varredura(caminho_checkpoint: Path, mini_lotes_concluidos: set) -> None:
    _salvar_json(caminho_checkpoint, {"mini_lotes_concluidos": sorted(mini_lotes_concluidos)})


def _salvar_achados(achados: list[dict], caminho_saida: Path) -> None:
    achados_ordenados = sorted(achados, key=lambda item: (item["tipo"], int(item["codigo_servico"])))
    _salvar_json(caminho_saida, achados_ordenados)


def varrer_ids(
    menu_arq: str, saida: str, id_inicio: int, id_fim: int,
    workers: int = 2, tamanho_mini_lote: int = 100, forcar_varredura_completa: bool = False,
) -> int:
    caminho_menu = Path(menu_arq)
    caminho_saida = Path(saida)
    caminho_checkpoint = caminho_saida.with_suffix(".varredura_checkpoint.json")

    if forcar_varredura_completa:
        print("Modo de varredura completa ativado: todos os mini-lotes serão testados novamente.")
        mini_lotes_ja_concluidos = set()
    else:
        mini_lotes_ja_concluidos = _carregar_checkpoint_varredura(caminho_checkpoint)
        if mini_lotes_ja_concluidos:
            print(f"Checkpoint de varredura encontrado: {len(mini_lotes_ja_concluidos)} mini-lote(s) já concluído(s), pulando.")

    menu = _ler_json(caminho_menu, [])
    vistos = {f"{item['tipo']}:{item['codigo_servico']}" for item in menu}

    achados = _ler_json(caminho_saida, [])
    if caminho_saida.exists():
        for item in achados:
            vistos.add(f"{item['tipo']}:{item['codigo_servico']}")
        print(f"Checkpoint encontrado: {len(achados)} extras já salvos anteriormente.")

    todos_ids = list(range(id_inicio, id_fim + 1))
    mini_lotes = [todos_ids[i:i + tamanho_mini_lote] for i in range(0, len(todos_ids), tamanho_mini_lote)]

    mini_lotes_concluidos = set(mini_lotes_ja_concluidos)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tarefas = {
            executor.submit(_testar_lote_de_ids, mini_lote): indice
            for indice, mini_lote in enumerate(mini_lotes, start=1)
            if indice not in mini_lotes_ja_concluidos
        }
        
        for futuro in as_completed(tarefas):
            indice_mini_lote = tarefas[futuro]
            try:
                resultados = futuro.result()
            except Exception as erro:
                print(f"Mini-lote {indice_mini_lote} falhou: {erro}")
                resultados = []
            else:
                mini_lotes_concluidos.add(indice_mini_lote)

            for item in resultados:
                chave = f"{item['tipo']}:{item['codigo_servico']}"
                if chave not in vistos:
                    vistos.add(chave)
                    achados.append(item)
                    print(f"Extra encontrado: {chave}")

            _salvar_achados(achados, caminho_saida)
            _salvar_checkpoint_varredura(caminho_checkpoint, mini_lotes_concluidos)

    _salvar_achados(achados, caminho_saida)
    _salvar_checkpoint_varredura(caminho_checkpoint, mini_lotes_concluidos)
    print(f"Extras salvos: {len(achados)} em {caminho_saida}")
    return len(achados)


# =============================================================================
# Etapa 3: completar_dados (HTTP simples, em paralelo) -> FILTRO INTEGRADO AQUI
# =============================================================================

def _salvar_completos(final: dict, caminho_saida: Path) -> None:
    _salvar_json(caminho_saida, dict(sorted(final.items())))


def completar_dados(
    menu_arq: str, extras_arq: str, saida: str,
    limite: int | None = None, checkpoint_a_cada: int = 100, workers: int = 10,
) -> int:
    caminho_menu = Path(menu_arq)
    caminho_extras = Path(extras_arq)
    caminho_saida = Path(saida)

    entrada = _ler_json(caminho_menu, []) + _ler_json(caminho_extras, [])
    if limite is not None:
        entrada = entrada[:limite]

    final = defaultdict(list)
    vistos = set()
    if caminho_saida.exists():
        for categoria, itens in _ler_json(caminho_saida, {}).items():
            final[categoria] = itens
            for item in itens:
                vistos.add(f"{item['tipo']}:{item['codigo_servico']}")
        print(f"Checkpoint encontrado: {len(vistos)} páginas já processadas anteriormente.")

    pendentes = []
    for item in entrada:
        tipo = str(item.get("tipo", "servico")).strip()
        codigo = str(item.get("codigo_servico") or item.get("id", "")).strip()
        if not codigo:
            continue
        chave = f"{tipo}:{codigo}"
        if chave in vistos:
            continue
        vistos.add(chave)
        pendentes.append(item)

    print(f"Processando {len(pendentes)} páginas novas (de {len(entrada)} no total), {workers} em paralelo...")

    sessao_local = threading.local()

    def _obter_sessao() -> requests.Session:
        if not hasattr(sessao_local, "sessao"):
            sessao_local.sessao = _criar_sessao_http()
        return sessao_local.sessao

    def _processar_item(item: dict):
        tipo = str(item.get("tipo", "servico")).strip()
        codigo = str(item.get("codigo_servico") or item.get("id", "")).strip()
        chave = f"{tipo}:{codigo}"
        link = item.get("link") or item.get("url") or f"{URL_INFO}?{tipo}={codigo}"

        try:
            _pausa_entre_requisicoes()
            resposta = _obter_sessao().get(link, timeout=15, allow_redirects=True)
            soup = BS(resposta.text, "html.parser")
            bloco = soup.find("div", id="servicos-texto-holder")
            informacoes = campos_da_pagina(soup)
            orgao_bruto = informacoes.get("ÓRGÃO RESPONSÁVEL", "")
            tipo = extrair_classificacao(resposta.url)

            # ---> A MUDANÇA CIRÚRGICA DE ARQUITETURA ACONTECE AQUI:
            # Filtra e ignora o item na hora se não pertencer aos órgãos válidos
            if not orgao_e_da_smsub(orgao_bruto):
                # Se nem o container da página de serviço veio E nenhum campo foi
                # extraído, não é "outro órgão" - é sinal de bloqueio/captcha
                # (resposta 200 com página de desafio em vez do conteúdo real).
                if bloco is None and not informacoes:
                    return ("possivel_bloqueio", chave, resposta.status_code, None)
                return ("ignorado", chave, orgao_bruto, None)

            if item.get("nome") and item.get("categoria") and item.get("grupo"):
                nome, categoria, grupo = item["nome"], item["categoria"], item["grupo"]
                caminho_servico = item.get("caminho_servico", [categoria, grupo])
                caminho = item.get("caminho", ["Início", categoria, grupo, nome])
            else:
                nome, categoria, grupo, caminho = extrair_nome_e_caminho(soup, codigo)
                caminho_servico = [categoria, grupo]

            registro = {
                # Campos pedidos: categoria, caminho, codigo_servico, link,
                # tipo, informacoes, html_original, data_extracao.
                "categoria": categoria, "caminho": caminho, "link": resposta.url,
                "data_extracao": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "codigo_servico": codigo, "caminho_servico": caminho_servico,
                "nome": nome, "tipo": tipo, "informacoes": informacoes,
                "html_original": str(bloco) if bloco else "",
            }
            return ("ok", chave, categoria, registro)

        except requests.RequestException as erro:
            return ("erro_requisicao", chave, str(erro), None)
        except Exception as erro:
            return ("erro", chave, str(erro), None)

    processados = 0
    contagem_possivel_bloqueio = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tarefas = {executor.submit(_processar_item, item): item for item in pendentes}

        for futuro in as_completed(tarefas):
            processados += 1
            status, chave, extra, registro = futuro.result()

            if status == "ok":
                final[extra].append(registro)
                print(f"[{processados}/{len(pendentes)}] {chave} OK — {extra} › {registro['nome']}")
            elif status == "ignorado":
                print(f"[{processados}/{len(pendentes)}] {chave} ignorado — órgão: {extra or 'não encontrado'}")
            elif status == "possivel_bloqueio":
                contagem_possivel_bloqueio += 1
                print(f"[{processados}/{len(pendentes)}] {chave} SEM CONTEÚDO (status {extra}) — "
                      f"possível bloqueio/captcha do site, não filtro de órgão")
            elif status == "erro_requisicao":
                print(f"[{processados}/{len(pendentes)}] {chave} ERRO de requisição: {extra}")
            else:
                print(f"[{processados}/{len(pendentes)}] {chave} ERRO: {extra}")

            if processados % checkpoint_a_cada == 0:
                _salvar_completos(final, caminho_saida)

    _salvar_completos(final, caminho_saida)
    total = sum(len(itens) for itens in final.values())
    print(f"Salvo: {total} registros válidos em {caminho_saida}")
    if contagem_possivel_bloqueio:
        print(f"AVISO: {contagem_possivel_bloqueio}/{len(pendentes)} páginas vieram sem conteúdo "
              f"reconhecível - forte indício de bloqueio/captcha do site, não de filtro de órgão.")

    # Guarda contra bloqueio silencioso: se processamos várias páginas e
    # quase nenhuma passou, é muito mais provável que as respostas HTTP
    # tenham vindo de bloqueio/captcha/rate-limit (403, 200 com página de
    # desafio) do que quase todos os itens serem de outro órgão. Sem essa
    # trava, o pipeline "roda com sucesso" e publica quase vazio, do jeito
    # que aconteceu antes desta mudança (1 de 615 páginas processadas).
    proporcao_bloqueada = contagem_possivel_bloqueio / len(pendentes) if pendentes else 0
    if len(pendentes) >= 20 and (total == 0 or proporcao_bloqueada > 0.5):
        raise RuntimeError(
            f"completar_dados processou {len(pendentes)} páginas e só {total} passaram no filtro "
            f"de órgão (SMSUB) - {contagem_possivel_bloqueio} delas ({proporcao_bloqueada:.0%}) sem "
            f"conteúdo reconhecível. O mais provável é bloqueio/captcha/rate-limit do site, não que "
            f"quase todos os itens sejam de outro órgão. Reduza os workers (Variable "
            f"sp156_completar_dados_workers) e tente de novo antes de investigar o filtro."
        )
    return total
