"""Тесты gogle_search.py.

Запуск (подробный вывод уже включён в pytest.ini):
    python3 -m pytest                      # все тесты
    python3 -m pytest -k "ютюб"            # только запросы, где есть «ютюб»
    python3 -m pytest -k "опечатки" -s     # группа + пошаговый разбор каждого случая
    python3 -m pytest -k "сайт+запрос" -s  # поиск внутри сайта: «название [запрос]»

ГЛАВНАЯ ПРОВЕРКА обычных запросов — «полная цепочка». Для каждого запроса тест делает так:

    ВВОД (как набрал человек)
      -> скрипт чистит пробелы             -> ожидаем ЗАПРОС для Google
      -> скрипт идёт в «Google»            -> здесь ИМИТАЦИЯ Google, которая отвечает
                                              страницей Redirect Notice с заданным САЙТОМ
      -> скрипт достаёт САЙТ из ответа     -> ожидаем, что Firefox откроет именно его

ГЛАВНАЯ ПРОВЕРКА поиска внутри сайта — запрос пишется как «название [запрос]»:

    ВВОД «саундклауд [9mice]»
      -> название и запрос делятся по скобкам
      -> имитация Google возвращает адрес сайта
      -> тестовая страница сайта описывает поиск через OpenSearch, JSON-LD или GET-форму
      -> найденный шаблон заполняется запросом и открывается
      -> если шаблон не найден, проверяется запасной путь Google «site:домен запрос»

Важно: настоящий Google и внешние сайты в тестах не нужны. Ответы Google и HTML-страницы
имитируются. Тесты проверяют поведение кода, не завися от реального интернета.
"""
import http.server
import shutil
import subprocess
import threading
from typing import NamedTuple
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import pytest

import gogle_search as open_site

REAL_RESOLVE = open_site.resolve_target
ANY_SITE = "https://example.org/result"  # для запросов, где «правильного» сайта нет


@pytest.fixture(autouse=True)
def без_сети(monkeypatch):
    """Обычные тесты не ходят в интернет: резолвер по умолчанию ничего не находит."""
    monkeypatch.setattr(open_site, "resolve_target", lambda url: None)


# =============================================================================
# ТАБЛИЦА ЗАПРОСОВ: (группа, что вводим, какой сайт вернёт имитация Google)
# None вместо сайта = «любой сайт»
# =============================================================================
BASES = [
    # ---- русские названия сайтов --------------------------------------------
    ("русские названия", "вконтакте", "https://vk.com/"),
    ("русские названия", "одноклассники", "https://ok.ru/"),
    ("русские названия", "яндекс", "https://yandex.ru/"),
    ("русские названия", "яндекс почта", "https://mail.yandex.ru/"),
    ("русские названия", "яндекс карты", "https://yandex.ru/maps/"),
    ("русские названия", "авито", "https://www.avito.ru/"),
    ("русские названия", "озон", "https://www.ozon.ru/"),
    ("русские названия", "вайлдберриз", "https://www.wildberries.ru/"),
    ("русские названия", "сбербанк онлайн", "https://online.sberbank.ru/"),
    ("русские названия", "тинькофф", "https://www.tbank.ru/"),
    ("русские названия", "госуслуги", "https://www.gosuslugi.ru/"),
    ("русские названия", "ржд билеты", "https://ticket.rzd.ru/"),
    ("русские названия", "википедия", "https://ru.wikipedia.org/"),
    ("русские названия", "хабр", "https://habr.com/"),
    ("русские названия", "пикабу", "https://pikabu.ru/"),
    ("русские названия", "кинопоиск", "https://www.kinopoisk.ru/"),
    ("русские названия", "ютуб", "https://www.youtube.com/"),
    ("русские названия", "рутуб", "https://rutube.ru/"),
    ("русские названия", "телеграм веб", "https://web.telegram.org/"),
    ("русские названия", "мейл ру", "https://mail.ru/"),
    ("русские названия", "дзен", "https://dzen.ru/"),
    ("русские названия", "лента ру", "https://lenta.ru/"),
    ("русские названия", "рбк", "https://www.rbc.ru/"),
    ("русские названия", "банки ру", "https://www.banki.ru/"),
    ("русские названия", "гисметео", "https://www.gismeteo.ru/"),
    # ---- английские названия сайтов -----------------------------------------
    ("английские названия", "youtube", "https://www.youtube.com/"),
    ("английские названия", "github", "https://github.com/"),
    ("английские названия", "stackoverflow", "https://stackoverflow.com/"),
    ("английские названия", "python docs", "https://docs.python.org/3/"),
    ("английские названия", "wikipedia", "https://www.wikipedia.org/"),
    ("английские названия", "reddit", "https://www.reddit.com/"),
    ("английские названия", "amazon", "https://www.amazon.com/"),
    ("английские названия", "netflix", "https://www.netflix.com/"),
    ("английские названия", "twitter", "https://x.com/"),
    ("английские названия", "linkedin", "https://www.linkedin.com/"),
    ("английские названия", "ubuntu", "https://ubuntu.com/"),
    ("английские названия", "firefox download", "https://www.mozilla.org/firefox/new/"),
    ("английские названия", "google maps", "https://www.google.com/maps"),   # сервис Google
    ("английские названия", "gmail", "https://mail.google.com/"),            # сервис Google
    ("английские названия", "chatgpt", "https://chatgpt.com/"),
    ("английские названия", "openai", "https://openai.com/"),
    ("английские названия", "anthropic", "https://www.anthropic.com/"),
    ("английские названия", "ollama", "https://ollama.com/"),
    ("английские названия", "pytorch", "https://pytorch.org/"),
    ("английские названия", "kaggle", "https://www.kaggle.com/"),
    ("английские названия", "hugging face", "https://huggingface.co/"),
    ("английские названия", "arxiv", "https://arxiv.org/"),
    ("английские названия", "coursera", "https://www.coursera.org/"),
    ("английские названия", "udemy", "https://www.udemy.com/"),
    ("английские названия", "leetcode", "https://leetcode.com/"),
    # ---- опечатки (Google должен понять, что имелось в виду) ------------------
    ("опечатки", "ютюб", "https://www.youtube.com/"),
    ("опечатки", "ютуб ком", "https://www.youtube.com/"),
    ("опечатки", "гитхаб", "https://github.com/"),
    ("опечатки", "гит хаб", "https://github.com/"),
    ("опечатки", "стэковерфлов", "https://stackoverflow.com/"),
    ("опечатки", "стековерфлоу", "https://stackoverflow.com/"),
    ("опечатки", "вокипедия", "https://ru.wikipedia.org/"),
    ("опечатки", "википидия", "https://ru.wikipedia.org/"),
    ("опечатки", "вкантакте", "https://vk.com/"),
    ("опечатки", "одноклассники ру", "https://ok.ru/"),
    ("опечатки", "yutub", "https://www.youtube.com/"),
    ("опечатки", "gogle", "https://www.google.com/"),
    ("опечатки", "facebok", "https://www.facebook.com/"),
    ("опечатки", "githb", "https://github.com/"),
    ("опечатки", "stakoverflow", "https://stackoverflow.com/"),
    ("опечатки", "wikipeda", "https://www.wikipedia.org/"),
    ("опечатки", "redit", "https://www.reddit.com/"),
    ("опечатки", "amazn", "https://www.amazon.com/"),
    ("опечатки", "netflicks", "https://www.netflix.com/"),
    ("опечатки", "linkdin", "https://www.linkedin.com/"),
    ("опечатки", "ubunto", "https://ubuntu.com/"),
    ("опечатки", "firefx", "https://www.mozilla.org/firefox/new/"),
    ("опечатки", "huggin face", "https://huggingface.co/"),
    ("опечатки", "kagle", "https://www.kaggle.com/"),
    ("опечатки", "leetcod", "https://leetcode.com/"),
    # ---- фразы с предлогами «в», «на», «по» -------------------------------------
    ("предлоги в/на/по", "погода в софии", None),
    ("предлоги в/на/по", "курс доллара на сегодня", None),
    ("предлоги в/на/по", "расписание поездов в софию", None),
    ("предлоги в/на/по", "как добраться на работу", None),
    ("предлоги в/на/по", "билеты на самолёт в софию", None),
    ("предлоги в/на/по", "рестораны в софии", None),
    ("предлоги в/на/по", "новости на русском", None),
    ("предлоги в/на/по", "кино на вечер", None),
    ("предлоги в/на/по", "python в docker", None),
    ("предлоги в/на/по", "установить ubuntu на флешку", None),
    ("предлоги в/на/по", "прогноз на завтра софия", None),
    ("предлоги в/на/по", "учебник по python на русском", None),
    ("предлоги в/на/по", "документация на python", None),
    ("предлоги в/на/по", "работа в it софия", None),
    ("предлоги в/на/по", "купить ноутбук в болгарии", None),
    # ---- спецсимволы, которые могут сломать ссылку ------------------------------
    ("спецсимволы", "c++ tutorial", None),
    ("спецсимволы", "q&a сайт", None),
    ("спецсимволы", "что такое #хештег", None),
    ("спецсимволы", "a=b python", None),
    ("спецсимволы", '"pandas dataframe"', None),
    ("спецсимволы", "python? или что", None),
    ("спецсимволы", "100% python", None),
    ("спецсимволы", "путь/к/файлу linux", None),
    ("спецсимволы", "ёлка новогодняя", None),
    ("спецсимволы", "café paris", None),
    # ---- не та раскладка клавиатуры ---------------------------------------------
    ("не та раскладка", "ghbdtn", None),            # «привет» в английской раскладке
    ("не та раскладка", "рудддщ", None),             # «hello» в русской раскладке
    ("не та раскладка", "нщгегиу", "https://www.youtube.com/"),    # «youtube»
    ("не та раскладка", "пшерги", "https://github.com/"),          # «github»
    ("не та раскладка", "ыефсл", None),              # «stack»
    ("не та раскладка", "цшлшзувшф", "https://www.wikipedia.org/"),  # «wikipedia»
    ("не та раскладка", "фьфящт", "https://www.amazon.com/"),      # «amazon»
    ("не та раскладка", "ьфзы", None),               # «maps»
    ("не та раскладка", "пщщпду", "https://www.google.com/"),      # «google»
    ("не та раскладка", "ghbdtn vbh", None),         # «привет мир»
]

ВАРИАНТЫ_ВВОДА = [
    ("как есть", lambda q: q),
    ("пробелы по краям", lambda q: f"   {q}   "),
    ("лишние пробелы и табы", lambda q: "\u00a0\t" + q.replace(" ", "   ") + " \t"),
]


class Случай(NamedTuple):
    группа: str
    вариант: str
    ввод: str      # то, что набрал человек (с кривыми пробелами)
    запрос: str    # то, что должен получить Google (пробелы почищены)
    сайт: str      # что отвечает имитация Google
    сайт_подпись: str

    @property
    def id(self) -> str:
        return (f"[{self.группа}] {self.вариант}: "
                f"ввод {self.ввод!r} -> гуглим {self.запрос!r} -> ждём {self.сайт_подпись}")


СЛУЧАИ = [
    Случай(группа, вариант, подготовить(запрос), запрос,
           сайт or ANY_SITE, сайт or "любой сайт")
    for группа, запрос, сайт in BASES
    for вариант, подготовить in ВАРИАНТЫ_ВВОДА
]
БЕЗ_КРИВИЗНЫ = [c for c in СЛУЧАИ if c.вариант == "как есть"]


def ids(cases):
    return [c.id for c in cases]


def test_запросов_не_меньше_300():
    assert len(СЛУЧАИ) >= 300, f"в таблице всего {len(СЛУЧАИ)} запросов"


# =============================================================================
# 1. ПОЛНАЯ ЦЕПОЧКА для каждого запроса (330 тестов)
# =============================================================================
@pytest.mark.parametrize("c", СЛУЧАИ, ids=ids(СЛУЧАИ))
def test_полная_цепочка_ввод_запрос_сайт(c):
    """ввод -> чистка -> запрос в Google -> ответ Google -> сайт -> Firefox."""
    ушло_в_google = []
    ответ_google = (
        f"https://www.google.com/url?q={quote(c.сайт, safe='')}&sa=U&ved=2ahUKEwj"
    )

    def имитация_google(url):
        ушло_в_google.append(url)
        return 302, ответ_google

    открыто_в_firefox = []
    open_site.open_first_result(
        c.ввод,
        opener=открыто_в_firefox.append,
        resolver=lambda url: REAL_RESOLVE(url, fetch=имитация_google),
    )

    print(f"\nВВОД             : {c.ввод!r}")
    print(f"ОЖИДАЕМ ЗАПРОС   : {c.запрос!r}")
    print(f"УШЛО В GOOGLE    : {ушло_в_google}")
    print(f"ОТВЕТ GOOGLE     : {ответ_google}")
    print(f"ОТКРЫТО В FIREFOX: {открыто_в_firefox}")

    assert len(ушло_в_google) == 1, f"в Google должен уйти 1 запрос, ушло {len(ушло_в_google)}"
    разобран = urlparse(ушло_в_google[0])
    параметры = parse_qs(разобран.query)
    assert разобран.netloc == "www.google.com" and разобран.path == "/search", \
        f"запрос ушёл не в Google-поиск: {ушло_в_google[0]}"
    assert параметры["q"] == [c.запрос], \
        f"Google должен получить запрос {c.запрос!r}, а получил {параметры['q']!r}"
    assert параметры["btnI"] == ["1"], "не включён режим «Мне повезёт» (btnI=1)"
    assert открыто_в_firefox == [c.сайт], \
        f"Firefox должен открыть {c.сайт}, а открыл {открыто_в_firefox}"


# =============================================================================
# 2. ЗАПАСНОЙ ВАРИАНТ: Google не ответил — открывается обычная ссылка (110 тестов)
# =============================================================================
@pytest.mark.parametrize("c", БЕЗ_КРИВИЗНЫ, ids=ids(БЕЗ_КРИВИЗНЫ))
def test_если_google_не_ответил_открывается_ссылка_через_google(c):
    """Google недоступен -> Firefox всё равно получает ссылку «Мне повезёт» с тем же запросом."""
    открыто = []
    open_site.open_first_result(c.ввод, opener=открыто.append,
                                resolver=lambda url: REAL_RESOLVE(url, fetch=lambda u: (None, None)))
    assert len(открыто) == 1, f"браузер должен открыться 1 раз, открылся {len(открыто)}"
    параметры = parse_qs(urlparse(открыто[0]).query)
    assert параметры["q"] == [c.запрос], f"ожидали запрос {c.запрос!r}, в ссылке {параметры['q']!r}"
    assert параметры["btnI"] == ["1"]


@pytest.mark.parametrize("c", СЛУЧАИ[:30], ids=ids(СЛУЧАИ[:30]))
def test_ссылка_состоит_только_из_ascii_без_пробелов(c):
    """Кириллица и пробелы должны быть закодированы, иначе ссылка сломается."""
    url = open_site.build_url(c.ввод)
    assert url.isascii(), f"в ссылке остались не-ASCII символы: {url}"
    assert " " not in url, f"в ссылке остался пробел: {url}"


# =============================================================================
# 3. ПУСТОЙ ВВОД и ЧИСТКА ПРОБЕЛОВ
# =============================================================================
ПУСТЫЕ = [("", "пустая строка"), (" ", "один пробел"), ("   ", "три пробела"),
          ("\t", "таб"), ("\u00a0", "неразрывный пробел"), (" \t\n ", "пробелы, таб и перевод строки")]


@pytest.mark.parametrize("ввод", [p[0] for p in ПУСТЫЕ], ids=[p[1] for p in ПУСТЫЕ])
def test_пустой_ввод_ничего_не_открывает(ввод):
    открыто = []
    результат = open_site.open_first_result(ввод, opener=открыто.append)
    assert результат is None, f"для пустого ввода ожидали None, получили {результат!r}"
    assert открыто == [], "браузер не должен открываться"


def test_чистка_убирает_лишние_пробелы_но_не_меняет_слова_и_регистр():
    ввод, ожидаем = "  Ютуб   Музыка ", "Ютуб Музыка"
    assert open_site.normalize_query(ввод) == ожидаем


# =============================================================================
# 4. ЗАПУСК FIREFOX
# =============================================================================
class ПоддельныйПроцесс:
    """running=True — процесс не завершается (Firefox стартует); иначе завершается с кодом code."""

    def __init__(self, running=False, code=0):
        self.running, self.code = running, code

    def wait(self, timeout=None):
        if self.running:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return self.code


def подменить_запуск(monkeypatch, найдено, процессы):
    """найдено: какие программы «есть в системе»; процессы: как поведут себя запуски по порядку."""
    запущено = []
    monkeypatch.setattr(shutil, "which", lambda имя: найдено.get(имя))

    def поддельный_popen(cmd, **kwargs):
        запущено.append(cmd)
        return процессы[len(запущено) - 1]

    monkeypatch.setattr(subprocess, "Popen", поддельный_popen)
    return запущено


URL = "https://example.com"
FF, XDG = "/usr/bin/firefox", "/usr/bin/xdg-open"


def test_firefox_ещё_стартует_считаем_запуск_успешным(monkeypatch):
    запущено = подменить_запуск(monkeypatch, {"firefox": FF, "xdg-open": XDG}, [ПоддельныйПроцесс(running=True)])
    open_site.open_in_browser(URL)
    assert запущено == [[FF, URL]], f"ожидали только запуск firefox, было {запущено}"


def test_firefox_передал_ссылку_уже_открытому_окну_и_завершился_с_кодом_0(monkeypatch):
    запущено = подменить_запуск(monkeypatch, {"firefox": FF}, [ПоддельныйПроцесс(code=0)])
    open_site.open_in_browser(URL)
    assert запущено == [[FF, URL]]


def test_firefox_упал_пробуем_xdg_open(monkeypatch):
    запущено = подменить_запуск(monkeypatch, {"firefox": FF, "xdg-open": XDG},
                                [ПоддельныйПроцесс(code=1), ПоддельныйПроцесс(running=True)])
    open_site.open_in_browser(URL)
    assert запущено == [[FF, URL], [XDG, URL]], f"ожидали firefox, затем xdg-open; было {запущено}"


def test_firefox_не_установлен_используется_xdg_open(monkeypatch):
    запущено = подменить_запуск(monkeypatch, {"xdg-open": XDG}, [ПоддельныйПроцесс(code=0)])
    open_site.open_in_browser(URL)
    assert запущено == [[XDG, URL]]


def test_ничего_не_сработало_будет_понятная_ошибка(monkeypatch):
    подменить_запуск(monkeypatch, {"firefox": FF, "xdg-open": XDG},
                     [ПоддельныйПроцесс(code=1), ПоддельныйПроцесс(code=2)])
    with pytest.raises(open_site.BrowserError):
        open_site.open_in_browser(URL)


def test_нет_ни_firefox_ни_xdg_open_будет_понятная_ошибка(monkeypatch):
    подменить_запуск(monkeypatch, {}, [])
    with pytest.raises(open_site.BrowserError):
        open_site.open_in_browser(URL)


# =============================================================================
# 5. РАЗБОР ОТВЕТА GOOGLE (обход страницы Redirect Notice)
# =============================================================================
УВЕДОМЛЕНИЕ = "https://www.google.com/url?q=https://soundcloud.com/cok7vs7piyh5&sa=U&ved=2ahUKE"
ПОИСК = "https://www.google.com/search?q=x&btnI=1"


def подменить_сеть(*ответы):
    """Каждый запрос получает следующий Location из списка; когда список кончился — нет редиректа."""
    очередь, были = list(ответы), []

    def fetch(url):
        были.append(url)
        return (302, очередь.pop(0)) if очередь else (200, None)

    fetch.были = были
    return fetch


def test_страница_redirect_notice_заменяется_настоящим_сайтом():
    # Google ответил: …/url?q=https://soundcloud.com/… -> ждём сам soundcloud, а не страницу-уведомление
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(УВЕДОМЛЕНИЕ)) == "https://soundcloud.com/cok7vs7piyh5"


def test_закодированный_адрес_сайта_декодируется():
    ответ = "/url?q=https%3A%2F%2Fexample.org%2Fa%3Fb%3D1%26c%3D2&sa=U"
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(ответ)) == "https://example.org/a?b=1&c=2"


def test_адрес_в_параметре_url_а_не_q():
    ответ = "https://www.google.com/url?sa=t&url=https://example.org/page"
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(ответ)) == "https://example.org/page"


def test_google_сразу_редиректит_на_сайт():
    assert REAL_RESOLVE(ПОИСК, подменить_сеть("https://www.python.org/")) == "https://www.python.org/"


def test_сначала_переход_внутри_google_потом_уведомление():
    fetch = подменить_сеть("/search?q=x&btnI=2", УВЕДОМЛЕНИЕ)
    assert REAL_RESOLVE(ПОИСК, fetch) == "https://soundcloud.com/cok7vs7piyh5"
    assert fetch.были[1] == "https://www.google.com/search?q=x&btnI=2", "второй запрос должен идти по новой ссылке"


СЕРВИСЫ_GOOGLE = [
    ("https://www.google.com/url?q=https://mail.google.com/", "https://mail.google.com/", "Gmail через уведомление"),
    ("https://www.google.com/url?q=https://www.google.com/maps", "https://www.google.com/maps", "Карты через уведомление"),
    ("https://mail.google.com/", "https://mail.google.com/", "Gmail напрямую"),
    ("https://drive.google.com/drive/", "https://drive.google.com/drive/", "Диск напрямую"),
]


@pytest.mark.parametrize("ответ, ожидаем, _имя", СЕРВИСЫ_GOOGLE, ids=[s[2] for s in СЕРВИСЫ_GOOGLE])
def test_сервисы_самого_google_открываются_как_обычные_сайты(ответ, ожидаем, _имя):
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(ответ)) == ожидаем


НЕПОДХОДЯЩИЕ = [
    ("https://consent.google.com/ml?continue=https://www.google.com/search", "окно согласия на cookie"),
    ("https://consent.google.bg/m?continue=x", "окно согласия (болгарский домен)"),
    ("https://www.google.com/sorry/index?continue=x", "капча"),
    ("javascript:alert(1)", "javascript: вместо ссылки"),
    ("file:///etc/passwd", "file: вместо ссылки"),
    ("https://www.google.com/url?q=javascript:alert(1)", "javascript: внутри q"),
    ("https://www.google.com/url?q=file:///etc/passwd", "file: внутри q"),
    ("https://www.google.com/url?q=https://www.google.com/search?q=y", "q ведёт обратно в поиск Google"),
    ("https://www.google.com/url?sa=U", "в уведомлении нет адреса сайта"),
]


@pytest.mark.parametrize("ответ", [n[0] for n in НЕПОДХОДЯЩИЕ], ids=[n[1] for n in НЕПОДХОДЯЩИЕ])
def test_неподходящий_ответ_значит_запасной_вариант(ответ):
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(ответ)) is None


def test_ответ_без_редиректа_значит_запасной_вариант():
    assert REAL_RESOLVE(ПОИСК, подменить_сеть()) is None


def test_бесконечные_редиректы_значит_запасной_вариант():
    assert REAL_RESOLVE(ПОИСК, подменить_сеть(*["/search?q=x"] * 20)) is None


@pytest.mark.parametrize("c", СЛУЧАИ[:60], ids=ids(СЛУЧАИ[:60]))
def test_найденный_сайт_открывается_вместо_ссылки_google(c):
    открыто = []
    итог = open_site.open_first_result(c.ввод, opener=открыто.append, resolver=lambda url: "https://example.org/found")
    assert итог == "https://example.org/found" and открыто == ["https://example.org/found"]


# =============================================================================
# 6. НАСТОЯЩИЙ HTTP-ЗАПРОС К ЛОКАЛЬНОМУ СЕРВЕРУ (проверка, что редирект не выполняется)
# =============================================================================
class ЛокальныйGoogle(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assert "Firefox" in self.headers.get("User-Agent", ""), "скрипт должен представляться браузером"
        self.send_response(302)
        self.send_header("Location", УВЕДОМЛЕНИЕ)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def локальный_google():
    сервер = http.server.HTTPServer(("127.0.0.1", 0), ЛокальныйGoogle)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{сервер.server_port}/search?q=x&btnI=1"
    сервер.shutdown()


def test_реальный_http_запрос_читает_location_и_не_переходит_по_нему(локальный_google):
    assert open_site._fetch_location(локальный_google) == (302, УВЕДОМЛЕНИЕ)


def test_реальный_http_запрос_до_настоящего_сайта(локальный_google):
    assert REAL_RESOLVE(локальный_google) == "https://soundcloud.com/cok7vs7piyh5"


def test_нет_соединения_не_роняет_программу():
    assert open_site._fetch_location("http://127.0.0.1:1/") == (None, None)


# =============================================================================
# 7. ТЕРМИНАЛЬНАЯ ЧАСТЬ (main)
# =============================================================================
def test_main_с_аргументами_dry_run_печатает_ссылку(capsys):
    assert open_site.main(["--dry-run", "ютуб", "музыка"]) == 0
    вывод = capsys.readouterr().out.strip()
    assert parse_qs(urlparse(вывод).query)["q"] == ["ютуб музыка"], f"в ссылке не тот запрос: {вывод}"


def test_main_диалог_пустая_строка_пропускается_выход_закрывает(monkeypatch, capsys):
    ответы = iter(["", "  ютуб  ", "выход"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(ответы))
    assert open_site.main(["--dry-run"]) == 0
    ссылки = [l for l in capsys.readouterr().out.splitlines() if l.startswith("https://")]
    assert len(ссылки) == 1, f"ожидали 1 ссылку (для «ютуб»), получили {len(ссылки)}"


def test_main_печатает_адрес_найденного_сайта(monkeypatch, capsys):
    monkeypatch.setattr(open_site, "resolve_target", lambda url: "https://example.org/found")
    monkeypatch.setattr(open_site, "open_in_browser", lambda url: None)
    assert open_site.main(["ютуб"]) == 0
    assert "Открываю: https://example.org/found" in capsys.readouterr().out


def test_main_сообщает_об_ошибке_браузера(capsys):
    def сломанный_браузер(url):
        raise open_site.BrowserError("нет браузера")

    assert open_site._handle("ютуб", сломанный_браузер, dry_run=False) is False
    assert "Ошибка" in capsys.readouterr().out


# =============================================================================
# 8. ПОИСК ВНУТРИ САЙТА: динамическое определение поиска со страницы сайта
# =============================================================================
class ИмитацияGoogle:
    """Имитирует Google. Ответы задаются как {текст запроса: URL результата}."""

    def __init__(self, ответы=None, по_умолчанию="https://example.org/result"):
        self.ответы = ответы or {}
        self.по_умолчанию = по_умолчанию
        self.запросы = []

    def __call__(self, url):
        параметры = parse_qs(urlparse(url).query)
        q = параметры["q"][0]
        self.запросы.append(q)
        адрес = self.ответы.get(q, self.по_умолчанию)
        if адрес is None:
            return 200, None
        return 302, f"https://www.google.com/url?q={quote(адрес, safe='')}&sa=U"

    def резолвер(self):
        return lambda url: REAL_RESOLVE(url, fetch=self)


def finder_с_шаблоном(template, source="form", calls=None):
    """Фабрика finder для тестирования make_plan без настоящей сети."""
    def finder(url):
        if calls is not None:
            calls.append(url)
        return (template, source) if template else None
    return finder


# ---- Разбор «название [запрос]» ---------------------------------------------------------------
РАЗБОР = [
    ("саундклауд [9mice]", ("саундклауд", "9mice")),
    ("саундклауд[9mice]", ("саундклауд", "9mice")),
    ("  саунд   клауд  [  linkin   park ]  ", ("саунд клауд", "linkin park")),
    ("\u00a0ютуб\t[\tрок\u00a0]", ("ютуб", "рок")),
    ("ютуб", ("ютуб", None)),
    ("  ютуб   музыка ", ("ютуб музыка", None)),
    ("ютуб []", ("ютуб", "")),
    ("ютуб [   ]", ("ютуб", "")),
    ("[9mice]", ("", "9mice")),
    ("[]", ("", "")),
    ("", ("", None)),
    ("гитхаб [arr[0] fix]", ("гитхаб", "arr[0] fix")),
    ("ютуб [9mice", ("ютуб", "9mice")),
    ("ютуб [a] хвост", ("ютуб", "a")),
    ("ютуб [a] и [b]", ("ютуб", "a] и [b")),
    ("гитхаб [c++ templates]", ("гитхаб", "c++ templates")),
    ("гитхаб [a&b=c #1 100%]", ("гитхаб", "a&b=c #1 100%")),
    ("hugging face [bert]", ("hugging face", "bert")),
]


@pytest.mark.parametrize("ввод, ожидаем", РАЗБОР, ids=[f"{в!r} -> {о}" for в, о in РАЗБОР])
def test_разбор_названия_и_запроса(ввод, ожидаем):
    assert open_site.parse_input(ввод) == ожидаем


ВАРИАНТЫ_СКОБОК = [
    ("как есть", lambda название, запрос: f"{название} [{запрос}]"),
    ("без пробела перед скобкой", lambda название, запрос: f"{название}[{запрос}]"),
    ("пробелы внутри и по краям", lambda название, запрос: f"  {название}   [  {запрос}  ]  "),
    ("табы и неразрывные пробелы", lambda название, запрос: f"\u00a0{название}\t[\t{запрос}\u00a0] "),
]


@pytest.mark.parametrize("поиск", ["9mice", "linkin park", "c++ templates", "ёлка", "a&b=c #1 100%"])
@pytest.mark.parametrize("название", ["саундклауд", "саунд клауд", "hugging face", "яндекс почта", "a b c d"])
@pytest.mark.parametrize("вариант", [v[0] for v in ВАРИАНТЫ_СКОБОК])
def test_любой_способ_набора_даёт_то_же_название_и_запрос(название, поиск, вариант):
    подготовить = dict(ВАРИАНТЫ_СКОБОК)[вариант]
    assert open_site.parse_input(подготовить(название, поиск)) == (название, поиск)


@pytest.mark.parametrize("ввод", ["[]", "  [  ]  ", "[ ]", "\t[\u00a0]\t", "", "  \u00a0 \t "])
def test_пустой_ввод_и_пустые_скобки_ничего_не_открывают(ввод):
    открыто = []
    google = ИмитацияGoogle()
    assert open_site.make_plan(ввод, resolver=google.резолвер(), finder=lambda _: None) is None
    assert open_site.open_first_result(ввод, opener=открыто.append) is None
    assert открыто == []


# ---- Планирование обычного запроса и поиска внутри сайта -------------------------------------
def test_без_скобок_это_обычный_запрос_и_в_google_уходит_один_запрос():
    google = ИмитацияGoogle({"python в docker": "https://docs.python.org/3/"})
    plan = open_site.make_plan("  python   в docker ", resolver=google.резолвер())
    assert (plan.kind, plan.url, plan.site, plan.term) == ("plain", "https://docs.python.org/3/", None, None)
    assert google.запросы == ["python в docker"]


def test_скобки_без_названия_это_обычный_поиск_по_тексту_в_скобках():
    google = ИмитацияGoogle({"погода в софии": "https://www.meteo.bg/"})
    plan = open_site.make_plan("[погода в софии]", resolver=google.резолвер())
    assert (plan.kind, plan.url) == ("plain", "https://www.meteo.bg/")
    assert google.запросы == ["погода в софии"]


def test_пустые_скобки_после_названия_просто_открывают_сайт():
    google = ИмитацияGoogle({"саундклауд": "https://soundcloud.com/"})
    plan = open_site.make_plan("саундклауд []", resolver=google.резолвер(), finder=lambda _: None)
    assert (plan.kind, plan.url, plan.site) == ("site_home", "https://soundcloud.com/", "soundcloud.com")
    assert google.запросы == ["саундклауд"]


def test_google_не_нашёл_сайт_и_получается_запасная_ссылка():
    google = ИмитацияGoogle(по_умолчанию=None)
    plan = open_site.make_plan("саундклауд [9mice]", resolver=google.резолвер(), finder=lambda _: None)
    assert plan.kind == "fallback"
    assert parse_qs(urlparse(plan.url).query)["q"] == ["саундклауд 9mice"]
    assert google.запросы == ["саундклауд"]


def test_если_поиск_на_сайте_найден_открывается_его_шаблон():
    google = ИмитацияGoogle({"саундклауд": "https://soundcloud.com/"})
    вызовы_finder = []
    finder = finder_с_шаблоном("https://soundcloud.com/search?q={searchTerms}", "form", вызовы_finder)
    plan = open_site.make_plan("саундклауд [9mice]", resolver=google.резолвер(), finder=finder)
    assert (plan.kind, plan.url, plan.site, plan.term, plan.source) == (
        "site_search", "https://soundcloud.com/search?q=9mice", "soundcloud.com", "9mice", "form")
    assert вызовы_finder == ["https://soundcloud.com/"]
    assert google.запросы == ["саундклауд"]


def test_поиск_на_сайте_с_кириллицей_и_спецсимволами_кодируется():
    google = ИмитацияGoogle({"сайт": "https://example.org/"})
    finder = finder_с_шаблоном("https://example.org/search?q={searchTerms}")
    plan = open_site.make_plan("сайт [ёлка & c++]", resolver=google.резолвер(), finder=finder)
    params = parse_qs(urlparse(plan.url).query)
    assert params["q"] == ["ёлка & c++"]
    assert plan.url.isascii() and " " not in plan.url


def test_если_на_сайте_нет_обнаруженного_поиска_используется_google_site():
    google = ИмитацияGoogle({
        "сайт": "https://example.org/",
        "site:example.org pandas": "https://example.org/search-result",
    })
    plan = open_site.make_plan("сайт [pandas]", resolver=google.резолвер(), finder=lambda _: None)
    assert (plan.kind, plan.url, plan.site, plan.term) == (
        "google_site", "https://example.org/search-result", "example.org", "pandas")
    assert google.запросы == ["сайт", "site:example.org pandas"]


def test_если_google_не_ответил_на_site_query_используется_резервная_ссылка():
    google = ИмитацияGoogle({"сайт": "https://example.org/", "site:example.org x": None})
    plan = open_site.make_plan("сайт [x]", resolver=google.резолвер(), finder=lambda _: None)
    assert plan.kind == "fallback"
    assert (plan.site, plan.term) == ("example.org", "x")
    assert parse_qs(urlparse(plan.url).query)["q"] == ["site:example.org x"]
    assert google.запросы == ["сайт", "site:example.org x"]


def test_запрос_в_скобках_не_добавляется_к_первому_запросу_в_google():
    google = ИмитацияGoogle({"youtube": "https://www.youtube.com/"})
    finder = finder_с_шаблоном("https://www.youtube.com/results?search_query={searchTerms}")
    open_site.make_plan("youtube [linkin park]", resolver=google.резолвер(), finder=finder)
    assert google.запросы == ["youtube"]


@pytest.mark.parametrize("host, ожидаем", [
    ("https://www.youtube.com/watch?v=1", "youtube.com"),
    ("https://m.youtube.com/", "youtube.com"),
    ("https://mobile.twitter.com/x", "twitter.com"),
    ("https://soundcloud.com", "soundcloud.com"),
    ("HTTPS://WWW.SoundCloud.COM/", "soundcloud.com"),
    ("https://docs.python.org/3/", "docs.python.org"),
    ("http://ya.ru", "ya.ru"),
    ("https://example.org:8080/a", "example.org"),
    ("https://ru.m.wikipedia.org/", "ru.m.wikipedia.org"),
    ("https://evilgithub.com/", "evilgithub.com"),
])
def test_домен_сайта_из_адреса(host, ожидаем):
    assert open_site.site_host(host) == ожидаем


@pytest.mark.parametrize("template, term, ожидаем", [
    ("https://example.org/search?q={searchTerms}", "linkin park", "https://example.org/search?q=linkin+park"),
    ("https://example.org/search?q={searchTerms}", "c++ templates", "https://example.org/search?q=c%2B%2B+templates"),
    ("https://example.org/search/{searchTerms}", "linkin park", "https://example.org/search/linkin%20park"),
    ("https://example.org/search/{searchTerms}", "a/b", "https://example.org/search/a%2Fb"),
    ("https://example.org/#search/{searchTerms}", "linkin park", "https://example.org/#search/linkin%20park"),
])
def test_подстановка_запроса_в_шаблон(template, term, ожидаем):
    assert open_site.fill_template(template, term) == ожидаем


# ---- Обнаружение поискового шаблона на HTML-странице -----------------------------------------
def test_поиск_через_opensearch():
    page_url = "https://example.org/"
    html = '<link rel="search" type="application/opensearchdescription+xml" href="/opensearch.xml">'
    xml = '''<?xml version="1.0"?>
    <OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
      <Url type="text/html" template="https://example.org/search?q={searchTerms}" />
    </OpenSearchDescription>'''
    вызовы = []
    def fetch(url):
        вызовы.append(url)
        if url == page_url:
            return page_url, html
        if url == "https://example.org/opensearch.xml":
            return url, xml
        return None
    assert open_site.find_search_template(page_url, fetch=fetch) == (
        "https://example.org/search?q={searchTerms}", "opensearch")
    assert вызовы == [page_url, "https://example.org/opensearch.xml"]


def test_поиск_через_jsonld_searchaction():
    page_url = "https://example.org/"
    html = '''<script type="application/ld+json">
    {"@context":"https://schema.org","@type":"WebSite",
     "potentialAction":{"@type":"SearchAction",
     "target":"https://example.org/search?q={search_term_string}",
     "query-input":"required name=search_term_string"}}
    </script>'''
    assert open_site.find_search_template(page_url, fetch=lambda url: (url, html)) == (
        "https://example.org/search?q={searchTerms}", "jsonld")


def test_поиск_через_обычную_get_форму():
    page_url = "https://example.org/"
    html = '<form method="get" action="/search"><input type="search" name="q"></form>'
    assert open_site.find_search_template(page_url, fetch=lambda url: (url, html)) == (
        "https://example.org/search?q={searchTerms}", "form")


def test_get_форма_сохраняет_hidden_параметры_и_кодирует_действие():
    html = ('<form action="/search" method="get">'
            '<input type="hidden" name="type" value="all">'
            '<input type="search" name="query"></form>')
    template, source = open_site._template_from_page(
        "https://example.org/", html, lambda url: None)
    assert source == "form"
    assert template == "https://example.org/search?type=all&query={searchTerms}"


def test_post_форма_не_принимается_за_поиск_get():
    html = '<form method="post" action="/search"><input type="search" name="q"></form>'
    assert open_site.find_search_template(
        "https://example.org/", fetch=lambda url: (url, html)) is None


def test_форма_входа_не_принимается_за_поиск():
    html = ('<form id="login-form" action="/login" method="get">'
            '<input type="text" name="q"></form>')
    assert open_site.find_search_template(
        "https://example.org/", fetch=lambda url: (url, html)) is None


def test_opensearch_не_принимает_xml_с_entity_или_doctype():
    xml = '<!DOCTYPE foo [<!ENTITY x "boom">]><OpenSearchDescription><Url type="text/html" template="https://example.org/?q={searchTerms}"/></OpenSearchDescription>'
    assert open_site._opensearch_template(xml, "https://example.org/search.xml") is None


def test_шаблон_должен_иметь_ровно_один_searchTerms():
    assert not open_site._is_valid_template("https://example.org/search?q=x")
    assert not open_site._is_valid_template("https://example.org/?q={searchTerms}&x={searchTerms}")
    assert open_site._is_valid_template("https://example.org/?q={searchTerms}")


def test_поиск_на_странице_сначала_проверяет_текущий_url_потом_главную():
    page_url = "https://example.org/category/page"
    root_url = "https://example.org/"
    calls = []
    def fetch(url):
        calls.append(url)
        if url == page_url:
            return url, "<html><body>нет поиска</body></html>"
        if url == root_url:
            return url, '<form action="/search" method="get"><input name="q" type="search"></form>'
        return None
    assert open_site.find_search_template(page_url, fetch=fetch) == (
        "https://example.org/search?q={searchTerms}", "form")
    assert calls[:2] == [page_url, root_url]


def test_неудача_загрузки_страницы_не_роняет_поиск():
    assert open_site.find_search_template("https://example.org/", fetch=lambda _: None) is None


def test_кэш_запоминает_результат_обычного_поиска_шаблона(monkeypatch):
    url = "https://cache-example.invalid/"
    html = '<form action="/search" method="get"><input name="q" type="search"></form>'
    calls = []
    monkeypatch.setattr(open_site, "_fetch_page", lambda requested: (calls.append(requested) or (requested, html)))
    open_site.clear_cache()
    try:
        first = open_site.find_search_template(url)
        second = open_site.find_search_template(url)
        assert first == second == ("https://cache-example.invalid/search?q={searchTerms}", "form")
        assert calls == [url]
    finally:
        open_site.clear_cache()


# ---- Терминальная часть для синтаксиса «сайт [запрос]» ---------------------------------------
def test_main_саундклауд_9mice_печатает_сайт_и_открывает_поиск(monkeypatch, capsys):
    google = ИмитацияGoogle({"саундклауд": "https://soundcloud.com/"})
    monkeypatch.setattr(open_site, "resolve_target", google.резолвер())
    monkeypatch.setattr(open_site, "find_search_template",
                        lambda url: ("https://soundcloud.com/search?q={searchTerms}", "form"))
    открыто = []
    monkeypatch.setattr(open_site, "open_in_browser", открыто.append)
    assert open_site.main(["саундклауд", "[9mice]"]) == 0
    вывод = capsys.readouterr().out
    assert открыто == ["https://soundcloud.com/search?q=9mice"]
    assert "soundcloud.com" in вывод and "9mice" in вывод
    assert google.запросы == ["саундклауд"]


def test_main_один_аргумент_в_кавычках_работает_так_же(monkeypatch):
    google = ИмитацияGoogle({"ютуб": "https://www.youtube.com/"})
    monkeypatch.setattr(open_site, "resolve_target", google.резолвер())
    monkeypatch.setattr(open_site, "find_search_template",
                        lambda url: ("https://www.youtube.com/results?search_query={searchTerms}", "form"))
    открыто = []
    monkeypatch.setattr(open_site, "open_in_browser", открыто.append)
    assert open_site.main(["ютуб [linkin park]"]) == 0
    assert открыто == ["https://www.youtube.com/results?search_query=linkin+park"]


def test_main_сайт_без_найденного_поиска_использует_google_site(monkeypatch, capsys):
    google = ИмитацияGoogle({"каггл": "https://www.kaggle.com/",
                             "site:kaggle.com titanic": "https://www.kaggle.com/c/titanic"})
    monkeypatch.setattr(open_site, "resolve_target", google.резолвер())
    monkeypatch.setattr(open_site, "find_search_template", lambda url: None)
    открыто = []
    monkeypatch.setattr(open_site, "open_in_browser", открыто.append)
    assert open_site.main(["каггл", "[titanic]"]) == 0
    вывод = capsys.readouterr().out
    assert открыто == ["https://www.kaggle.com/c/titanic"]
    assert "kaggle.com" in вывод and "titanic" in вывод and "Google" in вывод


def test_main_google_молчит_предупреждает_и_открывает_ссылку_через_google(monkeypatch, capsys):
    monkeypatch.setattr(open_site, "resolve_target", ИмитацияGoogle(по_умолчанию=None).резолвер())
    monkeypatch.setattr(open_site, "find_search_template", lambda url: None)
    открыто = []
    monkeypatch.setattr(open_site, "open_in_browser", открыто.append)
    assert open_site.main(["саундклауд [9mice]"]) == 0
    assert parse_qs(urlparse(открыто[0]).query)["q"] == ["саундклауд 9mice"]
    assert "не удалось" in capsys.readouterr().out


def test_main_dry_run_не_ходит_в_сеть_даже_для_сайт_плюс_запрос(monkeypatch, capsys):
    def запрещено(url):
        raise AssertionError("в режиме --dry-run сеть не нужна")
    monkeypatch.setattr(open_site, "resolve_target", запрещено)
    assert open_site.main(["--dry-run", "саундклауд", "[9mice]"]) == 0
    параметры = parse_qs(urlparse(capsys.readouterr().out.strip()).query)
    assert параметры["q"] == ["саундклауд 9mice"] and параметры["btnI"] == ["1"]


def test_main_пустые_скобки_в_аргументах_это_пустой_запрос(capsys):
    assert open_site.main(["--dry-run", "[]"]) == 1
    assert "Пустой запрос" in capsys.readouterr().out


def test_main_диалог_со_скобками(monkeypatch, capsys):
    ответы = iter(["саундклауд [9mice]", "", "[]", "  ютуб [  рок ]  ", "выход"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(ответы))
    assert open_site.main(["--dry-run"]) == 0
    ссылки = [line for line in capsys.readouterr().out.splitlines() if line.startswith("https://")]
    assert [parse_qs(urlparse(line).query)["q"][0] for line in ссылки] == ["саундклауд 9mice", "ютуб рок"]


def test_main_ошибка_браузера_при_поиске_на_сайте(capsys):
    def сломанный_браузер(url):
        raise open_site.BrowserError("нет браузера")
    assert open_site._handle("ютуб [рок]", сломанный_браузер, dry_run=False) is False
    assert "Ошибка" in capsys.readouterr().out
