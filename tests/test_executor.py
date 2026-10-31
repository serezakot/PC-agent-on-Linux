"""Тесты исполнителя (gogle_search.py) БЕЗ модели и без сети: модель подменяется «ответом по эталону».

Запуск из корня проекта:
    python3 -m unittest discover -s tests -v          (или: python3 tests/test_executor.py)
Проверяет две главные поломки и всё вокруг них:
    «загугли …»  -> страница результатов Google (в ссылке НЕТ btnI=1, «первый сайт» не открывается);
    «переведи …» -> Google Переводчик с подставленными текстом и языком.
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

import gogle_search as g          # noqa: E402
import ml_home                     # noqa: E402
import ml_spec                     # noqa: E402
from cases import all_cases, expected_kind   # noqa: E402

FAKE_HOMES = {}


def _home(name: str) -> str:
    """Любое имя сайта -> «найденная» главная. Для названий Google — главная Google."""
    key = name.casefold().strip()
    if key in ("гугл", "google"):
        return "https://www.google.com/"
    if key in ("гугл карты", "google maps"):
        return "https://www.google.com/maps"
    slug = "".join(ch for ch in key if ch.isalnum()) or "site"
    return f"https://www.{slug}.example/"


class FakeNet:
    """Подмена resolver / finder: считает вызовы, сети не касается."""

    def __init__(self, resolvable=True, searchable=True, google_ok=True):
        self.resolvable, self.searchable, self.google_ok = resolvable, searchable, google_ok
        self.resolver_calls, self.finder_calls = [], []

    def resolver(self, lucky_url):
        q = parse_qs(urlparse(lucky_url).query).get("q", [""])[0]
        self.resolver_calls.append(q)
        if q.startswith("site:"):
            return "https://found.example/result" if self.google_ok else None
        return _home(q) if self.resolvable else None

    def finder(self, page_url):
        self.finder_calls.append(page_url)
        if not self.searchable:
            return None
        host = urlparse(page_url).hostname
        return (f"https://{host}/search?q={g.TERM}", "opensearch")


def q_of(url, key="q"):
    return parse_qs(urlparse(url).query).get(key, [""])[0]


class TestGoogleSearchUrl(unittest.TestCase):
    def test_search_url_is_results_page_not_lucky(self):
        url = g.search_url("рецепт борща")
        self.assertTrue(url.startswith("https://www.google.com/search?"))
        self.assertNotIn("btnI", url)
        self.assertEqual(q_of(url), "рецепт борща")

    def test_lucky_url_still_has_btnI_for_site_resolution(self):
        self.assertIn("btnI=1", g.build_url("ютуб"))

    def test_special_characters_survive(self):
        for text in ['a&b=c#d?e', "c++ vs c#", 'он сказал "привет"', "100% гарантия", "a+b", "кофе 😀 чай"]:
            with self.subTest(text=text):
                self.assertEqual(q_of(g.search_url(text)), text)

    def test_whitespace_is_normalized(self):
        self.assertEqual(q_of(g.search_url("  как \t дела\u00a0сегодня  ")), "как дела сегодня")


class TestTranslateUrl(unittest.TestCase):
    def parts(self, text, lang="none"):
        url = g.translate_url(text, lang)
        self.assertTrue(url.startswith("https://translate.google.com/?"), url)
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    def test_basic(self):
        self.assertEqual(self.parts("как дела", "en"), {"sl": "auto", "tl": "en", "text": "как дела", "op": "translate"})

    def test_every_language_has_a_code(self):
        for code in ml_spec.LANGS:
            if code == "none":
                continue
            with self.subTest(lang=code):
                tl = self.parts("привет", code)["tl"]
                self.assertTrue(tl)
                self.assertEqual(tl, {"zh": "zh-CN", "he": "iw"}.get(code, code))

    def test_language_not_named_guessed_by_script(self):
        self.assertEqual(self.parts("привет")["tl"], "en")                 # кириллица -> английский
        self.assertEqual(self.parts("hello world")["tl"], "ru")            # латиница -> русский
        self.assertEqual(self.parts("12345")["tl"], "en")
        self.assertEqual(self.parts("привет всем my friend")["tl"], "en")  # кириллицы 10 букв, латиницы 8
        self.assertEqual(self.parts("привет good morning everyone")["tl"], "ru")   # кириллицы 6, латиницы 20

    def test_named_language_beats_guess(self):
        self.assertEqual(self.parts("привет", "ru")["tl"], "ru")

    def test_text_is_not_mangled(self):
        for text in ["я не говорю по болгарски", "открой ютуб", 'a&b=c#d?e+f', "билет стоит 250 евро", "он сказал «да»"]:
            with self.subTest(text=text):
                self.assertEqual(self.parts(text, "en")["text"], text)

    def test_spaces_are_percent20_not_plus(self):
        url = g.translate_url("как дела", "en")
        self.assertIn("%20", url)
        self.assertNotIn("+", url.split("text=")[1])

    def test_whitespace_normalized(self):
        self.assertEqual(self.parts("  два   кофе\tпожалуйста ", "it")["text"], "два кофе пожалуйста")


class TestMakePlan(unittest.TestCase):
    def test_no_site_means_real_google_search(self):          # баг №2 из жалобы: «загугли» открывал первый сайт
        net = FakeNet()
        plan = g.make_plan("", "рецепт борща", net.resolver, net.finder)
        self.assertEqual(plan.kind, "google_search")
        self.assertEqual(plan.url, g.search_url("рецепт борща"))
        self.assertNotIn("btnI", plan.url)
        self.assertEqual(net.resolver_calls, [])               # Google «Мне повезёт» даже не спрашиваем
        self.assertEqual(net.finder_calls, [])

    def test_nothing_to_do(self):
        self.assertIsNone(g.make_plan("", "", FakeNet().resolver))
        self.assertIsNone(g.make_plan("  ", " \t ", FakeNet().resolver))

    def test_site_only_opens_home(self):
        net = FakeNet()
        plan = g.make_plan("ютуб", "", net.resolver, net.finder)
        self.assertEqual((plan.kind, plan.url), ("site_home", "https://www.ютуб.example/"))
        self.assertEqual(net.finder_calls, [])

    def test_site_and_query_uses_site_search(self):
        net = FakeNet()
        plan = g.make_plan("саундклауд", "нойз мс", net.resolver, net.finder)
        self.assertEqual(plan.kind, "site_search")
        self.assertEqual(plan.source, "opensearch")
        self.assertEqual(q_of(plan.url), "нойз мс")
        self.assertEqual(urlparse(plan.url).hostname, "www.саундклауд.example")

    def test_site_without_own_search_goes_through_google_site(self):
        net = FakeNet(searchable=False)
        plan = g.make_plan("хабр", "rust", net.resolver, net.finder)
        self.assertEqual(plan.kind, "google_site")
        self.assertIn("site:хабр.example rust", net.resolver_calls)

    def test_google_unreachable_falls_back(self):
        plan = g.make_plan("ютуб", "linkin park", FakeNet(resolvable=False).resolver)
        self.assertEqual(plan.kind, "fallback")
        self.assertIn("btnI=1", plan.url)

    def test_google_down_for_site_query(self):
        plan = g.make_plan("хабр", "rust", FakeNet(searchable=False, google_ok=False).resolver, FakeNet(searchable=False).finder)
        self.assertEqual(plan.kind, "fallback")

    def test_open_google_and_search_is_plain_search(self):    # «открой гугл и найди X»
        net = FakeNet()
        plan = g.make_plan("гугл", "погода в софии", net.resolver, net.finder)
        self.assertEqual(plan.kind, "google_search")
        self.assertEqual(plan.url, g.search_url("погода в софии"))
        self.assertEqual(net.finder_calls, [])

    def test_google_maps_is_not_google_root(self):           # «гугл карты» — отдельный сервис, у него свой поиск
        net = FakeNet()
        plan = g.make_plan("гугл карты", "аптека", net.resolver, net.finder)
        self.assertEqual(plan.kind, "site_search")

    def test_open_google_alone_is_site_home(self):
        plan = g.make_plan("гугл", "", FakeNet().resolver)
        self.assertEqual(plan.kind, "site_home")

    def test_is_google_root(self):
        for url in ["https://www.google.com/", "https://google.bg/?hl=ru", "https://www.google.com", "https://www.google.com/webhp"]:
            with self.subTest(url=url):
                self.assertTrue(g._is_google_root(url))
        for url in ["https://www.google.com/maps", "https://mail.google.com/", "https://www.youtube.com/", "https://notgoogle.com/"]:
            with self.subTest(url=url):
                self.assertFalse(g._is_google_root(url))


class TestTranslatePlan(unittest.TestCase):
    def test_plan(self):
        plan = g.make_translate_plan("как дела", "en")
        self.assertEqual((plan.kind, plan.term, plan.source), ("translate", "как дела", "en"))
        self.assertEqual(plan.url, g.translate_url("как дела", "en"))

    def test_empty_text(self):
        self.assertIsNone(g.make_translate_plan("", "en"))
        self.assertIsNone(g.make_translate_plan("   ", "none"))

    def test_describe(self):
        self.assertIn("Google Переводчике", g._describe(g.make_translate_plan("привет", "de")))
        self.assertIn("Ищу в Google", g._describe(g.make_plan("", "борщ")))


def result(route, **kw):
    base = {"route": route, "site": "", "query": "", "text": "", "lang": "none", "confidence": 0.99,
            "fallback": False, "reason": "", "phrase": "x"}
    base.update(kw)
    return base


class TestHandlePhrase(unittest.TestCase):
    def run_phrase(self, res, net=None):
        net = net or FakeNet()
        opened = []
        out, plan = g.handle_phrase("фраза", lambda p: res, opened.append, net.resolver, net.finder)
        return opened, plan

    def test_translate_opens_translator(self):               # баг №1 из жалобы: «переведи» не открывал переводчик
        opened, plan = self.run_phrase(result("translate", text="как дела", lang="en"))
        self.assertEqual(opened, [g.translate_url("как дела", "en")])
        self.assertEqual(plan.kind, "translate")

    def test_translate_without_language(self):
        opened, _ = self.run_phrase(result("translate", text="hello", lang="none"))
        self.assertEqual(q_of(opened[0], "tl"), "ru")

    def test_translate_empty_text_opens_nothing(self):
        opened, plan = self.run_phrase(result("translate", text="", lang="en"))
        self.assertEqual((opened, plan), ([], None))

    def test_google_query_opens_results_page(self):
        opened, plan = self.run_phrase(result("browser", query="курс доллара"))
        self.assertEqual(opened, [g.search_url("курс доллара")])
        self.assertNotIn("btnI", opened[0])

    def test_site_and_query(self):
        opened, plan = self.run_phrase(result("browser", site="спотифай", query="хаски"))
        self.assertEqual(plan.kind, "site_search")
        self.assertEqual(len(opened), 1)

    def test_llm_opens_nothing(self):
        for fallback in (False, True):
            opened, plan = self.run_phrase(result("llm", fallback=fallback))
            self.assertEqual((opened, plan), ([], None))

    def test_browser_with_nothing_opens_nothing(self):
        self.assertEqual(self.run_phrase(result("browser")), ([], None))

    def test_unknown_route_is_ignored(self):
        self.assertEqual(self.run_phrase(result("зачем")), ([], None))

    def test_parser_error_propagates(self):
        def boom(_):
            raise g.ModelError("нет модели")
        with self.assertRaises(g.ModelError):
            g.handle_phrase("x", boom, lambda u: None)


class TestRunCli(unittest.TestCase):
    """_run — то, что видит пользователь в терминале (--dry-run: ссылка печатается, браузер не открывается)."""

    def run_cli(self, res):
        orig = g.model_parse
        g.model_parse = lambda phrase: res
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ok = g._run("любая фраза", lambda u: None, dry_run=True)
        finally:
            g.model_parse = orig
        return ok, buf.getvalue()

    def test_dry_run_translate_prints_translator_url(self):
        ok, out = self.run_cli(result("translate", text="как дела", lang="de"))
        self.assertTrue(ok)
        self.assertIn("https://translate.google.com/?sl=auto&tl=de", out)

    def test_dry_run_google_prints_results_url(self):
        ok, out = self.run_cli(result("browser", query="рецепт борща"))
        self.assertTrue(ok)
        self.assertIn("https://www.google.com/search?q=", out)
        self.assertNotIn("btnI", out)

    def test_translate_nothing_to_translate(self):
        _, out = self.run_cli(result("translate", text="", lang="en"))
        self.assertIn("Переводить нечего", out)

    def test_llm_message(self):
        _, out = self.run_cli(result("llm", fallback=True, reason="не уверена в намерении (0.80)"))
        self.assertIn("страховка", out)


class TestCorpus(unittest.TestCase):
    """Сам набор фраз должен быть правильным, иначе проверка модели ничего не стоит."""

    @classmethod
    def setUpClass(cls):
        cls.cases = all_cases()

    def test_size_and_uniqueness(self):
        self.assertGreaterEqual(len(self.cases), 1000)
        keys = [" ".join(c.phrase.casefold().replace("ё", "е").split()) for c in self.cases]
        self.assertEqual(len(keys), len(set(keys)))

    def test_fields_are_valid(self):
        for c in self.cases:
            with self.subTest(phrase=c.phrase):
                self.assertIn(c.route, ml_spec.INTENTS)
                if c.route == "translate":
                    self.assertIn(c.lang, ml_spec.LANGS)
                    self.assertTrue(c.text)
                    self.assertFalse(c.site or c.query)
                elif c.route == "browser":
                    self.assertTrue(c.site or c.query)
                    self.assertFalse(c.text)
                else:
                    self.assertFalse(c.site or c.query or c.text)

    def test_expected_chunks_are_literal_words_of_the_phrase(self):
        for c in self.cases:
            with self.subTest(phrase=c.phrase):
                words, tags = ml_spec.tag_phrase(c.phrase, c.site, c.query, c.text)
                self.assertEqual(len(words), len(tags))

    def test_corpus_obeys_where_to_search_rule(self):
        for c in self.cases:
            with self.subTest(phrase=c.phrase):
                self.assertEqual(ml_home.rule_violation(c.phrase, c.route, c.site, c.query), "")

    def test_variety(self):
        sites = {c.site.casefold() for c in self.cases if c.site}
        queries = {c.query.casefold() for c in self.cases if c.query}
        langs = {c.lang for c in self.cases if c.route == "translate"}
        texts = {c.text.casefold() for c in self.cases if c.route == "translate"}
        self.assertGreaterEqual(len(sites), 70)
        self.assertGreaterEqual(len(queries), 250)
        self.assertGreaterEqual(len(texts), 120)
        self.assertEqual(langs, set(ml_spec.LANGS))          # включая «none»: все языки перевода встречаются
        for route in ("browser", "translate", "llm"):
            self.assertGreaterEqual(sum(c.route == route for c in self.cases), 40)

    def test_every_case_executes_correctly_with_perfect_parser(self):
        """Если модель вернула эталон, исполнитель обязан открыть ровно то, что нужно."""
        bad = []
        for c in self.cases:
            net, opened = FakeNet(), []
            res = result(c.route, site=c.site, query=c.query, text=c.text, lang=c.lang)
            _, plan = g.handle_phrase(c.phrase, lambda p, res=res: res, opened.append, net.resolver, net.finder)
            kind = expected_kind(c)
            if kind is None:
                ok = plan is None and not opened
            elif kind == "google_search":
                ok = (plan.kind == kind and opened == [g.search_url(c.query)] and "btnI" not in opened[0])
            elif kind == "translate":
                ok = (plan.kind == kind and opened == [g.translate_url(c.text, c.lang)]
                      and q_of(opened[0], "text") == " ".join(c.text.split()))
            elif kind == "site_home":
                ok = plan.kind == kind and len(opened) == 1 and not (net.finder_calls)
            else:
                ok = plan.kind in ("site_search", "google_search") and len(opened) == 1
                if plan.kind == "site_search":
                    ok = ok and q_of(opened[0]) == " ".join(c.query.split())
            if not ok:
                bad.append((c.phrase, kind, plan))
        self.assertEqual(bad[:10], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
