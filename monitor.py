import os
import requests
import unicodedata
from datetime import datetime

# ============================================================
# CONFIGURAÇÕES
# ============================================================

# A URL será lida da variável de ambiente PJE_API_URL.
# Se ela não existir, usa diretamente a API oficial.
API_URL = os.getenv(
    "PJE_API_URL",
    "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
)

TRIBUNAL = "TRT2"

VARAS = [
    "1ª Vara do Trabalho de Mogi das Cruzes",
    "2ª Vara do Trabalho de Mogi das Cruzes",
    "3ª Vara do Trabalho de Mogi das Cruzes",
    "4ª Vara do Trabalho de Mogi das Cruzes",
]


# ============================================================
# NORMALIZAÇÃO DE TEXTO
# ============================================================

def normalizar(texto):
    if not texto:
        return ""

    texto = str(texto).lower()
    texto = unicodedata.normalize("NFD", texto)

    return "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )


# ============================================================
# FILTRO DAS VARAS
# ============================================================

def pertence_a_vara(nome_orgao):
    orgao_normalizado = normalizar(nome_orgao)

    for vara in VARAS:
        if normalizar(vara) in orgao_normalizado:
            return True

    return False


# ============================================================
# FILTRO "DISTRIBUÍDO"
# ============================================================

def contem_distribuido(texto):
    return "distribuido" in normalizar(texto)


# ============================================================
# CONSULTA AO COMUNICA PJE
# ============================================================

def consultar_publicacoes():
    hoje = datetime.now().strftime("%Y-%m-%d")

    pagina = 1
    resultados = []

    while True:

        parametros = {
            "siglaTribunal": TRIBUNAL,
            "dataDisponibilizacaoInicio": hoje,
            "dataDisponibilizacaoFim": hoje,
            "pagina": pagina,
            "itensPorPagina": 50,
        }

        print()
        print("=" * 70)
        print(f"Consultando página {pagina}...")
        print(f"API utilizada: {API_URL}")
        print("=" * 70)

        try:
            resposta = requests.get(
                API_URL,
                params=parametros,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "monitor-trt2/1.0",
                },
                timeout=60,
            )

        except requests.exceptions.RequestException as erro:
            print()
            print("ERRO DE CONEXÃO COM O COMUNICA PJE")
            print(f"Detalhes: {erro}")
            raise

        # --------------------------------------------------------
        # TRATAMENTO ESPECÍFICO DO ERRO 403
        # --------------------------------------------------------

        if resposta.status_code == 403:
            print()
            print("ERRO 403 - ACESSO NEGADO")
            print()
            print(
                "A API do Comunica PJe recusou a conexão."
            )
            print(
                "Se estiver executando pelo GitHub Actions, "
                "configure PJE_API_URL com o endereço do proxy brasileiro."
            )
            print()

            raise RuntimeError(
                "Comunica PJe retornou HTTP 403. "
                "Configure o proxy brasileiro em PJE_API_URL."
            )

        # Outros erros HTTP
        resposta.raise_for_status()

        try:
            dados = resposta.json()

        except ValueError:
            print()
            print("ERRO: A resposta recebida não é um JSON válido.")
            print()
            print("Resposta recebida:")
            print(resposta.text[:1000])
            raise

        itens = dados.get("items", [])

        print(f"Resultados recebidos nesta página: {len(itens)}")

        if not itens:
            break

        for item in itens:

            orgao = (
                item.get("nomeOrgao")
                or item.get("nomeorgao")
                or ""
            )

            texto = (
                item.get("texto")
                or item.get("teorComunicacao")
                or item.get("teor")
                or ""
            )

            # ----------------------------------------------------
            # FILTRO 1: VARA DO TRABALHO DE MOGI DAS CRUZES
            # ----------------------------------------------------

            if not pertence_a_vara(orgao):
                continue

            # ----------------------------------------------------
            # FILTRO 2: TEOR CONTÉM "DISTRIBUÍDO"
            # ----------------------------------------------------

            if not contem_distribuido(texto):
                continue

            resultados.append({
                "processo": (
                    item.get("numeroprocessocommascara")
                    or item.get("numeroProcesso")
                    or item.get("numero_processo")
                    or ""
                ),
                "orgao": orgao,
                "data": (
                    item.get("datadisponibilizacao")
                    or item.get("dataDisponibilizacao")
                    or ""
                ),
                "tipo": (
                    item.get("tipoComunicacao")
                    or item.get("tipocomunicacao")
                    or ""
                ),
                "texto": texto,
                "link": item.get("link", ""),
                "hash": item.get("hash", ""),
            })

        total = dados.get("count", 0)

        print(f"Total informado pela API: {total}")
        print(
            f"Publicações compatíveis encontradas até agora: "
            f"{len(resultados)}"
        )

        if pagina * 50 >= total:
            break

        pagina += 1

    return resultados


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":

    print()
    print("MONITOR TRT2")
    print("Varas do Trabalho de Mogi das Cruzes")
    print("Filtro: teor contém 'DISTRIBUÍDO'")
    print()

    publicacoes = consultar_publicacoes()

    print()
    print("=" * 70)
    print(f"PUBLICAÇÕES ENCONTRADAS: {len(publicacoes)}")
    print("=" * 70)

    if not publicacoes:
        print()
        print("Nenhuma publicação correspondente aos filtros foi encontrada.")

    for publicacao in publicacoes:

        print()
        print("-" * 70)
        print(f"Processo: {publicacao['processo']}")
        print(f"Órgão: {publicacao['orgao']}")
        print(f"Data: {publicacao['data']}")
        print(f"Tipo: {publicacao['tipo']}")

        if publicacao["link"]:
            print(f"Link: {publicacao['link']}")

        print()
        print("TEOR DA COMUNICAÇÃO:")
        print()
        print(publicacao["texto"])
        print()

    print("=" * 70)
```
