#!/usr/bin/env python3
"""Пишешь команду обычными словами -> наша модель (папка ml/) понимает, что и где искать -> открывается Firefox.

    открой ютуб и найди linkin park       -> поиск «linkin park» внутри YouTube
    запусти саунд клауд                    -> просто открыть сайт
    загугли рецепт борща                   -> страница результатов Google (именно поиск, а не первый сайт)
    переведи на английский как дела        -> Google Переводчик с уже подставленным текстом и языком
    кто такой павел дуров                  -> это не для браузера: «llm», браузер не открывается

Никаких скобок и особого синтаксиса: сайт, запрос, текст и язык перевода выделяет обученная модель.

Сайт не назван, есть запрос («загугли …», «поищи в интернете …», «включи …»)
------------------------------------------------------------------------------
Открывается страница результатов Google: https://www.google.com/search?q=запрос. Раньше здесь стоял режим «Мне повезёт»
(btnI=1) — он сам уводил на первый сайт из выдачи, поэтому «загугли» не гуглил. Теперь «Мне повезёт» используется
только для одного: узнать, какой сайт имелся в виду, когда пользователь назвал его («ютуб», «алиэкспресс»).
Если Google не ответил на такой вопрос — открывается ссылка «Мне повезёт» через Google (может появиться страница
«Redirect Notice»).

Перевод (route == "translate")
------------------------------
Открывается Google Переводчик: translate.google.com/?sl=auto&tl=<язык>&text=<текст>&op=translate. Язык перевода
даёт модель. Если он не назван («переведи привет»), берётся по тексту: кириллица -> английский, латиница -> русский.

Поиск внутри сайта (модель выделила и сайт, и запрос)
------------------------------------------------------
Заранее заготовленных адресов поиска НЕТ. Как искать на сайте, скрипт выясняет сам, из страницы этого сайта:
  1) название сайта отправляется в Google («Мне повезёт») -> получаем страницу сайта;
  2) скачиваем эту страницу (если на ней поиска нет — главную) и ищем, как сайт сам описывает свой поиск:
       • OpenSearch  — <link rel="search" type="application/opensearchdescription+xml">;
       • SearchAction — разметка schema.org в JSON-LD;
       • форма поиска — обычная <form method="get"> с полем ввода;
  3) нашли — подставляем запрос в найденный адрес и открываем;
  4) не нашли — спрашиваем Google «site:домен запрос»;
  5) Google не ответил — открывается обычная ссылка через Google.
Найденное запоминается до конца работы программы.

Модель
------
Лежит в ml/ml_model/ (её создаёт обучение: cd ml && python3 ml_train.py --mode final). Модель лениво грузится при
первой команде. Нет модели — программа так и скажет. Нужны torch и transformers (pip install -r requirements.txt).

Запуск:
    python3 gogle_search.py                                  # диалог
    python3 gogle_search.py "открой ютуб и найди linkin park"
    python3 gogle_search.py --dry-run "открой ютуб и найди linkin park"   # только показать ссылку (без сети и браузера)
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
from pathlib import Path
from urllib.parse import (parse_qs, quote, quote_plus, urlencode, urljoin, urlparse,
                          urlsplit, urlunsplit)

SEARCH_URL = "https://www.google.com/search"
TRANSLATE_URL = "https://translate.google.com/"
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
    """Ссылка «Мне повезёт» (btnI=1): Google сам уводит на первый результат. Нужна только для того, чтобы
    узнать, какой сайт имелся в виду; пользователю такую ссылку не открываем. Кодирование в UTF-8 делает urlencode."""
    return SEARCH_URL + "?" + urlencode({"q": normalize_query(query), "btnI": "1"})


def search_url(query: str) -> str:
    """Обычная страница результатов Google (без «Мне повезёт»): то, что открывается на «загугли»."""
    return SEARCH_URL + "?" + urlencode({"q": normalize_query(query)})


# =============================================================================
# Перевод
# =============================================================================
# Коды наших языков (ml_spec.LANGS) -> коды Google Переводчика. Остальные совпадают.
_TRANSLATE_CODES = {"zh": "zh-CN", "he": "iw"}
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_LATIN = re.compile(r"[A-Za-z]")


def target_language(text: str, lang: str = "none") -> str:
    """Язык, НА который переводим. Назван — берём его; не назван («none») — угадываем по тексту:
    больше кириллицы -> en, есть латиница -> ru, иначе (цифры, значки) -> en."""
    if lang and lang != "none":
        return _TRANSLATE_CODES.get(lang, lang)
    cyrillic, latin = len(_CYRILLIC.findall(text)), len(_LATIN.findall(text))
    return "ru" if latin > cyrillic else "en"


def translate_url(text: str, lang: str = "none") -> str:
    """Google Переводчик с подставленными текстом и языком. Исходный язык определяет сам переводчик (sl=auto)."""
    text = normalize_query(text)
    params = {"sl": "auto", "tl": target_language(text, lang), "text": text, "op": "translate"}
    return TRANSLATE_URL + "?" + urlencode(params, quote_via=quote)   # пробел -> %20 (так надёжнее, чем «+»)


# =============================================================================
# Адрес сайта
# =============================================================================
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
      google_search — сайт не назван (или назван сам Google): страница результатов Google по запросу
      translate     — Google Переводчик (term — текст, source — код языка перевода)
      fallback      — Google не ответил, открыта ссылка «Мне повезёт» через Google
      site_home     — только сайт: открыта главная найденного сайта
      site_search   — открыт поиск, найденный на самом сайте (source: opensearch / jsonld / form)
      google_site   — на сайте поиск не нашёлся, результат получен через «site:домен запрос»
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


def _is_google_root(url: str) -> bool:
    """Главная страница самого Google (google.com, google.bg/?hl=ru…), а не Карты, Диск или Gmail."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    # именно google.<домен> или www.google.<домен>: mail.google.com (Gmail), maps.google.com и т.п. — не поиск
    return bool(re.fullmatch(r"(www\.)?google\.[a-z.]+", host)) and parts.path in ("", "/", "/webhp")


def make_translate_plan(text: str, lang: str = "none") -> Plan | None:
    """Google Переводчик для (текст, язык перевода). None — переводить нечего."""
    text = normalize_query(text or "")
    if not text:
        return None
    return Plan("translate", translate_url(text, lang), term=text, source=target_language(text, lang))


def make_plan(site: str, query: str = "", resolver=None, finder=None) -> Plan | None:
    """Решает, какой адрес открыть. None — искать нечего (и сайт, и запрос пустые).

    site и query приходят от модели. resolver(lucky_url) -> адрес сайта или None (по умолчанию resolve_target);
    finder(page_url) -> (шаблон, способ) или None (по умолчанию find_search_template).
    """
    resolver = resolver or resolve_target
    finder = finder or find_search_template
    site, query = normalize_query(site or ""), normalize_query(query or "")
    if not site:                          # сайт не назван — настоящий поиск в Google (страница результатов)
        return Plan("google_search", search_url(query), term=query) if query else None

    home = resolver(build_url(site))      # 1. какой сайт имелся в виду
    if not home:
        whole = f"{site} {query}".strip()
        return Plan("fallback", build_url(whole), term=query or None)
    host = site_host(home)
    if not query:                         # только сайт — просто открыть
        return Plan("site_home", home, host)
    if _is_google_root(home):             # «открой гугл и найди X» — это обычный поиск Google
        return Plan("google_search", search_url(query), host, query)

    found = finder(home)                  # 2. как искать на этом сайте — узнаём у самого сайта
    if found:
        template, source = found
        return Plan("site_search", fill_template(template, query), host, query, source)

    site_query = f"site:{host} {query}"   # 3. поиск на сайте не нашёлся — через Google
    google_result = resolver(build_url(site_query))
    if google_result:
        return Plan("google_site", google_result, host, query)
    return Plan("fallback", build_url(site_query), host, query)   # 4. запасной вариант


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


_SOURCE_NAMES = {"opensearch": "OpenSearch", "jsonld": "SearchAction", "form": "форма поиска"}


def _describe(plan: Plan) -> str:
    if plan.kind == "google_search":
        return f"Ищу в Google «{plan.term}»: {plan.url}"
    if plan.kind == "translate":
        return f"Перевожу на {plan.source} в Google Переводчике «{plan.term}»: {plan.url}"
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


# =============================================================================
# Модель: фраза -> что делать
# =============================================================================
ML_DIR = Path(__file__).resolve().parent / "ml"


class ModelError(RuntimeError):
    """Обученной модели нет или она не загрузилась."""


def model_parse(phrase: str) -> dict:
    """Фраза -> решение модели: {"route": browser|translate|llm, "site", "query", "text", "lang", "fallback", ...}.

    Тяжёлые torch/transformers грузятся только здесь, при первой команде. Журнал команд (ML_PARSE_LOG=1) пишется сам.
    """
    if not (ML_DIR / "ml_model" / "heads.pt").exists():
        raise ModelError(f"нет обученной модели: {ML_DIR / 'ml_model'}. Положите сюда папку ml_model "
                         "или обучите: cd ml && python3 ml_gen.py --mode final && python3 ml_train.py --mode final")
    if str(ML_DIR) not in sys.path:
        sys.path.insert(0, str(ML_DIR))
    try:
        import ml_infer
        import ml_log
    except ImportError as exc:
        raise ModelError(f"не хватает пакета для модели ({exc}). Установите: pip install -r requirements.txt") from exc
    result = ml_infer.get_engine().predict_one(phrase)
    ml_log.log_result(result)
    return result


def open_first_result(site: str, query: str = "", opener=open_in_browser, resolver=None, finder=None) -> str | None:
    """Открывает результат для (сайт, запрос). Возвращает открытую ссылку или None, если искать нечего."""
    plan = make_plan(site, query, resolver, finder)
    if plan is None:
        return None
    opener(plan.url)
    return plan.url


def handle_phrase(phrase: str, parser=model_parse, opener=open_in_browser, resolver=None, finder=None):
    """Одна фраза от начала до конца. Возвращает (решение модели, план или None).

    browser   -> поиск в Google / сайт / поиск на сайте;
    translate -> Google Переводчик с подставленными текстом и языком;
    llm       -> браузер не открывается (эта ветка ещё не подключена), план None.
    """
    result = parser(phrase)
    route = result.get("route")
    if route == "browser":
        plan = make_plan(result.get("site", ""), result.get("query", ""), resolver, finder)
    elif route == "translate":
        plan = make_translate_plan(result.get("text", ""), result.get("lang", "none"))
    else:
        return result, None
    if plan is not None:
        opener(plan.url)
    return result, plan


def _show_model(result: dict) -> str:
    route = result.get("route")
    extra = {"browser": f"сайт={result.get('site') or '—'}, запрос={result.get('query') or '—'}",
             "translate": f"текст={result.get('text')!r}, язык={result.get('lang')}",
             "llm": "отправляю большой LLM (пока не подключена)"}.get(route, "")
    note = f"  [страховка: {result['reason']}]" if result.get("fallback") else ""
    return f"модель: {route} · {extra}{note}"


def _run(phrase: str, opener, dry_run: bool) -> bool:
    """Обрабатывает одну фразу. True — всё хорошо (или фраза пустая)."""
    if not normalize_query(phrase):
        return True
    resolver = (lambda url: None) if dry_run else None      # --dry-run: без сети
    try:
        result, plan = handle_phrase(phrase, model_parse, opener, resolver)
    except ModelError as exc:
        print(f"Ошибка: {exc}")
        return False
    except BrowserError as exc:
        print(f"Ошибка: не удалось открыть браузер — {exc}")
        return False
    print(_show_model(result))
    if plan is None:
        if result.get("route") == "browser":
            print("Искать нечего.")
        elif result.get("route") == "translate":
            print("Переводить нечего.")
        return True
    print(plan.url if dry_run else _describe(plan))
    return True


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    opener = (lambda url: None) if dry_run else open_in_browser

    if args:  # фраза передана аргументами
        text = " ".join(args)
        if not normalize_query(text):
            print("Пустой запрос.")
            return 1
        return 0 if _run(text, opener, dry_run) else 1

    try:
        sys.stdin.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    print("Скажи команду обычными словами (exit или выход — закрыть).")
    while True:
        try:
            query = input("> ").replace("\ufffd", "")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if normalize_query(query).lower() in EXIT_WORDS:
            return 0
        _run(query, opener, dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
