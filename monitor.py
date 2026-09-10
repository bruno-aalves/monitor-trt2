import requests
import unicodedata
from datetime import datetime

API_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"

TRIBUNAL = "TRT2"

VARAS = [
    "1ª Vara do Trabalho de Mogi das Cruzes",
    "2ª Vara do Trabalho de Mogi das Cruzes",
    "3ª Vara do Trabalho de Mogi das Cruzes",
    "4ª Vara do Trabalho de Mogi das Cruzes",
]


def normalizar(texto):
    if not texto:
        return ""

    texto = texto.lower()
    texto = unicodedata.normalize("NFD", texto)

    return "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )


def pertence_a_vara(nome_orgao):
    orgao_normalizado = normalizar(nome_orgao)

    for vara in VARAS:
        if normalizar(vara) in orgao_normalizado:
            return True

    return False


def contem_distribuido(texto):
    return "distribuido" in normalizar(texto)


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

        print(f"Consultando página {pagina}...")

        resposta = requests.get(
            API_URL,
            params=parametros,
            headers={"Accept": "application/json"},
            timeout=30,
        )

        resposta.raise_for_status()

        dados = resposta.json()

        itens = dados.get("items", [])

        if not itens:
            break

        for item in itens:

            orgao = item.get("nomeOrgao", "")
            texto = item.get("texto", "")

            if not pertence_a_vara(orgao):
                continue

            if not contem_distribuido(texto):
                continue

            resultados.append({
                "processo": item.get("numeroprocessocommascara"),
                "orgao": orgao,
                "data": item.get("datadisponibilizacao"),
                "tipo": item.get("tipoComunicacao"),
                "texto": texto,
                "link": item.get("link"),
                "hash": item.get("hash"),
            })

        total = dados.get("count", 0)

        if pagina * 50 >= total:
            break

        pagina += 1

    return resultados


if __name__ == "__main__":

    publicacoes = consultar_publicacoes()

    print()
    print("=" * 70)
    print(f"PUBLICAÇÕES ENCONTRADAS: {len(publicacoes)}")
    print("=" * 70)

    for publicacao in publicacoes:

        print()
        print(f"Processo: {publicacao['processo']}")
        print(f"Órgão: {publicacao['orgao']}")
        print(f"Data: {publicacao['data']}")
        print(f"Tipo: {publicacao['tipo']}")
        print()
        print("TEOR:")
        print(publicacao["texto"])
        print()
        print("-" * 70)
