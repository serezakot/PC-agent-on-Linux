#!/usr/bin/env python3
"""Вводишь запрос как в поисковике -> открывается первый сайт в Firefox.
А если написать «сайт [запрос]» — запрос ищется ВНУТРИ этого сайта.

Обычный запрос
--------------
Используется режим Google «Мне повезёт» (btnI=1): Google сам перенаправляет
на первый результат, поэтому запрос мы не разбираем и не исправляем.
Google при таком переходе часто показывает страницу «Redirect Notice»
(google.com/url?q=...). Чтобы её не видеть, скрипт сам спрашивает у Google,
куда он перенаправляет, берёт оттуда адрес сайта и открывает сразу его.
Если это не вышло — открывается обычная ссылка через Google.

Поиск внутри сайта: «название [запрос]»
---------------------------------------
Запрос всегда пишется в квадратных скобках, поэтому угадывать, где кончается
название сайта и начинается запрос, не нужно. Всё, что до первой «[» — название
сайта, всё между первой «[» и последней «]» — запрос.

    саундклауд [9mice]
    ютуб [linkin park]
    hugging face [bert]

Заранее заготовленных адресов поиска НЕТ. Как искать на сайте, скрипт выясняет
сам, из страницы этого сайта:
  1) название сайта отправляется в Google («Мне повезёт») -> получаем страницу сайта;
  2) скачиваем эту страницу (если на ней поиска нет — главную страницу сайта)
     и ищем, как сайт сам описывает свой поиск. Три способа, по убыванию надёжности:
       • OpenSearch  — <link rel="search" type="application/opensearchdescription+xml">:
                       тот самый стандарт, по которому Firefox добавляет «поисковые системы»;
       • SearchAction — разметка schema.org в JSON-LD (поле поиска для Google);
       • форма поиска — обычная <form method="get"> с полем ввода (так устроены
                       WordPress, MediaWiki, Discourse, Яндекс и очень многие другие);
  3) нашли — подставляем запрос в найденный адрес и открываем;
  4) не нашли (сайт целиком на JavaScript, поиск через POST и т. п.) — спрашиваем
     Google «site:домен запрос»;
  5) Google не ответил — открывается обычная ссылка через Google.
Найденное запоминается до конца работы программы, повторный поиск по тому же
сайту идёт без скачивания страницы.

Запуск (в оболочке скобки лучше брать в кавычки):
    python3 gogle_search.py                          # интерактивный режим
    python3 gogle_search.py ютуб                     # обычный запрос
    python3 gogle_search.py "саундклауд [9mice]"     # поиск внутри сайта
    python3 gogle_search.py --dry-run "ютуб [rock]"  # только показать ссылку (без сети)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import (parse_qs, quote, quote_plus, urlencode, urljoin, urlparse,
                          urlsplit, urlunsplit)

SEARCH_URL = "https://www.google.com/search"
EXIT_WORDS = {"exit", "выход"}
LAUNCH_WAIT_SECONDS = 3.0
HTTP_TIMEOUT_SECONDS = 6.0
PAGE_TIMEOUT_SECONDS = 8.0
MAX_PAGE_BYTES = 1_500_000
MAX_REDIRECT_HOPS = 5
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Accept-Language": "ru,en;q=0.8",
    "Cookie": "SOCS=CAI; CONSENT=YES+",
}
# Для чужих сайтов — без cookie, нужной только Google.
PAGE_HEADERS = {
    "User-Agent": REQUEST_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": REQUEST_HEADERS["Accept-Language"],
    "Accept-Encoding": "gzip",
}
_GOOGLE_HOST = re.compile(r"(^|\.)google\.[a-z.]+$")
_STRIP_PREFIX = re.compile(r"^(www|m|mobile)\.")

# Место для запроса в найденном шаблоне адреса (как в стандарте OpenSearch).
TERM = "{searchTerms}"


class BrowserError(RuntimeError):
    """Браузер не удалось запустить."""


def normalize_query(text: str) -> str:
    """Убирает лишние пробелы/табы/неразрывные пробелы. Слова не трогает."""
    return " ".join(text.split())


def build_url(query: str) -> str:
    """Ссылка «Мне повезёт» для запроса. Кодирование в UTF-8 делает urlencode."""
    return SEARCH_URL + "?" + urlencode({"q": normalize_query(query), "btnI": "1"})


# =============================================================================
# Разбор ввода «название [запрос]»
# =============================================================================
def parse_input(text: str) -> tuple[str, str | None]:
    """«название [запрос]» -> (название, запрос).

    Название — всё до первой «[». Запрос — всё между первой «[» и последней «]»
    (вложенные скобки допустимы). Нет закрывающей «]» — запрос берётся до конца
    строки. Текст после последней «]» отбрасывается.
    Нет «[» вообще -> (весь текст, None): это обычный запрос.
    """
    start = text.find("[")
    if start == -1:
        return normalize_query(text), None
    name = normalize_query(text[:start])
    rest = text[start + 1:]
    end = rest.rfind("]")
    if end != -1:
        rest = rest[:end]
    return name, normalize_query(rest)


def site_host(url: str) -> str:
    """Домен сайта без «www.», «m.», «mobile.»: https://www.youtube.com/x -> youtube.com."""
    host = (urlparse(url).hostname or "").lower()
    return _STRIP_PREFIX.sub("", host)


# =============================================================================
# Скачивание страниц
# =============================================================================
def _is_web_url(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def _sniff_charset(raw: bytes) -> str | None:
    match = re.search(rb"charset\s*=\s*[\"']?([\w-]+)", raw[:4096], re.I)
    return match.group(1).decode("ascii", "replace") if match else None


def _fetch_page(url: str, max_bytes: int = MAX_PAGE_BYTES) -> tuple[str, str] | None:
    """Скачивает страницу. Возвращает (адрес после редиректов, текст) или None.

    Только http/https (file:// и прочее не открываем), размер ограничен.
    """
    if not _is_web_url(url):
        return None
    request = urllib.request.Request(url, headers=PAGE_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=PAGE_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if not _is_web_url(final_url):
                return None
            raw = response.read(max_bytes)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                # decompressobj переживает обрезанный поток и не раздувается сверх лимита
                raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, max_bytes * 2)
            charset = response.headers.get_content_charset() or _sniff_charset(raw) or "utf-8"
    except (urllib.error.URLError, OSError, ValueError, EOFError, zlib.error):
        return None
    try:
        return final_url, raw.decode(charset, errors="replace")
    except LookupError:
        return final_url, raw.decode("utf-8", errors="replace")


# =============================================================================
# Разбор HTML: ссылки на OpenSearch, JSON-LD, формы
# =============================================================================
class _PageScanner(HTMLParser):
    """Один проход по HTML: собирает всё, что может рассказать, как искать на сайте."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base_href: str | None = None
        self.opensearch: list[str] = []
        self.jsonld: list[str] = []
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._select: dict | None = None
        self._option: dict | None = None
        self._script: list[str] | None = None

    # --- вспомогательное -----------------------------------------------------
    def _finish_option(self):
        if self._option is not None and self._select is not None:
            value = self._option["value"]
            if value is None:
                value = self._option["text"].strip()
            if self._option["selected"] and self._select["value"] is None:
                self._select["value"] = value
            if self._select["first"] is None:
                self._select["first"] = value
        self._option = None

    def _finish_select(self):
        self._finish_option()
        if self._select is not None and self._form is not None:
            value = self._select["value"] if self._select["value"] is not None else self._select["first"]
            if self._select["name"] and value is not None and not self._select["disabled"]:
                self._form["fields"].append(("select", {"name": self._select["name"], "value": value}))
        self._select = None

    def _close_form(self):
        self._finish_select()
        if self._form is not None:
            self.forms.append(self._form)
        self._form = None

    # --- события парсера -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "base":
            if self.base_href is None and a.get("href"):
                self.base_href = a["href"]
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if ("search" in rel and "opensearchdescription" in a.get("type", "").lower()
                    and a.get("href")):
                self.opensearch.append(a["href"])
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self._script = []
        elif tag == "form":
            self._close_form()
            self._form = {"attrs": a, "fields": []}
        elif self._form is not None:
            if tag == "input":
                self._form["fields"].append(("input", a))
            elif tag == "select":
                self._finish_select()
                self._select = {"name": a.get("name", ""), "value": None, "first": None,
                                "disabled": "disabled" in a}
            elif tag == "option" and self._select is not None:
                self._finish_option()
                self._option = {"value": a.get("value"), "selected": "selected" in a, "text": ""}

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.jsonld.append("".join(self._script))
            self._script = None
        elif tag == "form":
            self._close_form()
        elif tag == "select":
            self._finish_select()
        elif tag == "option":
            self._finish_option()

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif self._option is not None:
            self._option["text"] += data

    def finish(self):
        try:
            self.close()
        except Exception:  # битый HTML не должен ронять программу — берём что успели собрать
            pass
        if self._script is not None:
            self.jsonld.append("".join(self._script))
            self._script = None
        self._close_form()


def _scan_html(text: str) -> _PageScanner:
    scanner = _PageScanner()
    try:
        scanner.feed(text)
    except Exception:
        pass
    scanner.finish()
    return scanner


def _is_valid_template(template: str | None) -> bool:
    return bool(template) and _is_web_url(template) and template.count(TERM) == 1


# =============================================================================
# Способ 1: OpenSearch
# =============================================================================
_OPENSEARCH_DEFAULTS = {"inputEncoding": "UTF-8", "outputEncoding": "UTF-8", "language": "*",
                        "startIndex": "1", "startPage": "1", "count": "10"}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _normalize_opensearch_template(template: str, base_url: str) -> str | None:
    url = urljoin(base_url, template.strip())
    parts = urlsplit(url)
    # необязательные параметры вида «&start={startIndex?}» просто выбрасываем
    pairs = [p for p in parts.query.split("&") if p and not re.search(r"\{[^{}]*\?\}", p)]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(pairs), parts.fragment))

    def replace(match):
        local = match.group(1).split(":")[-1]
        optional = local.endswith("?")
        local = local.rstrip("?")
        if local == "searchTerms":
            return TERM
        return "" if optional else _OPENSEARCH_DEFAULTS.get(local, "")

    url = _PLACEHOLDER.sub(replace, url)
    return url if _is_valid_template(url) else None


def _opensearch_template(xml_text: str, description_url: str) -> str | None:
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:   # защита от «взрыва» сущностей
        return None
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Url":
            continue
        kind = (element.get("type") or "").lower()
        method = (element.get("method") or "get").lower()
        rel = (element.get("rel") or "results").lower().split()
        template = element.get("template")
        if not template or method != "get" or "results" not in rel:
            continue
        if not (kind.startswith("text/html") or kind.startswith("application/xhtml")):
            continue
        result = _normalize_opensearch_template(template, description_url)
        if result:
            return result
    return None


# =============================================================================
# Способ 2: schema.org SearchAction в JSON-LD
# =============================================================================
def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _normalize_jsonld_template(target: str, query_input, base_url: str) -> str | None:
    url = urljoin(base_url, target.strip().replace("%7B", "{").replace("%7D", "}").replace("%7b", "{").replace("%7d", "}"))
    wanted = None
    if isinstance(query_input, str):
        match = re.search(r"name\s*=\s*([\w-]+)", query_input)
        wanted = match.group(1) if match else None
    elif isinstance(query_input, dict):
        wanted = query_input.get("valueName")
    names = _PLACEHOLDER.findall(url)
    if not names:
        return None
    chosen = wanted if wanted in names else (names[0] if len(names) == 1 else None)
    if chosen is None:
        return None
    done = []

    def replace(match):
        if match.group(1) == chosen and not done:
            done.append(True)
            return TERM
        return ""

    url = _PLACEHOLDER.sub(replace, url)
    return url if _is_valid_template(url) else None


def _jsonld_template(blocks: list[str], base_url: str) -> str | None:
    for text in blocks:
        try:
            data = json.loads(text)
            for node in _walk(data):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "SearchAction" not in types:
                    continue
                target = node.get("target")
                for candidate in (target if isinstance(target, list) else [target]):
                    if isinstance(candidate, dict):
                        candidate = candidate.get("urlTemplate")
                    if isinstance(candidate, str):
                        result = _normalize_jsonld_template(candidate, node.get("query-input"), base_url)
                        if result:
                            return result
        except (ValueError, RecursionError):
            continue
    return None


# =============================================================================
# Способ 3: обычная форма поиска <form method="get">
# =============================================================================
_TEXT_TYPES = {"", "text", "search"}
_BLOCKING_TYPES = {"password", "email", "tel", "file"}
_SEARCH_NAMES = {"q", "query", "s", "search", "text", "k", "keyword", "keywords", "term", "terms",
                 "searchterm", "search_query", "searchtext", "wd", "find", "qs", "txt", "kw", "string"}
_SEARCH_WORDS = ("search", "поиск", "найти", "искать", "find", "buscar", "suche", "recherche",
                 "cerca", "szukaj", "ara", "検索", "搜索", "검색")
_NOT_SEARCH_WORDS = ("login", "signin", "sign-in", "register", "subscribe", "newsletter",
                     "comment", "contact", "feedback", "auth")


def _form_candidate(form: dict, base_url: str) -> tuple[int, str] | None:
    """(оценка, шаблон адреса) для формы или None, если это не форма поиска."""
    attrs = form["attrs"]
    if (attrs.get("method") or "get").lower() != "get":
        return None
    action = (attrs.get("action") or "").strip()
    if action.lower().startswith(("javascript:", "mailto:", "#")):
        return None
    target = urljoin(base_url, action) if action else base_url
    if not _is_web_url(target):
        return None

    inputs = [f for kind, f in form["fields"] if kind == "input"]
    if any(f.get("type", "").lower() in _BLOCKING_TYPES for f in inputs):
        return None
    form_hint = " ".join(attrs.get(k, "") for k in ("role", "id", "class", "name", "action",
                                                   "aria-label", "title")).lower()
    if any(w in form_hint for w in _NOT_SEARCH_WORDS):
        return None

    text_inputs = [f for f in inputs
                   if (f.get("type", "").lower() or "text") in _TEXT_TYPES
                   and f.get("name") and "disabled" not in f]
    if not text_inputs:
        return None

    def input_score(f):
        score = 0
        if f.get("type", "").lower() == "search":
            score += 4
        if f["name"].lower() in _SEARCH_NAMES:
            score += 3
        hint = " ".join(f.get(k, "") for k in ("placeholder", "aria-label", "title", "id", "class")).lower()
        if any(w in hint for w in _SEARCH_WORDS):
            score += 2
        return score

    best = max(text_inputs, key=input_score)        # при равенстве — первый в документе
    form_score = 0
    if attrs.get("role", "").lower() == "search":
        form_score += 5
    if any(w in form_hint for w in _SEARCH_WORDS):
        form_score += 3
    total = form_score + input_score(best)
    if total < 3 or (len(text_inputs) > 1 and input_score(best) == 0):
        return None

    pairs: list[tuple[str, str | None]] = []        # None вместо значения = место для запроса
    for kind, field in form["fields"]:
        if kind == "select":
            pairs.append((field["name"], field["value"]))
            continue
        if field is best:
            pairs.append((field["name"], None))
            continue
        name, ftype = field.get("name"), field.get("type", "").lower()
        if not name or "disabled" in field:
            continue
        if ftype == "hidden":
            pairs.append((name, field.get("value", "")))
        elif ftype in ("checkbox", "radio"):
            if "checked" in field:
                pairs.append((name, field.get("value", "on")))
        elif (ftype or "text") in _TEXT_TYPES:
            pairs.append((name, ""))                 # браузер тоже отправил бы пустое поле

    parts = urlsplit(target)
    query = "&".join(f"{quote_plus(k)}={TERM if v is None else quote_plus(v)}" for k, v in pairs)
    template = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return (total, template) if _is_valid_template(template) else None


def _form_template(forms: list[dict], base_url: str) -> str | None:
    best = None
    for form in forms:
        candidate = _form_candidate(form, base_url)
        if candidate and (best is None or candidate[0] > best[0]):
            best = candidate
    return best[1] if best else None


# =============================================================================
# Поиск шаблона на странице сайта
# =============================================================================
def _template_from_page(page_url: str, html: str, fetch) -> tuple[str, str] | None:
    """(шаблон адреса поиска, способ) или None. Способы: opensearch / jsonld / form."""
    scan = _scan_html(html)
    base = urljoin(page_url, scan.base_href) if scan.base_href else page_url

    for href in scan.opensearch[:3]:
        description_url = urljoin(base, href)
        if not _is_web_url(description_url):
            continue
        page = fetch(description_url)
        if page:
            template = _opensearch_template(page[1], page[0])
            if template:
                return template, "opensearch"

    template = _jsonld_template(scan.jsonld, base)
    if template:
        return template, "jsonld"

    template = _form_template(scan.forms, base)
    if template:
        return template, "form"
    return None


_TEMPLATE_CACHE: dict[str, tuple[str, str] | None] = {}


def clear_cache() -> None:
    _TEMPLATE_CACHE.clear()


def find_search_template(page_url: str, fetch=None) -> tuple[str, str] | None:
    """Выясняет по странице сайта, как искать на нём. -> (шаблон с {searchTerms}, способ) или None.

    Сначала смотрим саму страницу (для «яндекс карты» это поиск по картам), потом главную.
    Со своей fetch (тесты) кэш не используется.
    """
    use_cache = fetch is None
    fetch = fetch or _fetch_page
    if use_cache and page_url in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[page_url]

    parts = urlsplit(page_url)
    root = f"{parts.scheme}://{parts.netloc}/"
    candidates = [page_url] + ([root] if root != page_url else [])
    reachable = False
    result = None
    for candidate in candidates:
        page = fetch(candidate)
        if not page:
            continue
        reachable = True
        result = _template_from_page(page[0], page[1], fetch)
        if result:
            break
    if use_cache and (result or reachable):   # временный сбой сети не запоминаем
        _TEMPLATE_CACHE[page_url] = result
    return result


def fill_template(template: str, term: str) -> str:
    """Подставляет запрос в шаблон: в строке запроса пробел -> «+», в пути/после «#» -> «%20»."""
    term = normalize_query(term)
    head = template.partition(TERM)[0]
    in_query = "?" in head and "#" not in head
    encoded = quote_plus(term) if in_query else quote(term, safe="")
    return template.replace(TERM, encoded, 1)


# =============================================================================
# Редиректы Google и план действий
# =============================================================================
@dataclass(frozen=True)
class Plan:
    """Что открывать и почему.

    kind:
      plain        — обычный запрос, открыт найденный сайт
      fallback     — Google не ответил, открыта ссылка «Мне повезёт» через Google
      site_home    — «сайт []»: открыта главная найденного сайта
      site_search  — открыт поиск, найденный на самом сайте (source: opensearch / jsonld / form)
      google_site  — на сайте поиск не нашёлся, результат получен через «site:домен запрос»
    """
    kind: str
    url: str
    site: str | None = None
    term: str | None = None
    source: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не переходим по редиректам сами — нам нужен только адрес из Location."""

    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_location(url: str) -> tuple[int | None, str | None]:
    """Один запрос без перехода по редиректу. Возвращает (код, заголовок Location)."""
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.headers.get("Location")
    except urllib.error.HTTPError as exc:  # 3xx приходят сюда, раз редирект отключён
        return exc.code, exc.headers.get("Location")
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def _is_google_host(host: str) -> bool:
    return bool(_GOOGLE_HOST.search(host.lower()))


def _is_google_service_page(url: str) -> bool:
    """Поиск, редирект, капча или окно согласия самого Google — это не сайт-результат.

    А вот Gmail, Карты, Диск и т.п. (тоже google.com) — полноценные результаты.
    """
    parts = urlparse(url)
    host = parts.hostname or ""
    if not _is_google_host(host):
        return False
    return host.startswith("consent.") or parts.path.startswith(("/search", "/url", "/sorry"))


def resolve_target(lucky_url: str, fetch=None) -> str | None:
    """Возвращает адрес первого сайта или None, если выяснить не удалось.

    Идём по редиректам Google вручную. Как только встречаем google.com/url?q=САЙТ
    (страница «Redirect Notice») — берём САЙТ. Окно согласия, /sorry (капча) и
    любые не-http(s) ссылки считаем неудачей.
    """
    fetch = fetch or _fetch_location
    url = lucky_url
    for _ in range(MAX_REDIRECT_HOPS):
        _status, location = fetch(url)
        if not location:
            return None
        next_url = urljoin(url, location)
        if not _is_web_url(next_url):
            return None
        parts = urlparse(next_url)
        host = parts.hostname or ""
        if _is_google_host(host):
            if host.startswith("consent.") or parts.path.startswith("/sorry"):
                return None
            if parts.path == "/url":
                params = parse_qs(parts.query)
                target = (params.get("q") or params.get("url") or [""])[0]
                if _is_web_url(target) and not _is_google_service_page(target):
                    return target
                return None
            if parts.path.startswith("/search"):
                url = next_url  # ещё один редирект внутри Google
                continue
            return next_url  # другой сервис Google (Gmail, Карты…) — это и есть результат
        return next_url  # Google отправил сразу на сайт
    return None


def _plain_plan(query: str, resolver) -> Plan:
    lucky_url = build_url(query)
    found = resolver(lucky_url)
    return Plan("plain", found) if found else Plan("fallback", lucky_url)


def make_plan(text: str, resolver=None, finder=None) -> Plan | None:
    """Решает, какой адрес открыть. None — вводить нечего (пустой ввод).

    resolver(lucky_url) -> адрес сайта или None; по умолчанию resolve_target.
    finder(page_url) -> (шаблон, способ) или None; по умолчанию find_search_template.
    """
    resolver = resolver or resolve_target
    finder = finder or find_search_template
    name, term = parse_input(text)
    if term is None:                      # обычный запрос, без скобок
        return _plain_plan(name, resolver) if name else None
    if not name:                          # «[запрос]» — название не указано
        return _plain_plan(term, resolver) if term else None

    home = resolver(build_url(name))      # 1. какой сайт имелся в виду
    if not home:
        whole = f"{name} {term}".strip()
        return Plan("fallback", build_url(whole), term=term or None)
    host = site_host(home)
    if not term:                          # «сайт []» — просто открыть сайт
        return Plan("site_home", home, host)

    found = finder(home)                  # 2. как искать на этом сайте — узнаём у самого сайта
    if found:
        template, source = found
        return Plan("site_search", fill_template(template, term), host, term, source)

    site_query = f"site:{host} {term}"    # 3. поиск на сайте не нашёлся — через Google
    google_result = resolver(build_url(site_query))
    if google_result:
        return Plan("google_site", google_result, host, term)
    return Plan("fallback", build_url(site_query), host, term)   # 4. запасной вариант


# =============================================================================
# Запуск браузера
# =============================================================================
def _launch(cmd: list[str], wait_seconds: float = LAUNCH_WAIT_SECONDS) -> str | None:
    """Запускает команду. None — успех, иначе текст ошибки.

    Если процесс ещё работает через wait_seconds — значит Firefox стартует
    (это нормально, особенно для snap-версии), считаем запуск успешным.
    Если завершился с кодом 0 — команда передана уже открытому Firefox.
    Если завершился с ошибкой — возвращаем её текст.
    """
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                return None
            if code == 0:
                return None
            err.seek(0)
            message = err.read().decode(errors="replace").strip()
            return f"{cmd[0]} завершился с кодом {code}: {message[-300:]}"
    except OSError as exc:
        return f"{cmd[0]}: {exc}"


def open_in_browser(url: str) -> None:
    """Открывает ссылку в Firefox; если не вышло — через xdg-open.

    Бросает BrowserError, если ничего не сработало.
    """
    candidates = []
    for name in ("firefox", "xdg-open"):
        path = shutil.which(name)
        if path:
            candidates.append([path, url])
    if not candidates:
        raise BrowserError("не найден ни firefox, ни xdg-open")

    errors = []
    for cmd in candidates:
        error = _launch(cmd)
        if error is None:
            return
        errors.append(error)
    raise BrowserError("; ".join(errors))


def open_first_result(query: str, opener=open_in_browser, resolver=None, finder=None) -> str | None:
    """Открывает результат. Возвращает открытую ссылку или None для пустого запроса.

    Понимает и обычный запрос, и «сайт [запрос]» (см. make_plan).
    """
    plan = make_plan(query, resolver, finder)
    if plan is None:
        return None
    opener(plan.url)
    return plan.url


_SOURCE_NAMES = {"opensearch": "OpenSearch", "jsonld": "SearchAction", "form": "форма поиска"}


def _describe(plan: Plan) -> str:
    if plan.kind == "site_home":
        return f"Открываю сайт {plan.site}: {plan.url}"
    if plan.kind == "site_search":
        how = _SOURCE_NAMES.get(plan.source or "", plan.source or "?")
        return f"Сайт {plan.site}, ищу «{plan.term}» (поиск найден на сайте: {how}): {plan.url}"
    if plan.kind == "google_site":
        return f"Сайт {plan.site} (свой поиск не нашёл), ищу «{plan.term}» через Google: {plan.url}"
    if plan.kind == "fallback":
        if plan.site:
            return (f"Сайт {plan.site}, запрос «{plan.term}»: прямую ссылку получить не удалось, "
                    "открываю через Google (может появиться страница-уведомление)…")
        return ("Прямую ссылку получить не удалось, открываю через Google "
                "(может появиться страница-уведомление)…")
    return f"Открываю: {plan.url}"


def _handle(query: str, opener, dry_run: bool) -> bool:
    """Обрабатывает один запрос. True — всё хорошо (или запрос пустой)."""
    resolver = (lambda url: None) if dry_run else None  # --dry-run: без сети
    plan = make_plan(query, resolver)
    if plan is None:
        return True
    try:
        opener(plan.url)
    except BrowserError as exc:
        print(f"Ошибка: не удалось открыть браузер — {exc}")
        return False
    print(plan.url if dry_run else _describe(plan))
    return True


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    opener = (lambda url: None) if dry_run else open_in_browser

    if args:  # запрос передан аргументами
        text = " ".join(args)
        if make_plan(text, lambda url: None) is None:
            print("Пустой запрос.")
            return 1
        return 0 if _handle(text, opener, dry_run) else 1

    print("Введи запрос или «сайт [запрос]» (exit или выход — закрыть).")
    while True:
        try:
            query = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if normalize_query(query).lower() in EXIT_WORDS:
            return 0
        _handle(query, opener, dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
