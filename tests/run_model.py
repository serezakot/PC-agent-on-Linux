#!/usr/bin/env python3
"""Прогон корпуса через НАСТОЯЩУЮ обученную модель (нужны torch, transformers и папка ml/ml_model).

    python3 tests/run_model.py                      # весь корпус (~1200 фраз, около полминуты на CPU)
    python3 tests/run_model.py --hand               # только ручные («живые») фразы
    python3 tests/run_model.py --tag translate_tricky --show 60
    python3 tests/run_model.py --route browser --no-reach
    python3 tests/run_model.py --tsv ошибки.tsv     # ошибки в формате ml_real.tsv: ПРАВИЛЬНЫЙ разбор, готово для дообучения
    python3 tests/run_model.py --selftest           # проверить сам отчёт на «подпорченном» парсере (без модели)

Как читать итог. У фразы пять возможных исходов:
    ok            — всё верно
    страховка     — должно быть действие, а модель не уверена и отдала фразу большой LLM (медленно, но не опасно)
    ушла в llm    — должно быть действие, а модель уверенно решила «llm» (действие потеряно)
    лишнее действие — должно быть llm, а модель открыла бы браузер / переводчик (самая неприятная ошибка)
    не те данные  — действие верное, а сайт / запрос / текст / язык выделены неверно (откроется не то)
«reach»-фразы (за пределами правил ml_spec) показываются отдельно и в общий процент не входят.
"""
import argparse
import collections
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT / "ml", HERE):
    sys.path.insert(0, str(p))

from cases import all_cases           # noqa: E402
from ml_spec import norm              # noqa: E402


def same(a: str, b: str) -> bool:
    return norm(a) == norm(b)


def judge(case, res):
    """-> (исход, подробность)."""
    got = res.get("route")
    if case.route == "llm":
        return ("ok", "") if got == "llm" else ("лишнее действие", describe(res))
    if got == "llm":
        return ("страховка", res.get("reason", "")) if res.get("fallback") else ("ушла в llm", "")
    if got != case.route:
        return "не те данные", f"маршрут {got} вместо {case.route}: {describe(res)}"
    if case.route == "browser":
        if same(res.get("site", ""), case.site) and same(res.get("query", ""), case.query):
            return "ok", ""
        return "не те данные", describe(res)
    ok = same(res.get("text", ""), case.text) and res.get("lang") == case.lang
    return ("ok", "") if ok else ("не те данные", describe(res))


def describe(res):
    if res.get("route") == "browser":
        return f"browser site={res.get('site')!r} query={res.get('query')!r}"
    if res.get("route") == "translate":
        return f"translate text={res.get('text')!r} lang={res.get('lang')}"
    return str(res.get("route"))


def expected(case):
    if case.route == "browser":
        return f"browser site={case.site!r} query={case.query!r}"
    if case.route == "translate":
        return f"translate text={case.text!r} lang={case.lang}"
    return "llm"


def selftest_parser(cases):
    """Парсер-«оракул» с намеренными ошибками: проверка самого отчёта."""
    table = {c.phrase: c for c in cases}
    counter = {"n": 0}

    def parse(phrase):
        c = table[phrase]
        counter["n"] += 1
        n = counter["n"]
        res = {"route": c.route, "site": c.site, "query": c.query, "text": c.text, "lang": c.lang,
               "confidence": 0.99, "fallback": False, "reason": "", "phrase": phrase}
        if c.route != "llm" and n % 10 == 0:
            res.update(route="llm", site="", query="", text="", lang="none", fallback=True, reason="не уверена в намерении (0.70)")
        elif c.route == "browser" and c.query and n % 17 == 0:
            res["query"] = res["query"].split()[0]
        elif c.route == "translate" and n % 13 == 0:
            res["lang"] = "none" if c.lang != "none" else "en"
        elif c.route == "llm" and n % 4 == 0:
            res.update(route="browser", query="что угодно")
        return res
    return parse


def load_real_parser():
    try:
        import ml_infer
        return ml_infer.get_engine().predict_one
    except ImportError as exc:
        sys.exit(f"Не хватает пакета для модели: {exc}\nУстановите: pip install torch transformers")
    except Exception as exc:                      # нет папки модели и т.п.
        sys.exit(f"Модель не загрузилась: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Прогон корпуса фраз через обученную модель")
    ap.add_argument("--hand", action="store_true", help="только ручные фразы")
    ap.add_argument("--tag"), ap.add_argument("--route", choices=["browser", "translate", "llm"])
    ap.add_argument("--no-reach", action="store_true", help="убрать «reach»-фразы вовсе")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=25, help="сколько ошибок каждого вида печатать")
    ap.add_argument("--show-ok", action="store_true")
    ap.add_argument("--tsv", help="записать ошибки в этот файл в формате ml_real.tsv")
    ap.add_argument("--min-accuracy", type=float, default=0.0, help="код возврата 1, если доля ok ниже")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    cases = all_cases(generated=not a.hand)
    if a.tag:
        cases = [c for c in cases if c.tag == a.tag]
    if a.route:
        cases = [c for c in cases if c.route == a.route]
    if a.no_reach:
        cases = [c for c in cases if c.tag != "reach"]
    if a.limit:
        cases = cases[:a.limit]
    if not cases:
        sys.exit("Нет фраз под такой фильтр.")

    parse = selftest_parser(cases) if a.selftest else load_real_parser()
    outcomes, times, fails = collections.defaultdict(list), [], []
    by_tag = collections.defaultdict(collections.Counter)
    for c in cases:
        t = time.perf_counter()
        res = parse(c.phrase)
        times.append((time.perf_counter() - t) * 1000)
        outcome, detail = judge(c, res)
        by_tag[c.tag][outcome] += 1
        if outcome != "ok":
            outcomes[outcome].append((c, detail))
            fails.append(c)
        elif a.show_ok:
            print(f"  ok  {c.phrase}")

    counted = [c for c in cases if c.tag != "reach"]
    ok_counted = sum(1 for c in counted if not any(c is f for f in fails))
    print(f"\nФраз: {len(cases)} (в зачёт {len(counted)}). Верно в зачёте: {ok_counted} = {ok_counted / max(1, len(counted)):.1%}")
    print(f"Время на фразу: среднее {statistics.mean(times):.0f} мс, 95-й процентиль {sorted(times)[int(len(times) * 0.95) - 1]:.0f} мс")

    kinds = ["ok", "страховка", "ушла в llm", "лишнее действие", "не те данные"]
    print("\n%-18s" % "группа" + "".join("%17s" % k for k in kinds))
    for tag in sorted(by_tag):
        print("%-18s" % tag + "".join("%17d" % by_tag[tag][k] for k in kinds))

    for kind in kinds[1:]:
        rows = outcomes.get(kind, [])
        if not rows:
            continue
        print(f"\n=== {kind}: {len(rows)} ===")
        for c, detail in rows[:a.show]:
            print(f"  [{c.tag}] {c.phrase}\n      нужно: {expected(c)}" + (f"\n      вышло: {detail}" if detail else ""))
        if len(rows) > a.show:
            print(f"  … ещё {len(rows) - a.show} (увеличьте --show)")

    if a.tsv:
        with open(a.tsv, "w", encoding="utf-8") as f:
            for c in fails:
                if c.tag == "reach":
                    continue
                f.write("\t".join([c.phrase, c.route, c.site, c.query, c.text, c.lang]) + "\n")
        print(f"\nОшибки в формате ml_real.tsv: {a.tsv}")
    return 0 if ok_counted / max(1, len(counted)) >= a.min_accuracy else 1


if __name__ == "__main__":
    sys.exit(main())
