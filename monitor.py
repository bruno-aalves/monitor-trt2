import os
import re
import time
import math
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURAÇÕES
# ============================================================

API_URL = os.getenv(
    "PJE_API_URL",
    "https://comunicaapi.pje.jus.br/api/v1/comunicacao",
)

TRIBUNAL = "TRT2"
ITENS_POR_PAGINA = 50

VARAS_ALVO = [
    "1ª Vara do Trabalho de Mogi das Cruzes",
    "2ª Vara do Trabalho de Mogi das Cruzes",
    "3ª Vara do Trabalho de Mogi das Cruzes",
    "4ª Vara do Trabalho de Mogi das Cruzes",
]

# Número de páginas consultadas simultaneamente.
# 8 costuma acelerar bastante sem exagerar na quantidade de requisições.
MAX_WORKERS = 8

# Quantidade máxima de tentativas para uma mesma página.
MAX_TENTATIVAS = 8


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar(texto):
    """
    Converte para minúsculas, remove acentos e padroniza espaços.

    Exemplos:
        "DISTRIBUÍDO" -> "distribuido"
        "1ª Vara do Trabalho" -> "1 vara do trabalho"
    """
    if texto is None:
        return ""

    texto = str(texto)
    texto = texto.replace("ª", "").replace("º", "")

    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )

    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)

    return " ".join(texto.split())


VARAS_ALVO_NORMALIZADAS = [
    normalizar(vara) for vara in VARAS_ALVO
]


def obter_numero_processo(item):
    """
    Tenta localizar o número do processo mesmo que a API varie
    levemente o nome do campo.
    """
    return (
        item.get("numeroProcesso")
        or item.get("numeroprocessocommascara")
        or item.get("numero_processo")
        or "Não informado"
    )


def obter_data(item):
    return (
        item.get("dataDisponibilizacao")
        or item.get("datadisponibilizacao")
        or "Não informada"
    )


def contem_palavra_distribuido(texto):
    """
    Procura a PALAVRA INTEIRA 'distribuido'.

    Assim:
        DISTRIBUÍDO   -> aceita
        Distribuído   -> aceita
        distribuido   -> aceita

        DISTRIBUIDORA -> NÃO aceita
        distribuidor  -> NÃO aceita
    """
    texto_normalizado = normalizar(texto)

    return bool(
        re.search(r"\bdistribuido\b", texto_normalizado)
    )


def item_eh_compativel(item):
    """
    Retorna True apenas quando:
    1. pertence a uma das 4 Varas do Trabalho de Mogi das Cruzes; e
    2. o teor contém a palavra inteira DISTRIBUÍDO.
    """
    orgao = normalizar(item.get("nomeOrgao"))

    vara_compativel = any(
        vara_alvo in orgao
        for vara_alvo in VARAS_ALVO_NORMALIZADAS
    )

    if not vara_compativel:
        return False

    return contem_palavra_distribuido(item.get("texto"))


def chave_unica(item):
    """
    Evita duplicidade da mesma comunicação.

    A API pode trazer a mesma publicação mais de uma vez, por exemplo
    quando existem diferentes intimados/citados.
    """
    hash_publicacao = item.get("hash")
    if hash_publicacao:
        return ("hash", str(hash_publicacao))

    link = item.get("link")
    if link:
        return ("link", str(link))

    return (
        "fallback",
        obter_numero_processo(item),
        item.get("nomeOrgao", ""),
        obter_data(item),
        normalizar(item.get("texto", "")),
    )


# ============================================================
# REQUISIÇÕES COM RETRY
# ============================================================

def fazer_requisicao(parametros):
    """
    Faz uma requisição e tenta novamente quando houver falha temporária.
    """
    ultimo_erro = None

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resposta = requests.get(
                API_URL,
                params=parametros,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "monitor-trt2/1.0",
                },
                timeout=90,
            )

            if resposta.status_code == 403:
                raise RuntimeError(
                    "Comunica PJe retornou HTTP 403. "
                    "Verifique o Secret PJE_API_URL e o proxy brasileiro."
                )

            if resposta.status_code in (429, 500, 502, 503, 504):
                ultimo_erro = RuntimeError(
                    f"HTTP {resposta.status_code}"
                )

                if tentativa < MAX_TENTATIVAS:
                    espera = min(3 * tentativa, 20)

                    print(
                        f"⚠️ Página {parametros.get('pagina')} retornou "
                        f"HTTP {resposta.status_code}. "
                        f"Tentativa {tentativa}/{MAX_TENTATIVAS}. "
                        f"Nova tentativa em {espera}s."
                    )

                    time.sleep(espera)
                    continue

            resposta.raise_for_status()

            try:
                return resposta.json()

            except ValueError as erro_json:
                raise RuntimeError(
                    "A API respondeu, mas não retornou JSON válido."
                ) from erro_json

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as erro:
            ultimo_erro = erro

            if tentativa < MAX_TENTATIVAS:
                espera = min(3 * tentativa, 20)

                print(
                    f"⚠️ Falha de conexão na página "
                    f"{parametros.get('pagina')}. "
                    f"Tentativa {tentativa}/{MAX_TENTATIVAS}. "
                    f"Nova tentativa em {espera}s."
                )

                time.sleep(espera)
                continue

        except requests.exceptions.RequestException as erro:
            ultimo_erro = erro
            break

    raise RuntimeError(
        f"Página {parametros.get('pagina')} falhou após "
        f"{MAX_TENTATIVAS} tentativas. Último erro: {ultimo_erro}"
    )


def consultar_pagina(pagina, hoje):
    """
    Consulta uma página específica.
    """
    parametros = {
        "siglaTribunal": TRIBUNAL,
        "dataDisponibilizacaoInicio": hoje,
        "dataDisponibilizacaoFim": hoje,
        "pagina": pagina,
        "itensPorPagina": ITENS_POR_PAGINA,
    }

    dados = fazer_requisicao(parametros)

    itens = dados.get("items", [])

    if not isinstance(itens, list):
        raise RuntimeError(
            f"Resposta inesperada na página {pagina}: "
            "o campo 'items' não é uma lista."
        )

    return pagina, dados, itens


# ============================================================
# CONSULTA PRINCIPAL
# ============================================================

def consultar_publicacoes():
    hoje = datetime.now(
        ZoneInfo("America/Sao_Paulo")
    ).strftime("%Y-%m-%d")

    print()
    print("=" * 70)
    print("MONITOR TRT2")
    print("Varas do Trabalho de Mogi das Cruzes")
    print("Filtro: palavra inteira 'DISTRIBUÍDO'")
    print(f"Data da consulta (São Paulo): {hoje}")
    print(f"Consultas simultâneas: {MAX_WORKERS}")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # 1. Consulta a primeira página separadamente
    # --------------------------------------------------------

    print("Consultando página 1 para descobrir o total de resultados...")

    _, dados_primeira, itens_primeira = consultar_pagina(1, hoje)

    try:
        total_registros = int(dados_primeira.get("count", 0))
    except (TypeError, ValueError):
        total_registros = 0

    if total_registros > 0:
        total_paginas = math.ceil(
            total_registros / ITENS_POR_PAGINA
        )
    else:
        # Se a API não informar count, ao menos processamos a página 1.
        total_paginas = 1

    print(f"Total informado pela API: {total_registros}")
    print(f"Total estimado de páginas: {total_paginas}")
    print()

    resultados = []

    # Processa página 1.
    for item in itens_primeira:
        if item_eh_compativel(item):
            resultados.append(item)

    print(
        f"Página 1 concluída. "
        f"Compatíveis encontrados: {len(resultados)}"
    )

    # --------------------------------------------------------
    # 2. Consulta as demais páginas em paralelo
    # --------------------------------------------------------

    if total_paginas > 1:
        print()
        print(
            f"Consultando páginas 2 a {total_paginas} "
            f"com até {MAX_WORKERS} requisições simultâneas..."
        )
        print()

        concluidas = 1

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futuros = {
                executor.submit(
                    consultar_pagina,
                    pagina,
                    hoje,
                ): pagina
                for pagina in range(2, total_paginas + 1)
            }

            for futuro in as_completed(futuros):
                pagina = futuros[futuro]

                try:
                    pagina_retornada, _, itens = futuro.result()

                except Exception as erro:
                    # Uma página que continua falhando depois de todos
                    # os retries deve derrubar a execução, pois em um
                    # monitor jurídico não queremos fingir que a consulta
                    # foi completa.
                    raise RuntimeError(
                        f"Falha definitiva na página {pagina}: {erro}"
                    ) from erro

                for item in itens:
                    if item_eh_compativel(item):
                        resultados.append(item)

                concluidas += 1

                # Mostra progresso sem imprimir 895 blocos enormes.
                if (
                    concluidas % 25 == 0
                    or concluidas == total_paginas
                ):
                    percentual = (
                        concluidas / total_paginas
                    ) * 100

                    print(
                        f"Progresso: {concluidas}/{total_paginas} páginas "
                        f"({percentual:.1f}%) | "
                        f"compatíveis brutos: {len(resultados)}"
                    )

    # --------------------------------------------------------
    # 3. Remove duplicidades
    # --------------------------------------------------------

    unicos = {}
    for item in resultados:
        chave = chave_unica(item)

        if chave not in unicos:
            unicos[chave] = item

    publicacoes_unicas = list(unicos.values())

    # Ordena por órgão e número do processo para facilitar leitura.
    publicacoes_unicas.sort(
        key=lambda item: (
            normalizar(item.get("nomeOrgao", "")),
            obter_numero_processo(item),
        )
    )

    print()
    print(
        f"Compatíveis antes da remoção de duplicidades: "
        f"{len(resultados)}"
    )
    print(
        f"Publicações únicas após deduplicação: "
        f"{len(publicacoes_unicas)}"
    )

    return publicacoes_unicas


# ============================================================
# EXIBIÇÃO FINAL
# ============================================================

def exibir_resultados(publicacoes):
    print()
    print("=" * 70)
    print(f"PUBLICAÇÕES ENCONTRADAS: {len(publicacoes)}")
    print("=" * 70)

    if not publicacoes:
        print()
        print(
            "Nenhuma publicação correspondente aos filtros "
            "foi encontrada."
        )
        print("=" * 70)
        return

    for indice, item in enumerate(publicacoes, start=1):
        processo = obter_numero_processo(item)
        orgao = item.get("nomeOrgao", "Não informado")
        data = obter_data(item)
        tipo = item.get(
            "tipoComunicacao",
            "Não informado",
        )
        link = item.get("link", "")
        texto = item.get("texto", "")

        print()
        print(f"[{indice}] Processo: {processo}")
        print(f"Órgão: {orgao}")
        print(f"Data de disponibilização: {data}")
        print(f"Tipo de comunicação: {tipo}")

        if link:
            print(f"Link: {link}")

        print("-" * 70)
        print("Teor:")
        print(texto)
        print("=" * 70)


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    try:
        publicacoes = consultar_publicacoes()
        exibir_resultados(publicacoes)

    except Exception as erro:
        print()
        print("=" * 70)
        print("❌ ERRO NO MONITOR")
        print("=" * 70)
        print(str(erro))
        print("=" * 70)

        # Mantém o GitHub Actions como erro quando a consulta
        # não tiver sido concluída corretamente.
        raise
