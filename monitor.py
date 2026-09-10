import os
import re
import time
import math
import html
import smtplib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage

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

# Mantemos 5 para equilibrar velocidade e reduzir HTTP 429.
MAX_WORKERS = 5
MAX_TENTATIVAS = 8


# ============================================================
# E-MAIL
# ============================================================
#
# Configure estes Secrets no GitHub:
#
# SMTP_HOST
# SMTP_PORT
# SMTP_USUARIO
# SMTP_SENHA
# EMAIL_DESTINATARIO
#
# Opcional:
# EMAIL_REMETENTE
#
# Exemplos:
# Gmail:
#   SMTP_HOST = smtp.gmail.com
#   SMTP_PORT = 587
#
# Outlook / Microsoft:
#   SMTP_HOST = smtp.office365.com
#   SMTP_PORT = 587
#
# EMAIL_REMETENTE, se não informado, será igual a SMTP_USUARIO.
#

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USUARIO = os.getenv("SMTP_USUARIO", "")
SMTP_SENHA = os.getenv("SMTP_SENHA", "")
EMAIL_DESTINATARIO = os.getenv("EMAIL_DESTINATARIO", "")
EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE", SMTP_USUARIO)


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar(texto):
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

TIPO_COMUNICACAO_ALVO = normalizar("Lista de distribuição")


def obter_numero_processo(item):
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
    texto_normalizado = normalizar(texto)
    return bool(re.search(r"\bdistribuido\b", texto_normalizado))


def item_eh_compativel(item):
    orgao = normalizar(item.get("nomeOrgao"))
    tipo_comunicacao = normalizar(item.get("tipoComunicacao"))

    vara_compativel = any(
        vara_alvo in orgao
        for vara_alvo in VARAS_ALVO_NORMALIZADAS
    )

    if not vara_compativel:
        return False

    if tipo_comunicacao != TIPO_COMUNICACAO_ALVO:
        return False

    return contem_palavra_distribuido(item.get("texto"))


def chave_unica(item):
    """
    Evita duplicidade sem usar somente o link, porque várias distribuições
    podem compartilhar o mesmo link.
    """
    return (
        obter_numero_processo(item),
        normalizar(item.get("nomeOrgao", "")),
        obter_data(item),
        normalizar(item.get("texto", "")),
    )


def formatar_valor(valor):
    """
    Converte valores simples, listas e dicionários em texto legível.
    """
    if valor is None:
        return ""

    if isinstance(valor, str):
        return valor.strip()

    if isinstance(valor, (int, float, bool)):
        return str(valor)

    if isinstance(valor, list):
        partes = []
        for item in valor:
            texto = formatar_valor(item)
            if texto:
                partes.append(texto)
        return "; ".join(partes)

    if isinstance(valor, dict):
        partes = []
        for chave, conteudo in valor.items():
            texto = formatar_valor(conteudo)
            if texto:
                partes.append(f"{chave}: {texto}")
        return "; ".join(partes)

    return str(valor)


def procurar_campos(item, palavras_chave):
    """
    Procura valores em campos cujo nome contenha uma das palavras-chave.
    Serve para aproveitar pequenas variações do JSON da API sem quebrar
    o monitor.
    """
    encontrados = []

    def visitar(obj):
        if isinstance(obj, dict):
            for chave, valor in obj.items():
                chave_norm = normalizar(chave)

                if any(
                    palavra in chave_norm
                    for palavra in palavras_chave
                ):
                    texto = formatar_valor(valor)
                    if texto and texto not in encontrados:
                        encontrados.append(texto)

                if isinstance(valor, (dict, list)):
                    visitar(valor)

        elif isinstance(obj, list):
            for valor in obj:
                visitar(valor)

    visitar(item)
    return encontrados


def obter_meio(item):
    for chave in (
        "meio",
        "meioComunicacao",
        "meio_comunicacao",
    ):
        valor = item.get(chave)
        texto = formatar_valor(valor)
        if texto:
            return texto

    candidatos = procurar_campos(
        item,
        ["meio"]
    )

    return candidatos[0] if candidatos else "Não informado pela API"


def _lista_direta(item, nomes):
    """Procura listas somente nas chaves conhecidas da API."""
    for nome in nomes:
        valor = item.get(nome)
        if isinstance(valor, list):
            return valor
    return []


def _valor_texto(objeto, *chaves):
    if not isinstance(objeto, dict):
        return ""

    for chave in chaves:
        valor = objeto.get(chave)
        if valor is not None and str(valor).strip():
            return str(valor).strip()

    return ""


def obter_partes(item):
    """Somente nome e polo, sem IDs/metadados internos."""
    registros = _lista_direta(
        item,
        ("destinatarios", "partes", "parte", "destinatario"),
    )

    resultado = []
    vistos = set()

    for registro in registros:
        if not isinstance(registro, dict):
            continue

        nome = _valor_texto(registro, "nome", "nomeParte", "nome_parte")
        polo = _valor_texto(registro, "polo", "tipoPolo", "tipo_polo")

        if not nome:
            continue

        chave = (normalizar(nome), normalizar(polo))
        if chave in vistos:
            continue

        vistos.add(chave)
        resultado.append(f"{nome} — Polo {polo}" if polo else nome)

    return resultado if resultado else ["Não informado pela API"]


def obter_advogados(item):
    """Somente nome e OAB/UF, sem IDs, datas ou duplicidades."""
    registros = _lista_direta(
        item,
        (
            "destinatarioadvogados",
            "destinatarioAdvogados",
            "advogados",
            "advogado",
        ),
    )

    resultado = []
    vistos = set()

    for registro in registros:
        if not isinstance(registro, dict):
            continue

        cadastro = registro.get("advogado")
        if not isinstance(cadastro, dict):
            cadastro = registro

        nome = _valor_texto(
            cadastro, "nome", "nomeAdvogado", "nome_advogado"
        )
        numero_oab = _valor_texto(
            cadastro, "numero_oab", "numeroOab", "oab"
        )
        uf_oab = _valor_texto(
            cadastro, "uf_oab", "ufOab", "uf"
        )

        if not nome:
            continue

        chave = (
            normalizar(nome),
            normalizar(numero_oab),
            normalizar(uf_oab),
        )
        if chave in vistos:
            continue

        vistos.add(chave)

        if numero_oab and uf_oab:
            resultado.append(f"{nome} — OAB/{uf_oab} {numero_oab}")
        elif numero_oab:
            resultado.append(f"{nome} — OAB {numero_oab}")
        else:
            resultado.append(nome)

    return resultado if resultado else ["Não informado pela API"]


# ============================================================
# REQUISIÇÕES COM RETRY
# ============================================================

def fazer_requisicao(parametros):
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
    print("Filtro: tipo 'Lista de distribuição' + palavra inteira 'DISTRIBUÍDO'")
    print(f"Data da consulta (São Paulo): {hoje}")
    print(f"Consultas simultâneas: {MAX_WORKERS}")
    print("=" * 70)
    print()

    print("Consultando página 1 para descobrir o total de resultados...")

    _, dados_primeira, itens_primeira = consultar_pagina(1, hoje)

    try:
        total_registros = int(dados_primeira.get("count", 0))
    except (TypeError, ValueError):
        total_registros = 0

    total_paginas = (
        math.ceil(total_registros / ITENS_POR_PAGINA)
        if total_registros > 0
        else 1
    )

    print(f"Total informado pela API: {total_registros}")
    print(f"Total estimado de páginas: {total_paginas}")
    print()

    resultados = []

    for item in itens_primeira:
        if item_eh_compativel(item):
            resultados.append(item)

    print(
        f"Página 1 concluída. "
        f"Compatíveis encontrados: {len(resultados)}"
    )

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
                    _, _, itens = futuro.result()

                except Exception as erro:
                    raise RuntimeError(
                        f"Falha definitiva na página {pagina}: {erro}"
                    ) from erro

                for item in itens:
                    if item_eh_compativel(item):
                        resultados.append(item)

                concluidas += 1

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

    unicos = {}
    for item in resultados:
        chave = chave_unica(item)
        if chave not in unicos:
            unicos[chave] = item

    publicacoes_unicas = list(unicos.values())

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

    return publicacoes_unicas, hoje


# ============================================================
# EXIBIÇÃO NO LOG
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
        tipo = item.get("tipoComunicacao", "Não informado")
        meio = obter_meio(item)
        partes = obter_partes(item)
        advogados = obter_advogados(item)
        link = item.get("link", "")
        texto = item.get("texto", "")

        print()
        print(f"[{indice}] Processo: {processo}")
        print(f"Órgão: {orgao}")
        print(f"Data de disponibilização: {data}")
        print(f"Tipo de comunicação: {tipo}")
        print(f"Meio: {meio}")
        print(f"Parte(s): {' | '.join(partes)}")
        print(f"Advogado(s): {' | '.join(advogados)}")

        if link:
            print(f"Link: {link}")

        print("-" * 70)
        print("Conteúdo:")
        print(texto)
        print("=" * 70)


# ============================================================
# MONTAGEM DO E-MAIL
# ============================================================

def limpar_html_para_texto(texto):
    texto = texto or ""
    texto = re.sub(r"<br\s*/?>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"<[^>]+>", "", texto)
    return html.unescape(texto).strip()


def criar_corpo_texto(publicacoes, hoje):
    linhas = [
        f"Monitor TRT2 - Lista de distribuição - {hoje}",
        "",
        f"Total de publicações encontradas: {len(publicacoes)}",
        "",
    ]

    if not publicacoes:
        linhas.append(
            "Nenhuma publicação correspondente aos filtros foi encontrada."
        )
        return "\n".join(linhas)

    for indice, item in enumerate(publicacoes, start=1):
        processo = obter_numero_processo(item)
        orgao = item.get("nomeOrgao", "Não informado")
        data = obter_data(item)
        tipo = item.get("tipoComunicacao", "Não informado")
        meio = obter_meio(item)
        partes = obter_partes(item)
        advogados = obter_advogados(item)
        link = item.get("link", "")
        conteudo = limpar_html_para_texto(item.get("texto", ""))

        linhas.extend([
            "=" * 70,
            f"{indice}. Processo: {processo}",
            f"Órgão: {orgao}",
            f"Data de disponibilização: {data}",
            f"Tipo de comunicação: {tipo}",
            f"Meio: {meio}",
            f"Parte(s): {' | '.join(partes)}",
            f"Advogado(s): {' | '.join(advogados)}",
        ])

        if link:
            linhas.append(f"Link: {link}")

        linhas.extend([
            "",
            "Conteúdo:",
            conteudo,
            "",
        ])

    return "\n".join(linhas)


def criar_corpo_html(publicacoes, hoje):
    blocos = []

    if not publicacoes:
        blocos.append(
            "<p>Nenhuma publicação correspondente aos filtros foi encontrada.</p>"
        )
    else:
        for indice, item in enumerate(publicacoes, start=1):
            processo = html.escape(obter_numero_processo(item))
            orgao = html.escape(
                str(item.get("nomeOrgao", "Não informado"))
            )
            data = html.escape(obter_data(item))
            tipo = html.escape(
                str(item.get("tipoComunicacao", "Não informado"))
            )
            meio = html.escape(obter_meio(item))
            partes = "<br>".join(
                html.escape(valor) for valor in obter_partes(item)
            )
            advogados = "<br>".join(
                html.escape(valor) for valor in obter_advogados(item)
            )
            link = item.get("link", "")
            conteudo = html.escape(
                limpar_html_para_texto(item.get("texto", ""))
            ).replace("\n", "<br>")

            link_html = ""
            if link:
                link_seguro = html.escape(str(link), quote=True)
                link_html = (
                    f'<p><strong>Link:</strong> '
                    f'<a href="{link_seguro}">{link_seguro}</a></p>'
                )

            blocos.append(f"""
            <div style="margin: 0 0 28px 0; padding: 18px; border: 1px solid #d9d9d9; border-radius: 8px;">
                <p style="margin-top:0;"><strong>{indice}. Processo:</strong> {processo}</p>
                <p><strong>Órgão:</strong> {orgao}</p>
                <p><strong>Data de disponibilização:</strong> {data}</p>
                <p><strong>Tipo de comunicação:</strong> {tipo}</p>
                <p><strong>Meio:</strong> {meio}</p>
                <p><strong>Parte(s):</strong><br>{partes}</p>
                <p><strong>Advogado(s):</strong><br>{advogados}</p>
                {link_html}
                <p><strong>Conteúdo:</strong><br>{conteudo}</p>
            </div>
            """)

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222;">
        <h2>Monitor TRT2 - Lista de distribuição</h2>
        <p><strong>Data da consulta:</strong> {html.escape(hoje)}</p>
        <p><strong>Total de publicações:</strong> {len(publicacoes)}</p>
        {''.join(blocos)}
      </body>
    </html>
    """


# ============================================================
# ENVIO DO E-MAIL
# ============================================================

def validar_configuracao_email():
    faltando = []

    configuracoes = {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_USUARIO": SMTP_USUARIO,
        "SMTP_SENHA": SMTP_SENHA,
        "EMAIL_DESTINATARIO": EMAIL_DESTINATARIO,
        "EMAIL_REMETENTE": EMAIL_REMETENTE,
    }

    for nome, valor in configuracoes.items():
        if not valor:
            faltando.append(nome)

    if faltando:
        raise RuntimeError(
            "Configuração de e-mail incompleta. "
            "Secrets ausentes: " + ", ".join(faltando)
        )


def enviar_email(publicacoes, hoje):
    validar_configuracao_email()

    quantidade = len(publicacoes)

    assunto = (
        f"Monitor TRT2 - {quantidade} "
        f"{'distribuição' if quantidade == 1 else 'distribuições'} "
        f"- {hoje}"
    )

    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = EMAIL_REMETENTE
    mensagem["To"] = EMAIL_DESTINATARIO

    mensagem.set_content(
        criar_corpo_texto(publicacoes, hoje)
    )

    mensagem.add_alternative(
        criar_corpo_html(publicacoes, hoje),
        subtype="html",
    )

    print()
    print("Enviando e-mail...")

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=60,
    ) as servidor:
        servidor.ehlo()
        servidor.starttls()
        servidor.ehlo()
        servidor.login(
            SMTP_USUARIO,
            SMTP_SENHA,
        )
        servidor.send_message(mensagem)

    print(
        f"✅ E-mail enviado para {EMAIL_DESTINATARIO}"
    )


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    try:
        publicacoes, hoje = consultar_publicacoes()
        exibir_resultados(publicacoes)
        enviar_email(publicacoes, hoje)

    except Exception as erro:
        print()
        print("=" * 70)
        print("❌ ERRO NO MONITOR")
        print("=" * 70)
        print(str(erro))
        print("=" * 70)
        raise
