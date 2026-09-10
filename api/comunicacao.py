from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


API_PJE = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):

        # Pega os parâmetros recebidos pelo proxy
        partes = urlsplit(self.path)
        query = partes.query

        # Monta a URL oficial do Comunica PJe
        url = API_PJE

        if query:
            url += "?" + query

        print(f"Consultando Comunica PJe: {url}")

        requisicao = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "monitor-trt2/1.0",
            },
            method="GET",
        )

        try:
            with urlopen(requisicao, timeout=60) as resposta:

                conteudo = resposta.read()

                self.send_response(resposta.status)
                self.send_header(
                    "Content-Type",
                    resposta.headers.get(
                        "Content-Type",
                        "application/json; charset=utf-8",
                    ),
                )
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                self.wfile.write(conteudo)

        except HTTPError as erro:

            conteudo = erro.read()

            self.send_response(erro.code)
            self.send_header(
                "Content-Type",
                erro.headers.get(
                    "Content-Type",
                    "application/json; charset=utf-8",
                ),
            )
            self.end_headers()

            self.wfile.write(conteudo)

        except URLError as erro:

            mensagem = (
                '{"erro": "Falha ao acessar o Comunica PJe", '
                f'"detalhes": "{str(erro.reason)}"'
                "}"
            )

            self.send_response(502)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.end_headers()

            self.wfile.write(mensagem.encode("utf-8"))

        except Exception as erro:

            mensagem = (
                '{"erro": "Erro interno do proxy", '
                f'"detalhes": "{str(erro)}"'
                "}"
            )

            self.send_response(500)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.end_headers()

            self.wfile.write(mensagem.encode("utf-8"))
