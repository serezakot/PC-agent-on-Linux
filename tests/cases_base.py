"""Основа корпуса: описание одного тестового случая и короткие конструкторы.

Каждый случай — это фраза, как её сказал бы человек, и то, что ДОЛЖНА вернуть модель:
    browser   -> site, query   (site пустой = поиск в Google; query пустой = просто открыть сайт)
    translate -> text, lang    (lang — код из ml_spec.LANGS, «none» если язык не назван)
    llm       -> ничего: фраза не для браузера и не для переводчика
Куски site / query / text — это ДОСЛОВНО слова из фразы (так работает разметка слов: модель слова не меняет).
tag — группа для отчёта; «reach» — формулировки за пределами правил ml_spec (проверка обобщения, в зачёт не идут).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    phrase: str
    route: str
    site: str = ""
    query: str = ""
    text: str = ""
    lang: str = "none"
    tag: str = ""


def SQ(phrase, site, query, tag="site_query"):
    return Case(phrase, "browser", site=site, query=query, tag=tag)


def SO(phrase, site, tag="site_only"):
    return Case(phrase, "browser", site=site, tag=tag)


def GO(phrase, query, tag="google"):
    return Case(phrase, "browser", query=query, tag=tag)


def TR(phrase, text, lang, tag="translate"):
    return Case(phrase, "translate", text=text, lang=lang, tag=tag)


def LL(phrase, tag="llm"):
    return Case(phrase, "llm", tag=tag)


def expected_kind(c: Case):
    """Какой вид плана должен получиться у gogle_search для этого случая (None — браузер не открывается)."""
    if c.route == "translate":
        return "translate"
    if c.route != "browser":
        return None
    if not c.site:
        return "google_search"
    return "site_search" if c.query else "site_home"
