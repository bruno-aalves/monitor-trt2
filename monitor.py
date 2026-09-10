import os
import re
import time
import math
import unicodedata
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

PALAVRA_ALVO = "DISTRIBUÍDO"

# Quantas vezes tentar novamente quando a API/proxy falhar.
MAX_TENTATIVAS = 8

# Pequena pausa entre páginas para não bombardear a API.
PAUSA_ENTRE_PAGINAS = 0.15


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar(texto):
    """
    Deixa o texto em minúsculas, sem acentos e com espaços padronizados.
    Ex.:
        '1ª Vara do Trabalho' -> '1 vara do trabalho'
        'DISTRIBUÍDO'         -> 'distribuido'
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


VARAS_ALVO_NORMALIZADAS = [normalizar(vara) for vara in VARAS_ALVO]
PALAVRA_ALVO_NORMALIZADA = normalizar(PALAVRA_ALVO)


def obter_numero_processo(item):
    """
    A API já apresentou variações de nomes de campo.
    Tentamos os formatos mais comuns.
    """
    return (
        item.get("numeroProcesso")
        or item.get("numeroprocessocommascara")
        or item.get("numero_processo")
        or "Não informado"
    )


def item_eh_compativel(item):
    """
    Retorna True somente quando:
    1. o órgão é uma das 4 Varas do Trabalho de Mogi das Cruzes; e
    2. o teor contém a palavra DISTRIBUÍDO.
    """
    orgao = normalizar(item.get("nomeOrgao"))
    texto = normalizar(item.get("texto"))

    vara_compativel = any(
        vara_alvo in orgao
        for vara_alvo in VARAS_ALVO_NORMALIZADAS
    )

    distribuido = PALAVRA_ALVO_NORMALIZADA in texto

    return vara_compativel and distribuido


def fazer_requisicao(parametros):
    """
    Faz a requisição com várias tentativas automáticas.

    Erros 500, 502, 503, 504 e 429 são tratados como temporários.
    O script espera alguns segundos e tenta novamente a MESMA página.
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
                    "Verifique se PJE_API_URL está apontando para o proxy "
                    "brasileiro da Vercel."
                )

            if resposta.status_code in (429, 500, 502, 503, 504):
                espera = min(5 * tentativa, 30)

                print(
                    f"⚠️ API retornou HTTP {resposta.status_code}. "
                    f"Tentativa {tentativa}/{MAX_TENTATIVAS}."
                )

                ultimo_erro = RuntimeError(
                    f"HTTP {resposta.status_code}: {resposta.text[:300]}"
                )

                if tentativa < MAX_TENTATIVAS:
                    print(f"Aguardando {espera} segundos e tentando novamente...")
                    time.sleep(espera)
                    continue

            resposta.raise_for_status()

            try:
                return resposta.json()
            except ValueError as erro_json:
                raise RuntimeError(
                    "A API respondeu, mas o conteúdo não era um JSON válido."
                ) from erro_json

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as erro:
            ultimo_erro = erro
            espera = min(5 * tentativa, 30)

            print(
                f"⚠️ Falha de conexão. "
                f"Tentativa {tentativa}/{MAX_TENTATIVAS}: {erro}"
            )

            if tentativa < MAX_TENTATIVAS:
                print(f"Aguardando {espera} segundos e tentando novamente...")
                time.sleep(espera)
                continue

        except requests.exceptions.RequestException as erro:
            ultimo_erro = erro
            break

    raise RuntimeError(
        f"Não foi possível consultar a API após "
        f"{MAX_TENTATIVAS} tentativas. Último erro: {ultimo_erro}"
    )


# ============================================================
# CONSULTA
# ============================================================

def consultar_publicacoes():
    # IMPORTANTE:
    # GitHub Actions usa UTC. Aqui forçamos o horário de São Paulo.
    hoje = datetime.now(
        ZoneInfo("America/Sao_Paulo")
    ).strftime("%Y-%m-%d")

    print()
    print("MONITOR TRT2")
    print("Varas do Trabalho de Mogi das Cruzes")
    print("Filtro: teor contém 'DISTRIBUÍDO'")
    print(f"Data da consulta (São Paulo): {hoje}")
    print()

    publicacoes_encontradas = []

    pagina = 1
    total_registros = None
    total_paginas = None

    while True:
        print("=" * 70)
        print(f"Consultando página {pagina}...")
        print(f"API utilizada: {API_URL}")
        print("=" * 70)

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
                "Resposta inesperada da API: o campo 'items' não é uma lista."
            )

        print(f"Resultados recebidos nesta página: {len(itens)}")

        # Na primeira página, guardamos o total informado pela API.
        if total_registros is None:
            try:
                total_registros = int(dados.get("count", 0))
            except (TypeError, ValueError):
                total_registros = 0

            print(f"Total informado pela API: {total_registros}")

            if total_registros > 0:
                total_paginas = math.ceil(
                    total_registros / ITENS_POR_PAGINA
                )
                print(f"Total estimado de páginas: {total_paginas}")

        # Aplica os filtros localmente.
        for item in itens:
            if item_eh_compativel(item):
                publicacoes_encontradas.append(item)

                print()
                print("✅ PUBLICAÇÃO COMPATÍVEL ENCONTRADA")
                print(f"Processo: {obter_numero_processo(item)}")
                print(f"Órgão: {item.get('nomeOrgao', 'Não informado')}")
                print(
                    "Data: "
                    f"{item.get('dataDisponibilizacao', item.get('datadisponibilizacao', 'Não informada'))}"
                )
                print()

        print(
            "Publicações compatíveis encontradas até agora: "
            f"{len(publicacoes_encontradas)}"
        )

        # Critérios seguros de encerramento da paginação.
        if not itens:
            break

        if len(itens) < ITENS_POR_PAGINA:
            break

        if total_paginas is not None and pagina >= total_paginas:
            break

        pagina += 1

        if PAUSA_ENTRE_PAGINAS:
            time.sleep(PAUSA_ENTRE_PAGINAS)

    return publicacoes_encontradas


# ============================================================
# EXIBIÇÃO DO RESULTADO
# ============================================================

def exibir_resultados(publicacoes):
    print()
    print("=" * 70)
    print(f"PUBLICAÇÕES ENCONTRADAS: {len(publicacoes)}")
    print("=" * 70)

    if not publicacoes:
        print()
        print("Nenhuma publicação correspondente aos filtros foi encontrada.")
        print("=" * 70)
        return

    for indice, item in enumerate(publicacoes, start=1):
        processo = obter_numero_processo(item)
        orgao = item.get("nomeOrgao", "Não informado")
        data = (
            item.get("dataDisponibilizacao")
            or item.get("datadisponibilizacao")
            or "Não informada"
        )
        tipo = item.get("tipoComunicacao", "Não informado")
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

        # Faz o GitHub Actions marcar a execução como erro.
        raise
