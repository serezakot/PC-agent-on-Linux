"""Тесты поиска и связки с моделью: без модели, сети и браузера (всё подменено)."""
import pytest

import gogle_search as g

HOME = lambda url: "https://www.youtube.com/"
FINDER = lambda url: ("https://www.youtube.com/results?search_query={searchTerms}", "form")


def res(route, site="", query="", text="", lang="none", **extra):
    return {"route": route, "site": site, "query": query, "text": text, "lang": lang, "fallback": False, "reason": "", **extra}


def test_сайт_и_запрос_ищет_внутри_сайта():
    p = g.make_plan("ютуб", "linkin park", HOME, FINDER)
    assert p.kind == "site_search" and p.url == "https://www.youtube.com/results?search_query=linkin+park"
    assert p.site == "youtube.com" and p.source == "form"


def test_только_сайт_открывает_сайт():
    p = g.make_plan("ютуб", "", HOME, FINDER)
    assert (p.kind, p.url) == ("site_home", "https://www.youtube.com/")


def test_только_запрос_это_обычный_поиск_в_гугле():
    p = g.make_plan("", "рецепт борща", HOME, FINDER)
    assert p.kind == "plain"
    assert g.make_plan("", "рецепт борща", lambda u: None, FINDER).kind == "fallback"


def test_пусто_искать_нечего():
    assert g.make_plan("", "", HOME, FINDER) is None
    assert g.make_plan("  ", None, HOME, FINDER) is None


def test_у_сайта_нет_поиска_идёт_через_гугл_site():
    seen = []

    def resolver(url):
        seen.append(url)
        return "https://example.com/" if len(seen) == 1 else "https://example.com/found"

    p = g.make_plan("пример", "тест", resolver, lambda u: None)
    assert p.kind == "google_site" and "site%3Aexample.com" in seen[1]


def test_сайт_не_нашёлся_запасной_вариант_содержит_и_сайт_и_запрос():
    p = g.make_plan("непонятный сайт", "запрос", lambda u: None, FINDER)
    assert p.kind == "fallback" and "%D0%BD%D0%B5%D0%BF%D0%BE%D0%BD%D1%8F%D1%82%D0%BD%D1%8B%D0%B9" in p.url


def test_больше_нет_синтаксиса_со_скобками():
    assert not hasattr(g, "parse_input")


def handle(result, **kw):
    opened = []
    out = g.handle_phrase("фраза", lambda _: result, opened.append, HOME, FINDER)
    return out, opened


def test_browser_идёт_через_модель_в_поиск():
    (result, plan), opened = handle(res("browser", "ютуб", "linkin park"))
    assert opened == ["https://www.youtube.com/results?search_query=linkin+park"] and plan.kind == "site_search"


@pytest.mark.parametrize("route", ["translate", "llm"])
def test_не_поиск_браузер_не_открывается(route):
    (result, plan), opened = handle(res(route, text="привет", lang="en"))
    assert plan is None and opened == []


def test_browser_без_сайта_и_запроса_ничего_не_открывает():
    (result, plan), opened = handle(res("browser"))
    assert plan is None and opened == []


def test_open_first_result_принимает_сайт_и_запрос():
    opened = []
    url = g.open_first_result("ютуб", "рок", opened.append, HOME, FINDER)
    assert url == opened[0] and url.endswith("search_query=%D1%80%D0%BE%D0%BA")
    assert g.open_first_result("", "", opened.append, HOME, FINDER) is None


def test_нет_модели_понятная_ошибка(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "ML_DIR", tmp_path)
    with pytest.raises(g.ModelError, match="нет обученной модели"):
        g.model_parse("открой ютуб")


def test_диалог_печатает_решение_модели_и_ссылку(monkeypatch, capsys):
    monkeypatch.setattr(g, "model_parse", lambda p: res("browser", "ютуб", "рок"))
    monkeypatch.setattr(g, "resolve_target", lambda u, fetch=None: "https://www.youtube.com/")
    monkeypatch.setattr(g, "find_search_template", lambda u, fetch=None: FINDER(u))
    assert g.main(["--dry-run", "открой", "ютуб", "и", "найди", "рок"]) == 0
    out = capsys.readouterr().out
    assert "модель: browser" in out and "сайт=ютуб" in out and "запрос=рок" in out


def test_страховка_модели_видна_в_выводе(monkeypatch, capsys):
    monkeypatch.setattr(g, "model_parse", lambda p: {**res("llm"), "fallback": True, "reason": "не уверена в намерении (0.55)"})
    assert g.main(["--dry-run", "что-то", "непонятное"]) == 0
    assert "страховка" in capsys.readouterr().out


def test_без_модели_main_возвращает_ошибку(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(g, "ML_DIR", tmp_path)
    assert g.main(["--dry-run", "открой", "ютуб"]) == 1
    assert "нет обученной модели" in capsys.readouterr().out
