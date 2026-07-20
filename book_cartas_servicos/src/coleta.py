import copy
import json
import random
import re
import time
import tempfile
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
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://sp156.prefeitura.sp.gov.br/portal/servicos-online",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# JSON em disco - usado pra checkpoint em todas as etapas

def _ler_json(caminho: Path, padrao):
    """Lê um JSON do disco; devolve `padrao` se o arquivo ainda não existe."""
    if not caminho.exists():
        return padrao
    return json.loads(caminho.read_text(encoding="utf-8"))


def _salvar_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


# Filtro de órgão (SMSUB) - decide se um item pertence à secretaria certa

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
    resultado = _tipo_e_codigo_da_url(link)
    return resultado[0] if resultado else "servico"

# Extração de campos de uma página de serviço

CAMPOS = [
    "O QUE É", "QUANDO SOLICITAR", "PÚBLICO-ALVO",
    "REQUISITOS, DOCUMENTOS E INFORMAÇÕES", "PRAZO MÁXIMO",
    "TAXAS OU PREÇO PÚBLICO", "CANAIS PARA SOLICITAR O SERVIÇO",
    "CANAIS PARA SOLICITAR", "PRINCIPAIS ETAPAS", "LEGISLAÇÃO",
    "OBSERVAÇÕES", "ÓRGÃO RESPONSÁVEL",
    "MANIFESTAÇÃO SOBRE SERVIÇO", "MANIFESTAÇÃO SOBRE O SERVIÇO",
    "CRIADO EM", "ATUALIZADO EM",
]

MARCADOR_LINK_INICIO = "\u2060"
MARCADOR_LINK_MEIO = "\u2061"
MARCADOR_LINK_FIM = "\u2062"
MARCADOR_PARAGRAFO = "\u2063"


def _preparar_bloco_para_extracao(bloco):
    bloco = copy.copy(bloco)

    for a in bloco.find_all("a", href=True):
        texto_link = a.get_text(" ", strip=True)
        href = a["href"]
        a.replace_with(f"{MARCADOR_LINK_INICIO}{texto_link}{MARCADOR_LINK_MEIO}{href}{MARCADOR_LINK_FIM}")

    for tag in bloco.find_all(["p", "div", "li", "br"]):
        tag.insert_after(MARCADOR_PARAGRAFO)

    return bloco

def campos_da_pagina(soup):
    bloco = soup.find("div", id="servicos-texto-holder") or soup
    bloco_preparado = _preparar_bloco_para_extracao(bloco)
    texto = re.sub(r"\s+", " ", bloco_preparado.get_text(" ", strip=True))

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
        if titulo not in dados:  
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

# Etapa 1: pegar_menu (Selenium)

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


# Etapa 2: varrer_ids (HTTP simples, em paralelo)

def _criar_sessao_http(total_tentativas: int = 5, espera_base: float = 3) -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update(HEADERS)
    adaptador = HTTPAdapter(
        max_retries=Retry(
            total=total_tentativas, backoff_factor=espera_base,
            status_forcelist=[403, 429, 500, 502, 503, 504],
            allowed_methods=["GET"], respect_retry_after_header=True,
        ),
        pool_connections=20, pool_maxsize=20,
    )
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)

    try:
        sessao.get(URL_MENU, timeout=10)
    except requests.RequestException:
        pass

    return sessao

def _pausa_entre_requisicoes() -> None:
    time.sleep(random.uniform(0.4, 0.9))

def testar_id(sessao: requests.Session, numero_id: int, timeout: int = 10) -> tuple[list[dict], bool]:
    encontrados = []
    houve_erro_de_rede = False
    for tipo in ["servico", "conteudo"]:
        url = f"{URL_INFO}?{tipo}={numero_id}"
        try:
            _pausa_entre_requisicoes()
            resposta = sessao.get(url, timeout=timeout, allow_redirects=True)
            if resposta.status_code != 200:
                continue

            soup = BS(resposta.text, "html.parser")
            bloco = soup.find("div", id="servicos-texto-holder")
            if not bloco:
                continue

            informacoes = campos_da_pagina(soup)
            orgao_bruto = informacoes.get("ÓRGÃO RESPONSÁVEL", "")
            if not orgao_e_da_smsub(orgao_bruto):
                continue  

            tipo_real = extrair_classificacao(resposta.url)
            resultado_url = _tipo_e_codigo_da_url(resposta.url)
            codigo_real = resultado_url[1] if resultado_url else str(numero_id)
            nome, categoria, grupo, caminho = extrair_nome_e_caminho(soup, codigo_real)

            encontrados.append({
                "categoria": categoria, "caminho": caminho, "link": resposta.url,
                "data_extracao": (datetime.now() - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M:%S"),
                "codigo_servico": codigo_real, "caminho_servico": [categoria, grupo],
                "nome": nome, "tipo": tipo_real, "informacoes": informacoes,
                "html_original": str(bloco) if bloco else "",
            })

        except requests.RequestException as erro:
            print(f"  erro ao testar ID {numero_id} ({tipo}): {erro}")
            houve_erro_de_rede = True
    return encontrados, houve_erro_de_rede


def _testar_lote_de_ids(ids_do_lote: list[int]) -> tuple[list[dict], bool]:
    resultados = []
    houve_erro_no_lote = False
    erros_consecutivos = 0
    sessao = _criar_sessao_http(total_tentativas=0, espera_base=0)
    try:
        for indice, numero_id in enumerate(ids_do_lote, start=1):
            try:
                encontrados, houve_erro = testar_id(sessao, numero_id)
                resultados.extend(encontrados)
                houve_erro_no_lote = houve_erro_no_lote or houve_erro

                if houve_erro:
                    erros_consecutivos += 1
                else:
                    erros_consecutivos = 0

                if erros_consecutivos >= 3:
                    print(f"  3 erros seguidos - pausando este lote por 60s antes de desistir dele.")
                    time.sleep(60)
                    houve_erro_no_lote = True
                    break
            except Exception as erro:
                print(f"  falha ao testar ID {numero_id}: {erro}")
                houve_erro_no_lote = True
            if indice % 20 == 0:
                print(f"  progresso do lote: {indice}/{len(ids_do_lote)} IDs testados")
    finally:
        sessao.close()
    return resultados, houve_erro_no_lote


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
                resultados, houve_erro_no_lote = futuro.result()
            except Exception as erro:
                print(f"Mini-lote {indice_mini_lote} falhou: {erro}")
                resultados = []
                houve_erro_no_lote = True

            if houve_erro_no_lote:
                print(f"Mini-lote {indice_mini_lote} teve erro de rede em algum ID - será testado de novo na próxima execução.")
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

# Etapa 3: completar_dados (HTTP simples, em paralelo) -> FILTRO INTEGRADO AQUI


def _salvar_completos(final: dict, caminho_saida: Path) -> None:
    _salvar_json(caminho_saida, dict(sorted(final.items())))


def completar_dados(
    menu_arq: str, extras_arq: str, saida: str,
    limite: int | None = None, checkpoint_a_cada: int = 100,
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
    ja_prontos = []  
    for item in entrada:
        tipo = str(item.get("tipo", "servico")).strip()
        codigo = str(item.get("codigo_servico") or item.get("id", "")).strip()
        if not codigo:
            continue
        chave = f"{tipo}:{codigo}"
        if chave in vistos:
            continue
        vistos.add(chave)

        if "informacoes" in item:
            ja_prontos.append(item)
        else:
            pendentes.append(item)

    for registro in ja_prontos:
        final[registro["categoria"]].append(registro)
    if ja_prontos:
        print(f"{len(ja_prontos)} páginas já vieram prontas da varredura (sem reabrir).")

    print(f"Processando {len(pendentes)} páginas novas (de {len(entrada)} no total), uma de cada vez...")
    sessao = _criar_sessao_http()
    processados = 0
    contagem_possivel_bloqueio = 0

    for item in pendentes:
        tipo = str(item.get("tipo", "servico")).strip()
        codigo = str(item.get("codigo_servico") or item.get("id", "")).strip()
        chave = f"{tipo}:{codigo}"
        link = item.get("link") or item.get("url") or f"{URL_INFO}?{tipo}={codigo}"
        processados += 1

        try:
            _pausa_entre_requisicoes()
            resposta = sessao.get(link, timeout=20, allow_redirects=True)
            soup = BS(resposta.text, "html.parser")
            bloco = soup.find("div", id="servicos-texto-holder")
            informacoes = campos_da_pagina(soup)
            orgao_bruto = informacoes.get("ÓRGÃO RESPONSÁVEL", "")
            tipo = extrair_classificacao(resposta.url)

            if not orgao_e_da_smsub(orgao_bruto):
                if bloco is None and not informacoes:
                    contagem_possivel_bloqueio += 1
                    print(f"[{processados}/{len(pendentes)}] {chave} SEM CONTEÚDO ({resposta.status_code}) — "
                          f"possível bloqueio/captcha do site, não filtro de órgão")
                else:
                    print(f"[{processados}/{len(pendentes)}] {chave} ignorado — órgão: {orgao_bruto or 'não encontrado'}")
                continue

            if item.get("nome") and item.get("categoria") and item.get("grupo"):
                nome, categoria, grupo = item["nome"], item["categoria"], item["grupo"]
                caminho_servico = item.get("caminho_servico", [categoria, grupo])
                caminho = item.get("caminho", ["Início", categoria, grupo, nome])
            else:
                nome, categoria, grupo, caminho = extrair_nome_e_caminho(soup, codigo)
                caminho_servico = [categoria, grupo]

            registro = {
                "categoria": categoria, "caminho": caminho, "link": resposta.url,
                "data_extracao": (datetime.now() - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M:%S"),
                "codigo_servico": codigo, "caminho_servico": caminho_servico,
                "nome": nome, "tipo": tipo, "informacoes": informacoes,
                "html_original": str(bloco) if bloco else "",
            }
            final[categoria].append(registro)
            print(f"[{processados}/{len(pendentes)}] {chave} OK — {categoria} › {nome}")

        except requests.RequestException as erro:
            print(f"[{processados}/{len(pendentes)}] {chave} ERRO de requisição: {erro}")
        except Exception as erro:
            print(f"[{processados}/{len(pendentes)}] {chave} ERRO: {erro}")

        if processados % checkpoint_a_cada == 0:
            _salvar_completos(final, caminho_saida)

    _salvar_completos(final, caminho_saida)
    total = sum(len(itens) for itens in final.values())
    print(f"Salvo: {total} registros válidos em {caminho_saida}")
    if contagem_possivel_bloqueio:
        print(f"AVISO: {contagem_possivel_bloqueio}/{len(pendentes)} páginas vieram sem conteúdo "
              f"reconhecível - forte indício de bloqueio/captcha do site, não de filtro de órgão.")

    proporcao_bloqueada = contagem_possivel_bloqueio / len(pendentes) if pendentes else 0
    if len(pendentes) >= 20 and (total == 0 or proporcao_bloqueada > 0.5):
        raise RuntimeError(
            f"completar_dados processou {len(pendentes)} páginas e só {total} passaram no filtro "
            f"de órgão (SMSUB) - {contagem_possivel_bloqueio} delas ({proporcao_bloqueada:.0%}) sem "
            f"conteúdo reconhecível. O mais provável é bloqueio/captcha/rate-limit do site, não que "
            f"quase todos os itens sejam de outro órgão."
        )
    return total