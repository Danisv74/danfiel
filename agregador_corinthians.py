#!/usr/bin/env python3
"""DanFiel - agregador de notícias do Corinthians.
Lê feeds RSS, remove duplicatas e gera o site (index.html e páginas auxiliares).
Só usa a biblioteca padrão do Python (3.8+).

Estrutura:
  agregador_corinthians.py   este script
  posts/                     suas opiniões (AAAA-MM-DD-titulo.txt)
  site/                      pasta publicada (as páginas são geradas aqui;
                             cabecalho.jpg, favicon.png e og.jpg ficam aqui)

Uso local:  python agregador_corinthians.py
No GitHub, o arquivo .github/workflows/atualizar.yml roda este script sozinho.
"""
import html
import html.entities
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

GN = "https://news.google.com/rss/search?q={}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

# Fontes DIRETAS (feeds dos próprios sites): o clique abre direto na matéria.
#   nome        = nome exibido no card
#   intro       = mostrar a introdução que vem no feed? (False = só título + link)
#   categorias  = usar as etiquetas do feed para escolher a aba?
# ATENÇÃO: o campo "description" do ge traz a matéria inteira, por isso intro=False.
FONTES_DIRETAS = [
    {"nome": "Central do Timão", "site": "https://centraldotimao.com.br/",
     "url": "https://centraldotimao.com.br/feed",
     "intro": True, "categorias": False},
    {"nome": "Meu Timão", "site": "https://www.meutimao.com.br/",
     "url": "https://www.meutimao.com.br/rss",
     "intro": False, "categorias": True},
    {"nome": "ge", "site": "https://ge.globo.com/futebol/times/corinthians/",
     "url": "https://pox.globo.com/rss/ge/futebol/times/corinthians/",
     "intro": False, "categorias": False},
]

# Feeds do Google News da aba INÍCIO (complemento; o clique passa pelo Google).
FEEDS_GERAIS = [
    GN.format("Corinthians"),
    GN.format("Corinthians+Timão+when:2d"),
]

# Abas: nome -> buscas do Google News (tudo que vem de uma busca entra na aba dela).
# A ordem aqui é a ordem das abas no menu.
ABAS = {
    "Futebol": [GN.format("Corinthians+Brasileirão+jogo")],
    "Feminino": [GN.format("Corinthians+feminino")],
    "Base": [GN.format("Corinthians+(sub-20+OR+sub-17+OR+sub-15)")],
    "Mercado": [GN.format("Corinthians+contratação+reforço+when:30d")],
}
# Ordem de preenchimento: abas específicas primeiro, a genérica (Futebol) por último.
ORDEM_PREENCHIMENTO = ["Feminino", "Base", "Mercado", "Futebol"]

# Etiquetas (categorias) dos feeds diretos -> aba. Só vale para fontes com categorias=True.
# Procura estes trechos (em minúsculas) nas etiquetas da notícia.
CATEGORIAS_POR_ABA = {
    "Feminino": ["feminin"],
    "Base": ["sub-20", "sub-17", "sub-15", "base do corinthians"],
    "Mercado": ["mercado da bola", "contratações do corinthians"],
    "Futebol": ["campeonato brasileiro", "copa do brasil", "copa sul-americana",
                "libertadores da américa", "escalação do corinthians",
                "próximos jogos do corinthians", "resultados do corinthians",
                "treino do corinthians"],
}

MAX_DIAS_ABA = 30       # abas ignoram notícias mais velhas que isso
DIAS_FEED_PARADO = 3    # avisa no log se uma fonte direta ficar tantos dias sem notícia
N_DESTAQUES = 4         # 1 principal + 3 na coluna Destaques
N_ULTIMAS = 12          # cards "últimas notícias" no Início
MAX_POR_ABA = 20
BR = timezone(timedelta(hours=-3))  # horário de Brasília (o Brasil não tem horário de verão)
RAIZ = os.path.dirname(os.path.abspath(__file__))
PASTA_POSTS = os.path.join(RAIZ, "posts")
SITE_DIR = os.path.join(RAIZ, "site")
MAX_POSTS_TOPO = 3

# --- Divulgação e contato ---
SITE_URL = "https://danfiel.vercel.app"      # troque se um dia tiver domínio próprio
CONTATO_EMAIL = "contato.danfiel@hotmail.com"  # e-mail público para contato, correções e pedidos de retirada
GOOGLE_VERIFICACAO = ""                      # código do Google Search Console (meta tag), se tiver
DESCRICAO = ("Notícias do Corinthians em um só lugar: títulos, fontes e links para a "
             "matéria original, mais a opinião do Dan.")


AGENTE_PADRAO = "Mozilla/5.0 (danfiel)"                    # buscas do Google News
AGENTE_ROBO = f"DanFielBot/1.0 (+{SITE_URL}/fontes.html)"  # feeds diretos: o robô se identifica


def baixar(url, agente=None):
    url = urllib.parse.quote(url, safe=":/?&=+%@,;~-_.()")  # converte acentos (ã -> %C3%A3)
    req = urllib.request.Request(url, headers={"User-Agent": agente or AGENTE_PADRAO})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def introducao(bruto, titulo, limite=160):
    """Resumo do feed sem HTML, cortado em ~160 caracteres."""
    texto = html.unescape(re.sub(r"<[^>]+>", " ", bruto or ""))
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) < 40 or texto.lower().startswith(titulo.lower()[:30]):
        return ""  # Google News só repete título + fonte
    if len(texto) > limite:
        texto = texto[:limite].rsplit(" ", 1)[0] + "…"
    return texto


def limpar_titulo(t):
    return re.sub(r"\s+", " ", html.unescape(t or "")).strip()


def chave_titulo(t):
    """Chave para detectar a mesma notícia repetida (ignora pontuação e maiúsculas)."""
    return re.sub(r"\W+", " ", t.lower()).strip()[:70]


def aba_pelas_categorias(etiquetas):
    """Escolhe a aba pela primeira regra que combinar, na ordem de preenchimento."""
    for aba in ORDEM_PREENCHIMENTO:
        if any(p in etiquetas for p in CATEGORIAS_POR_ABA.get(aba, [])):
            return aba
    return None


def limpar_xml(dados):
    """Conserta defeitos comuns de feeds: caracteres inválidos, entidades HTML (&nbsp;...) e & soltos."""
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9_\-]+)[\"']", dados)
    try:
        t = dados.decode(m.group(1).decode() if m else "utf-8", errors="replace")
    except LookupError:
        t = dados.decode("utf-8", errors="replace")
    t = re.sub(r"^\s*<\?xml[^>]*\?>", '<?xml version="1.0" encoding="UTF-8"?>', t)
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)

    def entidade(m):
        nome = m.group(1)
        if nome in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        cp = html.entities.name2codepoint.get(nome)
        return f"&#{cp};" if cp else f"&amp;{nome};"
    t = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", entidade, t)
    t = re.sub(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)", "&amp;", t)
    return t.encode("utf-8")


def itens_do_xml(root):
    return [{"title": it.findtext("title") or "", "link": it.findtext("link") or "",
             "pubDate": it.findtext("pubDate") or "", "source": it.findtext("source") or "",
             "description": it.findtext("description") or "",
             "categorias": [c.text or "" for c in it.findall("category")]}
            for it in root.iter("item")]


def itens_por_regex(texto):
    """Último recurso: pega os campos de cada <item> por busca de texto, sem exigir XML perfeito."""
    itens = []
    for parte in re.split(r"(?=<item[\s>])", texto, flags=re.I):
        if not re.match(r"<item[\s>]", parte, re.I):
            continue
        bloco = re.split(r"</item>", parte, maxsplit=1, flags=re.I)[0]  # um item sem fechamento não engole o próximo

        def valor(v):
            v = v.strip()
            cd = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", v, re.S)
            return cd.group(1).strip() if cd else html.unescape(v)

        def campo(nome):
            m = re.search(rf"<{nome}\b[^>]*>(.*?)</{nome}>", bloco, re.S | re.I)
            return valor(m.group(1)) if m else ""
        cats = [valor(c) for c in re.findall(r"<category\b[^>]*>(.*?)</category>", bloco, re.S | re.I)]
        itens.append({"title": campo("title"), "link": campo("link"), "pubDate": campo("pubDate"),
                      "source": campo("source"), "description": campo("description"), "categorias": cats})
    return itens


def ler_xml_tolerante(dados):
    """Devolve (itens, aviso). Tenta o XML como veio; depois limpo; por fim item a item."""
    try:
        return itens_do_xml(ET.fromstring(dados)), None
    except ET.ParseError as e:
        erro = str(e)
    limpo = limpar_xml(dados)
    try:
        return itens_do_xml(ET.fromstring(limpo)), f"XML com erro ({erro}); corrigido automaticamente"
    except ET.ParseError:
        pass
    itens = itens_por_regex(limpo.decode("utf-8", errors="replace"))
    if itens:
        return itens, f"XML com erro ({erro}); lido item a item"
    raise ET.ParseError(erro)


def ler_feed(url, nome=None, usar_intro=True, usar_categorias=False):
    """Lê um feed. Devolve a lista de notícias, ou None se o feed falhou.
    nome=None  -> feed do Google News (o nome da fonte vem dentro de cada item).
    nome="X"   -> feed direto do site X (o robô se identifica como DanFielBot)."""
    google = nome is None
    try:
        brutos, aviso = ler_xml_tolerante(baixar(url, None if google else AGENTE_ROBO))
    except Exception as e:
        print(f"Falha em {url}: {e}")
        return None
    if aviso:
        print(f"Aviso em {url}: {aviso}.")
    itens = []
    for b in brutos:
        titulo = limpar_titulo(b["title"])
        link = b["link"].strip()
        if google:
            fonte = b["source"].strip(" -–—")
            if not fonte and " - " in titulo:
                titulo, fonte = titulo.rsplit(" - ", 1)
                fonte = fonte.strip(" -–—")
            elif " - " in titulo and fonte:
                titulo = titulo.rsplit(" - ", 1)[0]
        else:
            fonte = nome
        try:
            data = parsedate_to_datetime(b["pubDate"])
            if data.tzinfo is None:
                data = data.replace(tzinfo=timezone.utc)
        except Exception:
            data = datetime.now(timezone.utc)
        if titulo and link:
            n = {"titulo": titulo, "link": link, "fonte": fonte or "Fonte", "data": data,
                 "intro": introducao(b["description"], titulo) if usar_intro else ""}
            if usar_categorias:
                n["aba"] = aba_pelas_categorias(",".join(b["categorias"]).lower())
            itens.append(n)
    return itens


# ---------- Painel de saúde das fontes (aparece no log do GitHub) ----------
SAUDE = []


def registrar_saude(rotulo, itens, direta):
    SAUDE.append({"rotulo": rotulo, "direta": direta,
                  "itens": None if itens is None else len(itens),
                  "ultima": max((n["data"] for n in itens), default=None) if itens else None})


def relatorio_saude():
    print("\nSaúde das fontes:")
    agora = datetime.now(timezone.utc)
    for s in SAUDE:
        r = s["rotulo"]
        if s["itens"] is None:
            print(f"  [FALHOU] {r}")
            if s["direta"]:
                print(f"::warning::A fonte '{r}' falhou nesta execução.")
        elif not s["itens"]:
            print(f"  [vazio ] {r}: 0 notícias")
            if s["direta"]:
                print(f"::warning::A fonte '{r}' veio vazia.")
        else:
            horas = (agora - s["ultima"]).total_seconds() / 3600
            quando = f"há {int(horas)}h" if horas < 48 else f"há {int(horas // 24)}d"
            print(f"  [ok    ] {r}: {s['itens']} notícias, última em "
                  f"{s['ultima'].astimezone(BR):%d/%m %H:%M} ({quando})")
            if s["direta"] and horas > DIAS_FEED_PARADO * 24:
                print(f"::warning::A fonte '{r}' está sem notícia nova há {int(horas // 24)} dias.")


def rotulo_google(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [url])[0]
    return "Google News: " + q


def ler_google(urls):
    todos = []
    for u in urls:
        itens = ler_feed(u)
        registrar_saude(rotulo_google(u), itens, direta=False)
        todos += itens or []
    return todos


def coletar():
    """Devolve (noticias_do_inicio, {aba: noticias}) sem nenhuma repetição."""
    vistos = set()

    def novo(n):
        chave = chave_titulo(n["titulo"])
        if chave in vistos:
            return False
        vistos.add(chave)
        return True

    def mais_recentes(lista):
        return sorted(lista, key=lambda n: n["data"], reverse=True)

    diretas = []
    for f in FONTES_DIRETAS:
        itens = ler_feed(f["url"], nome=f["nome"], usar_intro=f.get("intro", True),
                         usar_categorias=f.get("categorias", False))
        registrar_saude(f["nome"], itens, direta=True)
        diretas += itens or []
    por_chave = {chave_titulo(n["titulo"]): n for n in diretas}

    def preferir_direta(lista):
        """Se a mesma notícia veio do Google e de um feed direto, fica a versão direta."""
        return [por_chave.get(chave_titulo(n["titulo"]), n) for n in lista]

    inicio = []
    for n in mais_recentes(diretas + preferir_direta(ler_google(FEEDS_GERAIS))):
        if novo(n):
            inicio.append(n)
        if len(inicio) >= N_DESTAQUES + N_ULTIMAS:
            break

    agora = datetime.now(timezone.utc)
    por_aba = {}
    for nome in ORDEM_PREENCHIMENTO:
        candidatas = [n for n in diretas if n.get("aba") == nome]
        candidatas += preferir_direta(ler_google(ABAS[nome]))
        lista = []
        for n in mais_recentes(candidatas):
            if (agora - n["data"]).days > MAX_DIAS_ABA:
                continue
            if novo(n):
                lista.append(n)
            if len(lista) >= MAX_POR_ABA:
                break
        por_aba[nome] = lista
    return inicio, por_aba


def tempo(dt):
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} min atrás"
    if s < 86400:
        return f"{int(s // 3600)}h atrás"
    return f"{int(s // 86400)}d atrás"


def data_do_post(caminho, nome):
    """Data do post: 1) início do nome do arquivo (AAAA-MM-DD-...), 2) data do último
    commit no Git, 3) data de modificação do arquivo."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", nome)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=BR)
        except ValueError:
            pass
    try:
        saida = subprocess.run(["git", "log", "-1", "--format=%cI", "--", caminho],
                               capture_output=True, text=True, timeout=10,
                               cwd=os.path.dirname(caminho)).stdout.strip()
        if saida:
            return datetime.fromisoformat(saida)
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(caminho), BR)


def slugify(nome):
    """2026-11-17-eleição.txt -> 2026-11-17-eleicao (só letras, números e hífens)."""
    base = os.path.splitext(nome)[0]
    ascii_ = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_).strip("-").lower() or "post"


def carregar_posts():
    """Lê posts/*.txt: 1ª linha = título, resto = texto. Todos, do mais novo ao mais antigo."""
    posts, slugs = [], set()
    if not os.path.isdir(PASTA_POSTS):
        return posts
    for nome in sorted(os.listdir(PASTA_POSTS)):
        if not nome.lower().endswith(".txt"):
            continue
        caminho = os.path.join(PASTA_POSTS, nome)
        try:
            with open(caminho, encoding="utf-8") as f:
                linhas = f.read().strip().splitlines()
        except Exception as e:
            print(f"Não consegui ler {nome}: {e}")
            continue
        if not linhas:
            continue
        slug = base = slugify(nome)
        n = 2
        while slug in slugs:
            slug = f"{base}-{n}"
            n += 1
        slugs.add(slug)
        posts.append({"titulo": linhas[0].strip(),
                      "corpo": "\n".join(linhas[1:]).strip(),
                      "data": data_do_post(caminho, nome),
                      "slug": slug})
    posts.sort(key=lambda p: p["data"], reverse=True)
    return posts


def esc(s):
    return html.escape(s, quote=True)


def paragrafos_html(corpo):
    return "".join(f"<p>{esc(par).replace(chr(10), '<br>')}</p>"
                   for par in re.split(r"\n\s*\n", corpo) if par.strip())


def previa_texto(corpo, limite=140):
    t = re.sub(r"\s+", " ", corpo)
    return t if len(t) <= limite else t[:limite].rsplit(" ", 1)[0] + "…"


def html_opinioes(posts):
    """Seção 'Opinião do Dan' da home: os mais recentes, com link para a página própria."""
    if not posts:
        return ""
    cards = []
    for p in posts[:MAX_POSTS_TOPO]:
        cards.append(
            f'<details class="post"><summary><span class="pd">{p["data"]:%d/%m/%Y}</span>'
            f'<span class="pt">{esc(p["titulo"])}</span>'
            f'<span class="prev">{esc(previa_texto(p["corpo"]))}</span>'
            f'<span class="mais">Ler opinião completa →</span></summary>'
            f'<div class="corpo">{paragrafos_html(p["corpo"])}'
            f'<p class="perma"><a href="opiniao/{p["slug"]}.html">Abrir em página própria (para compartilhar)</a></p>'
            f'</div></details>')
    mais = ('<p><a class="mais-op" href="opinioes.html">Ver todas as opiniões →</a></p>'
            if len(posts) > MAX_POSTS_TOPO else "")
    return ('<section id="opinioes"><h3>Opinião do Dan</h3><div class="posts">'
            + "".join(cards) + "</div>" + mais + "</section>")


# ---------- Prévia de compartilhamento, rodapé e páginas auxiliares ----------
def meta_head(titulo, descricao, url, tipo="website"):
    """Etiquetas que definem título, descrição e imagem quando o link é compartilhado."""
    t, d, u = esc(titulo), esc(descricao), esc(url)
    img = esc(SITE_URL + "/og.jpg")
    ver = (f'<meta name="google-site-verification" content="{esc(GOOGLE_VERIFICACAO)}">\n'
           if GOOGLE_VERIFICACAO else "")
    return (f'{ver}<meta name="description" content="{d}">\n'
            f'<link rel="canonical" href="{u}">\n'
            f'<meta property="og:type" content="{tipo}">\n'
            f'<meta property="og:site_name" content="DanFiel">\n'
            f'<meta property="og:locale" content="pt_BR">\n'
            f'<meta property="og:title" content="{t}">\n'
            f'<meta property="og:description" content="{d}">\n'
            f'<meta property="og:url" content="{u}">\n'
            f'<meta property="og:image" content="{img}">\n'
            f'<meta property="og:image:width" content="1200">\n'
            f'<meta property="og:image:height" content="630">\n'
            f'<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{t}">\n'
            f'<meta name="twitter:description" content="{d}">\n'
            f'<meta name="twitter:image" content="{img}">')


def rodape():
    return ('<footer>DanFiel é um site independente, sem ligação oficial com o Sport Club '
            'Corinthians Paulista.<br>Exibimos apenas títulos e links; o conteúdo pertence aos '
            'respectivos veículos. · <a href="/fontes.html">Fontes e contato</a></footer>')


PAGINA_INTERNA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITULO__</title>
<link rel="icon" type="image/png" href="__RAIZ__favicon.png">
__META__
<style>__ESTILO__</style></head><body>
<header><a href="/"><img src="__RAIZ__cabecalho.jpg" alt="DanFiel - Corinthians Sempre"></a></header>
<nav><div class="navin"><a href="/">← Início</a><a href="/opinioes.html">Opiniões</a><a href="/fontes.html">Fontes e contato</a></div></nav>
<main class="doc">__CONTEUDO__</main>
__RODAPE__
</body></html>"""


def pagina_interna(titulo, descricao, caminho, conteudo, tipo="website", raiz=""):
    """raiz = caminho relativo até a pasta do site ('' na raiz, '../' dentro de opiniao/)."""
    return (PAGINA_INTERNA.replace("__RAIZ__", raiz).replace("__TITULO__", esc(titulo))
            .replace("__META__", meta_head(titulo, descricao, SITE_URL + caminho, tipo))
            .replace("__ESTILO__", ESTILO).replace("__CONTEUDO__", conteudo)
            .replace("__RODAPE__", rodape()))


def pagina_fontes():
    fontes = "".join(f'<li><a href="{esc(f["site"])}" target="_blank" rel="noopener">{esc(f["nome"])}</a></li>'
                     for f in FONTES_DIRETAS)
    if CONTATO_EMAIL:
        contato = (f'<div class="contato"><p>Para falar com o DanFiel, enviar uma sugestão ou pedir a correção ou '
                   f'a retirada de algum conteúdo exibido aqui, escreva para '
                   f'<a href="mailto:{esc(CONTATO_EMAIL)}">{esc(CONTATO_EMAIL)}</a>. '
                   f'Se for um pedido de correção ou retirada, informe o endereço da página e o que deve ser feito. '
                   f'Os pedidos são analisados e atendidos o quanto antes.</p>'
                   f'<p class="obs">Obs.: o DanFiel não tem ligação com o clube. Para ingressos, sócio-torcedor '
                   f'e outros assuntos do Corinthians, procure o site oficial do clube.</p></div>')
    else:
        contato = "<p>O contato por e-mail será divulgado em breve.</p>"
    corpo = (
        "<h1>Fontes e contato</h1>"
        "<h2>Sobre o DanFiel</h2>"
        "<p>O DanFiel é um site independente, feito por um torcedor. Não tem ligação oficial com o "
        "Sport Club Corinthians Paulista. O nome e o escudo do clube pertencem ao clube, e o site "
        "não usa o escudo.</p>"
        "<h2>Como funciona</h2>"
        "<p>O site reúne títulos de notícias sobre o Corinthians e leva você, com um clique, à matéria "
        "original no site de quem a publicou. Mostramos apenas o título, o nome da fonte, o horário e, "
        "em alguns casos, uma linha curta de introdução. Não reproduzimos matérias e não usamos fotos "
        "de terceiros.</p>"
        "<h2>De onde vêm as notícias</h2>"
        f"<ul>{fontes}</ul>"
        "<p>Também usamos buscas no Google News. Nesses cards, o nome do veículo aparece em cada "
        "notícia, e o link passa antes pelo Google.</p>"
        "<p>Para ler os feeds das fontes acima, o site usa um robô chamado DanFielBot, que se "
        "identifica com o endereço desta página.</p>"
        "<h2>Opinião do Dan</h2>"
        "<p>Os textos dessa seção são autorais e expressam a opinião do autor, não a do clube nem a "
        "dos veículos citados.</p>"
        "<h2>Contato, correções e pedidos de retirada</h2>" + contato)
    return pagina_interna("Fontes e contato | DanFiel",
                          "De onde vêm as notícias do DanFiel e como falar com o site.",
                          "/fontes.html", corpo)


def pagina_opiniao(p):
    corpo = (f'<article><p class="pd">{p["data"]:%d/%m/%Y}</p><h1>{esc(p["titulo"])}</h1>'
             f'<p class="autor">Por Dan · DanFiel</p><div class="corpo">{paragrafos_html(p["corpo"])}</div>'
             f'<p><a class="mais-op" href="/opinioes.html">Ver todas as opiniões →</a></p></article>')
    return pagina_interna(f'{p["titulo"]} | Opinião do Dan',
                          previa_texto(p["corpo"], 155) or DESCRICAO,
                          f'/opiniao/{p["slug"]}.html', corpo, tipo="article", raiz="../")


def pagina_opinioes(posts):
    itens = "".join(
        f'<a href="/opiniao/{p["slug"]}.html"><span class="pd">{p["data"]:%d/%m/%Y}</span>'
        f'<span class="pt">{esc(p["titulo"])}</span>'
        f'<span class="prev">{esc(previa_texto(p["corpo"]))}</span></a>' for p in posts)
    corpo = f'<h1>Opinião do Dan</h1><div class="lista-op">{itens}</div>'
    return pagina_interna("Opinião do Dan | DanFiel", "Todas as opiniões do Dan sobre o Corinthians.",
                          "/opinioes.html", corpo)


def sitemap_xml(posts):
    urls = [(SITE_URL + "/", None), (SITE_URL + "/fontes.html", None)]
    if posts:
        urls.append((SITE_URL + "/opinioes.html", None))
        urls += [(f'{SITE_URL}/opiniao/{p["slug"]}.html', f'{p["data"]:%Y-%m-%d}') for p in posts]
    linhas = []
    for u, d in urls:
        extra = f"<lastmod>{d}</lastmod>" if d else ""
        linhas.append(f"  <url><loc>{esc(u)}</loc>{extra}</url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(linhas) + "\n</urlset>\n")


def escrever(caminho, conteudo):
    """Grava só se o conteúdo mudou (evita alterações inúteis no Git)."""
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            if f.read() == conteudo:
                return False
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(conteudo)
    return True


def gerar_paginas_extras(posts):
    """Fontes e contato, opiniões (uma página por texto), sitemap e robots."""
    if not CONTATO_EMAIL:
        print("::warning::CONTATO_EMAIL está vazio: a página 'Fontes e contato' ficou sem e-mail.")
    escrever(os.path.join(SITE_DIR, "fontes.html"), pagina_fontes())
    pasta_op = os.path.join(SITE_DIR, "opiniao")
    slugs = {p["slug"] for p in posts}
    for p in posts:
        escrever(os.path.join(pasta_op, p["slug"] + ".html"), pagina_opiniao(p))
    if os.path.isdir(pasta_op):  # apaga páginas de opiniões que você removeu
        for nome in os.listdir(pasta_op):
            if nome.endswith(".html") and nome[:-5] not in slugs:
                os.remove(os.path.join(pasta_op, nome))
    lista = os.path.join(SITE_DIR, "opinioes.html")
    if posts:
        escrever(lista, pagina_opinioes(posts))
    elif os.path.exists(lista):
        os.remove(lista)
    escrever(os.path.join(SITE_DIR, "sitemap.xml"), sitemap_xml(posts))
    escrever(os.path.join(SITE_DIR, "robots.txt"),
             f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n")


ESTILO = """:root{--bg:#0b0b0e;--card:#15151a;--fg:#f4f4f6;--mut:#9a9aa6;--roxo:#6d28d9;--roxo2:#a78bfa}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{background:#000}
header img{display:block;width:100%;max-width:1400px;max-height:300px;object-fit:cover;object-position:center;margin:0 auto}
nav{background:#000;border-bottom:1px solid #26262e;position:sticky;top:0;z-index:5}
.navin{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:4px;padding:0 8px;overflow-x:auto}
.navin button{background:none;border:0;border-bottom:3px solid transparent;color:var(--fg);font-weight:700;font-size:.85rem;text-transform:uppercase;padding:14px 12px;cursor:pointer;white-space:nowrap}
.navin button.on{border-color:var(--roxo2);color:#fff;background:#14101f}
.navin input{margin-left:auto;min-width:150px;background:#15151a;border:1px solid #2a2a33;border-radius:20px;padding:9px 14px;color:var(--fg);font-size:.85rem}
main{max-width:1100px;margin:0 auto;padding:14px}
.topgrid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
.hero{display:flex;flex-direction:column;justify-content:center;background:linear-gradient(135deg,#1b1233,#0f0f14);border-radius:14px;padding:26px;min-height:260px;text-decoration:none;color:inherit}
.tag{align-self:flex-start;background:#2a1a5e;color:#d8ccff;font-size:.7rem;font-weight:800;padding:5px 10px;border-radius:5px;letter-spacing:.04em}
.hero h2{font-size:clamp(1.4rem,3.4vw,2.2rem);line-height:1.15;margin:14px 0 10px}
.hero p{color:var(--mut);margin:0 0 14px}
.ler{font-weight:800;font-size:.85rem;border-bottom:2px solid var(--roxo2);align-self:flex-start;padding-bottom:2px}
.side{background:var(--card);border-radius:14px;padding:16px}
.side h3{margin:0 0 10px;font-size:1.05rem;border-left:4px solid var(--roxo);padding-left:10px;text-transform:uppercase}
.side a{display:block;text-decoration:none;color:inherit;padding:11px 0;border-top:1px solid #26262e}
.side a:first-of-type{border-top:0}
.side b{display:block;line-height:1.3;font-size:.92rem}
.meta{font-size:.72rem;color:var(--mut);margin-top:4px}
.sec{margin:0 0 10px;font-size:1.05rem;border-left:4px solid var(--roxo);padding-left:10px;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
a.card{display:block;background:var(--card);border-radius:12px;padding:16px;text-decoration:none;color:inherit;border:1px solid transparent}
a.card:hover{border-color:var(--roxo)}
a.card.dup{display:none}
.t{font-weight:700;line-height:1.3}
.i{margin-top:6px;font-size:.85rem;line-height:1.4;color:var(--mut)}
.vazio{color:var(--mut);padding:20px 4px}
.posts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
#opinioes{margin-bottom:18px}
#opinioes h3{margin:0 0 10px;font-size:1.05rem;border-left:4px solid var(--roxo);padding-left:10px;text-transform:uppercase}
.post{background:linear-gradient(135deg,#1b1233,#13131a);border:1px solid #2f2158;border-radius:12px;padding:16px;align-self:start}
.post summary{list-style:none;cursor:pointer;display:block}
.post summary::-webkit-details-marker{display:none}
.pd{display:block;font-size:.7rem;color:var(--roxo2);font-weight:800;letter-spacing:.04em}
.pt{display:block;font-weight:800;font-size:1.1rem;line-height:1.25;margin:6px 0}
.prev{display:block;color:var(--mut);font-size:.88rem;line-height:1.4}
.mais{display:inline-block;margin-top:10px;font-size:.8rem;font-weight:800;border-bottom:2px solid var(--roxo2)}
.post[open] .prev,.post[open] .mais{display:none}
.corpo{margin-top:8px;line-height:1.55;font-size:.95rem}
.corpo p{margin:0 0 12px}
footer{max-width:1100px;margin:0 auto;padding:20px 14px 40px;font-size:.75rem;color:var(--mut);text-align:center}
@media(max-width:760px){.topgrid{grid-template-columns:1fr}.hero{min-height:200px;padding:20px}}
.ler-orig{margin-top:8px;font-size:.78rem;font-weight:800;color:var(--roxo2)}
.navin a{color:var(--fg);text-decoration:none;font-weight:700;font-size:.85rem;text-transform:uppercase;padding:14px 12px;white-space:nowrap}
.navin a:hover{color:var(--roxo2)}
.doc{max-width:760px;margin:0 auto;padding:22px 16px 10px;line-height:1.6}
.doc h1{font-size:1.6rem;line-height:1.2;margin:.2em 0 .5em}
.doc h2{font-size:1.05rem;margin:1.6em 0 .4em;border-left:4px solid var(--roxo);padding-left:10px;text-transform:uppercase}
.doc a{color:var(--roxo2)}
.doc ul{padding-left:1.2em}
.autor{color:var(--mut);font-size:.85rem;margin:.2em 0 1em}
.lista-op a{display:block;background:var(--card);border-radius:12px;padding:16px;margin-bottom:10px;text-decoration:none;color:inherit;border:1px solid transparent}
.lista-op a:hover{border-color:var(--roxo)}
.perma{margin:6px 0 0}.perma a{color:var(--roxo2);font-size:.8rem}
.mais-op{display:inline-block;margin-top:10px;font-weight:800;font-size:.85rem;color:var(--roxo2);text-decoration:none}
footer a{color:var(--roxo2)}
.contato{background:var(--card);border:1px solid #2f2158;border-radius:12px;padding:6px 16px}
.obs{color:#ffd24d;font-size:.88rem}"""


PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DanFiel | Notícias do Corinthians</title>
<link rel="icon" type="image/png" href="favicon.png">
__META__
<style>__ESTILO__</style></head><body>
<header><img src="cabecalho.jpg" alt="DanFiel - Corinthians Sempre"></header>
<nav><div class="navin">
<button class="on" data-c="">Início</button>__BOTOES__
<input id="q" type="search" placeholder="Buscar notícias…">
</div></nav>
<main>
<section class="aba" data-aba="">
__OPINIOES__
<div class="topgrid">__TOPO__</div>
<h3 class="sec">Últimas notícias</h3>
<div class="grid">__CARDS_INICIO__</div>
</section>
__ABAS__
<p style="color:var(--mut);font-size:.8rem;margin-top:14px">Atualizado em __ATUALIZADO__</p>
</main>
__RODAPE__
<script>
let aba="",busca="";
function filtrar(){
  const buscando=busca.length>0;
  document.querySelectorAll('.aba').forEach(s=>{
    s.style.display=(buscando||s.dataset.aba===aba)?'':'none';
    s.querySelectorAll('a.card').forEach(c=>{
      const ok=!buscando||c.textContent.toLowerCase().includes(busca);
      c.style.display=(c.classList.contains('dup')?(buscando&&ok):ok)?'':'none';
    });
    s.querySelectorAll('.topgrid,#opinioes,.sec,.vazio').forEach(e=>e.style.display=buscando?'none':'');
  });
}
document.querySelectorAll('.navin button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.navin button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');aba=b.dataset.c;filtrar();
});
document.getElementById('q').oninput=e=>{busca=e.target.value.toLowerCase();filtrar();};
function rel(d){const s=(Date.now()-new Date(d))/1000;
  if(s<60)return'agora';
  if(s<3600)return Math.floor(s/60)+' min atrás';
  if(s<86400)return Math.floor(s/3600)+'h atrás';
  return Math.floor(s/86400)+'d atrás';}
function tempos(){document.querySelectorAll('.tm').forEach(e=>{e.textContent=rel(e.dataset.t)});}
tempos();setInterval(tempos,60000);
filtrar();
</script></body></html>"""


def meta_html(n):
    """Fonte + tempo. O tempo é recalculado no navegador (data-t) para nunca ficar velho."""
    return (f'Fonte: {esc(n["fonte"])} · <span class="tm" data-t="{n["data"].isoformat()}">'
            f'{tempo(n["data"])}</span>')


def card(n, dup=False):
    intro = f'<div class="i">{esc(n["intro"])}</div>' if n["intro"] else ""
    cls = "card dup" if dup else "card"
    return (f'<a class="{cls}" href="{esc(n["link"])}" target="_blank" rel="noopener">'
            f'<div class="t">{esc(n["titulo"])}</div>{intro}'
            f'<div class="meta">{meta_html(n)}</div>'
            f'<div class="ler-orig">Ler matéria original →</div></a>')


def gerar(inicio, por_aba, posts):
    topo = ""
    if inicio:
        h = inicio[0]
        intro = f'<p>{esc(h["intro"])}</p>' if h["intro"] else f'<p>{meta_html(h)}</p>'
        topo = (f'<a class="hero" href="{esc(h["link"])}" target="_blank" rel="noopener">'
                f'<span class="tag">ÚLTIMAS NOTÍCIAS</span><h2>{esc(h["titulo"])}</h2>{intro}'
                f'<span class="ler">LER MATÉRIA ORIGINAL →</span></a>')
        lado = "".join(
            f'<a href="{esc(n["link"])}" target="_blank" rel="noopener"><b>{esc(n["titulo"])}</b>'
            f'<div class="meta">{meta_html(n)}</div></a>'
            for n in inicio[1:N_DESTAQUES])
        topo += f'<div class="side"><h3>Destaques</h3>{lado}</div>'
    # As notícias do topo entram como cards escondidos, só para a busca encontrá-las.
    cards_inicio = "\n".join(
        [card(n, dup=True) for n in inicio[:N_DESTAQUES]] +
        [card(n) for n in inicio[N_DESTAQUES:]]) or '<p class="vazio">Nenhuma notícia encontrada.</p>'

    botoes = "".join(f'<button data-c="{esc(nome)}">{esc(nome)}</button>' for nome in ABAS)
    abas_html = ""
    for nome in ABAS:
        lista = por_aba.get(nome, [])
        corpo = ("\n".join(card(n) for n in lista) if lista else
                 '<p class="vazio">Sem notícias novas nesta aba agora. Volte mais tarde.</p>')
        abas_html += (f'<section class="aba" data-aba="{esc(nome)}" style="display:none">'
                      f'<h3 class="sec">{esc(nome)}</h3><div class="grid">{corpo}</div></section>\n')

    agora = datetime.now(BR).strftime("%d/%m/%Y %H:%M")
    meta = meta_head("DanFiel | Notícias do Corinthians", DESCRICAO, SITE_URL + "/")
    return (PAGINA.replace("__META__", meta).replace("__ESTILO__", ESTILO)
            .replace("__RODAPE__", rodape()).replace("__BOTOES__", botoes)
            .replace("__OPINIOES__", html_opinioes(posts))
            .replace("__TOPO__", topo).replace("__CARDS_INICIO__", cards_inicio)
            .replace("__ABAS__", abas_html).replace("__ATUALIZADO__", agora))


def normalizar(h):
    """Tira o que muda a cada execução (hora e 'há X min'), para comparar só o conteúdo."""
    h = re.sub(r"Atualizado em [\d/ :]+", "", h)
    return re.sub(r'(<span class="tm"[^>]*>)[^<]*(</span>)', r"\1\2", h)


if __name__ == "__main__":
    inicio, por_aba = coletar()
    relatorio_saude()
    total = len(inicio) + sum(len(v) for v in por_aba.values())
    print(f"{total} notícias coletadas")
    print(f"  Início: {len(inicio)} | " + " | ".join(f"{k}: {len(v)}" for k, v in por_aba.items()))
    if not inicio:
        print("Nenhuma notícia encontrada no Início. NÃO alterei o site (mantive a versão anterior).")
        sys.exit(1)
    posts = carregar_posts()
    novo = gerar(inicio, por_aba, posts)
    os.makedirs(SITE_DIR, exist_ok=True)
    destino = os.path.join(SITE_DIR, "index.html")
    antigo = ""
    if os.path.exists(destino):
        with open(destino, encoding="utf-8") as f:
            antigo = f.read()
    if normalizar(antigo) == normalizar(novo):
        print("Sem novidades: o index.html continua igual.")
    else:
        with open(destino, "w", encoding="utf-8") as f:
            f.write(novo)
        print("index.html atualizado em site/index.html")
    gerar_paginas_extras(posts)
    print(f"Páginas auxiliares geradas ({len(posts)} opinião(ões)).")
