import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl, urlencode

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Category Explorer", page_icon="🧭", layout="wide")
st.title("🧭 Category Explorer")
st.caption("Descobre páginas de categorias de lojas online e exporta a estrutura para CSV.")

with st.sidebar:
    st.header("Configuração")
    base_url = st.text_input("URL da loja", "https://www.zalando.pt/").strip()
    max_pages = st.slider("Máximo de páginas", 10, 500, 100, step=10)
    max_depth = st.slider("Profundidade de navegação", 1, 6, 3)
    delay = st.slider("Pausa entre pedidos (segundos)", 0.2, 2.0, 0.5, step=0.1)
    use_sitemaps = st.checkbox("Descobrir URLs via sitemap", value=True)
    follow_all = st.checkbox("Seguir todos os links internos", value=False)
    run = st.button("▶ Iniciar extração", type="primary", use_container_width=True)

st.info("A aplicação recolhe apenas páginas publicamente acessíveis. Não contorna CAPTCHA, login ou bloqueios. Sites com conteúdo carregado apenas por JavaScript podem produzir resultados incompletos.")

CATEGORY_HINTS = re.compile(
    r"(catalog|mulher|homem|crianca|criança|roupa|sapatos|calcado|calçado|desporto|"
    r"acessorios|acessórios|casa|lifestyle|beleza|designer|vestidos|calcas|calças|"
    r"sapatilhas|botas|saias|malhas|interior|kids|women|men|shoes|clothing|category|categories)",
    re.I,
)
SKIP_PARTS = ("/login", "/checkout", "/cart", "/help", "/faq", "/legal", "/privacy",
              "/account", "/customer", "/order", "/newsletter")
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|webp|svg|pdf|zip|css|js|woff2?)$", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CategoryExplorer/1.0; +https://github.com/weslleyreimberg/category-explorer)"}

def normalize_url(href, current, hosts):
    if not href:
        return None
    url = urldefrag(urljoin(current, href)).url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.netloc.lower() not in hosts:
        return None
    if SKIP_EXT.search(p.path) or any(part in p.path.lower() for part in SKIP_PARTS):
        return None
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() in ("catalog", "category", "gender")]
    return p._replace(scheme="https", query=urlencode(query), fragment="").geturl()

def discover_sitemaps(root_url, hosts, session, logs):
    maps = set()
    try:
        response = session.get(urljoin(root_url, "/robots.txt"), timeout=12)
        logs.append(f"robots.txt: HTTP {response.status_code}")
        if response.ok:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    maps.add(line.split(":", 1)[1].strip())
    except requests.RequestException as exc:
        logs.append(f"robots.txt: {type(exc).__name__}: {exc}")
    if not maps:
        maps.update([urljoin(root_url, "/sitemap.xml"), urljoin(root_url, "/sitemap_index.xml")])

    queue, seen_maps, found = deque(maps), set(), set()
    while queue and len(seen_maps) < 25:
        sitemap = queue.popleft()
        if sitemap in seen_maps:
            continue
        seen_maps.add(sitemap)
        try:
            response = session.get(sitemap, timeout=15)
            logs.append(f"Sitemap {sitemap}: HTTP {response.status_code}")
            if not response.ok:
                continue
            root = ET.fromstring(response.content)
            for node in root.iter():
                tag = node.tag.rsplit("}", 1)[-1].lower()
                loc = next((child.text.strip() for child in node
                            if child.tag.rsplit("}", 1)[-1].lower() == "loc" and child.text), None)
                if not loc:
                    continue
                if tag == "sitemap" and urlparse(loc).netloc.lower() in hosts:
                    queue.append(loc)
                elif tag == "url":
                    normalized = normalize_url(loc, root_url, hosts)
                    if normalized and CATEGORY_HINTS.search(urlparse(normalized).path):
                        found.add(normalized)
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            logs.append(f"Sitemap falhou ({sitemap}): {type(exc).__name__}: {exc}")
    logs.append(f"URLs candidatas em sitemaps: {len(found)}")
    return found

def parse_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.find("h1")
    label = h1.get_text(" ", strip=True) if h1 else title or urlparse(page_url).path.strip("/").split("/")[-1]
    crumbs = []
    for selector in ('nav[aria-label*="breadcrumb" i] a',
                     '[data-testid*="breadcrumb" i] a', '[class*="breadcrumb" i] a',
                     '[itemtype*="BreadcrumbList"] [itemprop="name"]'):
        crumbs = [el.get_text(" ", strip=True) for el in soup.select(selector)
                  if el.get_text(" ", strip=True)]
        if len(crumbs) >= 2:
            break
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        text = a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", "")
        in_nav = a.find_parent(["nav", "header"]) is not None or a.find_parent(attrs={"role": "navigation"}) is not None
        links.append({"href": href, "text": text.strip(), "in_nav": in_nav})
    return title, label, crumbs[:6], links

def crawl(site_url, page_limit, depth_limit, pause, sitemap_enabled, follow_all):
    parsed = urlparse(site_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Introduza uma URL válida, incluindo https://")
    hosts = {parsed.netloc.lower()}
    hosts.add(parsed.netloc.lower()[4:] if parsed.netloc.lower().startswith("www.") else "www." + parsed.netloc.lower())
    session = requests.Session()
    session.headers.update(HEADERS)
    logs, rows, failures = [], [], []
    seeds = [site_url]
    if "zalando.pt" in parsed.netloc.lower():
        seeds += [f"https://www.zalando.pt/{p}/" for p in (
            "catalogo-mulher", "catalogo-homem", "catalogo-crianca", "casa-lifestyle",
            "mulher-roupa", "mulher-sapatos", "homem-roupa", "homem-sapatos",
            "crianca-roupa", "crianca-sapatos")]
    queue = deque((u, 0, "Página inicial/semente") for u in seeds)
    if sitemap_enabled:
        for u in discover_sitemaps(site_url, hosts, session, logs):
            queue.append((u, 1, "Sitemap"))

    seen = set()
    progress = st.progress(0)
    status = st.empty()
    while queue and len(seen) < page_limit:
        url, depth, source = queue.popleft()
        url = normalize_url(url, site_url, hosts)
        if not url or url in seen or depth > depth_limit:
            continue
        seen.add(url)
        status.text(f"A analisar {len(seen)}/{page_limit}: {url}")
        try:
            response = session.get(url, timeout=20, allow_redirects=True)
            final_url = normalize_url(response.url, site_url, hosts) or url
            logs.append(f"HTTP {response.status_code} {final_url}")
            if response.status_code >= 400:
                failures.append(f"HTTP {response.status_code} {final_url}")
                continue
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower() and response.text.lstrip()[:20].lower().startswith("<?xml"):
                continue
            title, label, crumbs, links = parse_page(response.text, final_url)
            levels = crumbs[:] if crumbs else ([label] if label else [])
            if label and (not levels or levels[-1].casefold() != label.casefold()):
                levels.append(label)
            levels = levels[-6:]
            row = {f"Nível {i+1}": levels[i] if i < len(levels) else "" for i in range(6)}
            row.update({"Categoria": label, "Caminho completo": " > ".join(levels),
                        "URL": final_url, "HTTP": response.status_code,
                        "Fonte da descoberta": source,
                        "Breadcrumb disponível": "sim" if crumbs else "não",
                        "Título SEO": title})
            rows.append(row)
            if depth < depth_limit:
                candidates = []
                for link in links:
                    u = normalize_url(link["href"], final_url, hosts)
                    if not u or u in seen or u == final_url:
                        continue
                    path = urlparse(u).path
                    if follow_all or CATEGORY_HINTS.search(path) or (link["in_nav"] and link["text"]):
                        if len(path.strip("/").split("/")) <= 8:
                            candidates.append((u, link["text"] or "Link interno", link["in_nav"]))
                candidates.sort(key=lambda item: (not item[2], not bool(CATEGORY_HINTS.search(urlparse(item[0]).path))))
                added = set()
                for u, text, _ in candidates:
                    if u not in added:
                        queue.append((u, depth + 1, text))
                        added.add(u)
            time.sleep(pause)
        except requests.RequestException as exc:
            failures.append(f"ERRO {url}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            failures.append(f"ERRO {url}: {type(exc).__name__}: {exc}")
        progress.progress(min(1.0, len(seen) / page_limit))
    logs += [f"URLs visitadas: {len(seen)}", f"Registos capturados: {len(rows)}", f"Falhas: {len(failures)}"]
    logs.extend(failures[:150])
    return pd.DataFrame(rows), logs

if "results" not in st.session_state:
    st.session_state.results = None
if "diagnostics" not in st.session_state:
    st.session_state.diagnostics = None

if run:
    try:
        df, logs = crawl(base_url, max_pages, max_depth, delay, use_sitemaps, follow_all)
        st.session_state.results, st.session_state.diagnostics = df, logs
        st.success(f"Extração terminada: {len(df)} páginas capturadas.")
    except Exception as exc:
        st.error(f"A extração falhou: {type(exc).__name__}: {exc}")
        st.session_state.diagnostics = [f"{type(exc).__name__}: {exc}"]

if st.session_state.results is not None:
    df = st.session_state.results
    st.subheader("Resultados")
    a, b, c = st.columns(3)
    a.metric("Páginas capturadas", len(df))
    b.metric("Com breadcrumb", int((df["Breadcrumb disponível"] == "sim").sum()) if not df.empty else 0)
    c.metric("URLs únicas", df["URL"].nunique() if not df.empty else 0)
    search = st.text_input("Filtrar resultados", placeholder="Ex.: sapatilhas, mulher, desporto...")
    view = df
    if search and not df.empty:
        view = df[df.astype(str).apply(lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)]
    st.dataframe(view, use_container_width=True, hide_index=True)
    st.download_button("⬇️ Descarregar CSV", data=df.to_csv(index=False, sep=";", encoding="utf-8-sig"),
                       file_name="categorias_ecommerce.csv", mime="text/csv", type="primary")
    st.download_button("⬇️ Descarregar diagnóstico", data="\n".join(st.session_state.diagnostics or []),
                       file_name="diagnostico_crawler.txt", mime="text/plain")

if st.session_state.diagnostics:
    with st.expander("Diagnóstico da execução", expanded=st.session_state.results is None):
        st.code("\n".join(st.session_state.diagnostics))
st.markdown("---")
st.caption("A completude depende dos sitemaps, links públicos, HTML devolvido pelo site e limites escolhidos. Respeite os termos e limites de acesso da loja.")
