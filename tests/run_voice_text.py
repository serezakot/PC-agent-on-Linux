#!/usr/bin/env python3
"""Готов ли первый слой к голосу: каждая фраза корпуса переводится в «распознанный» вид — так её выдаёт распознавание
русской речи (строчные буквы, без знаков препинания, числа словами, английские названия кириллицей, «main.py» ->
«мейн точка пай»), и сравнивается с напечатанной фразой:

    намерение — тот же маршрут и то же действие, что у напечатанной фразы;
    исполнитель — тот же итоговый план: для системных команд и программ строится настоящий план (dry-run, временная
                  «домашняя папка» с файлами корпуса) по разбору напечатанной и разбору «голосовой» фразы.

    python3 tests/run_voice_text.py                 # весь корпус (системные, программы, браузер, переводчик, llm)
    python3 tests/run_voice_text.py --set system --show 40
    python3 tests/run_voice_text.py --print "Поставь громкость на 40%"   # только показать «голосовой» вид

Английский текст для перевода («переведи it is raining cats and dogs») русское распознавание не запишет латиницей —
такие фразы отмечены отдельно и в процент не входят.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, ROOT / "ml", HERE):
    sys.path.insert(0, str(p))

# --- как распознавание речи пишет латинские названия (только данные теста, исполнитель их не видит) ---
_SPOKEN_LATIN = {
    "big baby tape": "биг бэйби тейп", "travis scott": "трэвис скотт", "playboi carti": "плейбой карти",
    "boulevard depo": "бульвар депо", "redmi note": "редми ноут", "jay z": "джей зи", "vs code": "вэ эс код",
    "rubert-first-layer-intent-parser-lab": "руберт ферст лейер интент парсер лаб",
    "education": "эдьюкейшн", "docx": "докс", "ml": "эм эль", "jpg": "джейпег", "traning": "трейнинг", "txt": "тэ икс тэ",
    "wifi": "вай фай", "wi-fi": "вай фай", "bluetooth": "блютуз", "notes": "ноутс", "the": "зе", "main": "мейн",
    "py": "пай", "mp4": "эм пэ четыре", "kizaru": "кизару", "youtube": "ютуб", "soundcloud": "саундклауд",
    "aliexpress": "алиэкспресс", "rust": "раст", "google": "гугл", "log": "лог", "report": "репорт", "final": "файнал",
    "pdf": "пэ дэ эф", "zip": "зип", "lab": "лаб", "setup": "сетап", "sh": "эс аш", "jbl": "джей би эль",
    "backup": "бэкап", "tar": "тар", "gz": "гэ зэ", "run": "ран", "airpods": "эйрподс", "ip": "ай пи",
    "firefox": "файрфокс", "telegram": "телеграм", "vlc": "вэ эл цэ", "gimp": "гимп", "oxxxymiron": "оксимирон",
    "spotify": "спотифай", "radiohead": "рэдиохэд", "face": "фейс", "nas": "нас", "illmatic": "илматик",
    "xiaomi": "сяоми", "requests": "реквестс", "keyerror": "кей эррор", "translate": "транслейт", "deepl": "дипл",
    "python": "пайтон", "windows": "виндоус", "tcp": "тэ цэ пэ", "udp": "у дэ пэ", "desktop": "десктоп",
    "model": "модел", "todo": "ту ду", "assistant": "ассистант", "temp": "темп", "old": "олд",
    "hdmi": "эйч ди эм ай", "obsidian": "обсидиан", "discord": "дискорд", "weeknd": "уикенд", "csv": "си эс ви",
    "md": "эм ди", "readme": "ридми", "vpn": "вэ пэ эн", "escape": "эскейп", "ram": "рам", "steam": "стим",
    "dota": "дота", "pycharm": "пайчарм", "ssh": "эс эс аш", "ubuntu": "убунту", "docker": "докер", "data": "дата",
    "f2": "эф два", "f5": "эф пять", "f8": "эф восемь", "f11": "эф одиннадцать", "x": "икс",
}
_LAT_LETTERS = [("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ee", "и"), ("oo", "у"), ("ck", "к"),
                ("kh", "х"), ("zh", "ж"), ("ya", "я"), ("yu", "ю"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"),
                ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
                ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"),
                ("x", "кс"), ("y", "и"), ("z", "з")]


def _latin_word(w: str) -> str:
    if w in _SPOKEN_LATIN:
        return _SPOKEN_LATIN[w]
    out, i = "", 0
    while i < len(w):
        for a, b in _LAT_LETTERS:
            if w.startswith(a, i):
                out, i = out + b, i + len(a)
                break
        else:
            out, i = out + w[i], i + 1
    return out


_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
          "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def number_words(n: int) -> str:
    """Число словами в именительном падеже, как его выдаёт распознавание: 1920 -> «тысяча девятьсот двадцать»."""
    if n == 0:
        return "ноль"
    parts = []
    th, n = divmod(n, 1000)
    if th:
        parts.append({1: "тысяча", 2: "две тысячи", 3: "три тысячи", 4: "четыре тысячи"}.get(th) or f"{number_words(th)} тысяч")
    h, n = divmod(n, 100)
    if h:
        parts.append(_HUNDREDS[h])
    if 10 <= n < 20:
        parts.append(_TEENS[n - 10])
    else:
        t, u = divmod(n, 10)
        if t:
            parts.append(_TENS[t])
        if u:
            parts.append(_UNITS[u])
    return " ".join(parts)


def spoken_form(text: str) -> str:
    """Фраза так, как её запишет распознавание русской речи (без пунктуации, числа словами, латиница кириллицей)."""
    t = text.casefold().replace("ё", "е")
    t = re.sub(r"(\d+)\s*[xх×]\s*(\d+)", r"\1 на \2", t)
    t = re.sub(r"(?<=[а-я])(?=\d)|(?<=\d)(?=[а-я])", " ", t)            # «ф11» -> «ф 11»: распознавание пишет раздельно
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: f"{number_words(int(m.group(1)))} "
               + ("ноль ноль" if m.group(2) == "00" else ("ноль " + _UNITS[int(m.group(2))]) if m.group(2)[0] == "0"
                  else number_words(int(m.group(2)))), t)
    t = t.replace("%", " процентов")
    for phrase in sorted((k for k in _SPOKEN_LATIN if " " in k or "-" in k), key=len, reverse=True):
        t = re.sub(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", _SPOKEN_LATIN[phrase], t)
    t = re.sub(r"(?<=\w)\.(?=[a-z])", " точка ", t)                           # main.py -> main точка py
    t = re.sub(r"[a-z][a-z0-9']*", lambda m: _latin_word(m.group(0)), t)
    t = re.sub(r"\d+(?:[.,]\d+)?", lambda m: number_words(int(float(m.group(0).replace(",", ".")))), t)
    t = re.sub(r"[^\w\s]|_", " ", t)
    return " ".join(t.split())


def _english_speech(case) -> bool:
    return case.route == "translate" and bool(re.search(r"[a-z]{2}", (case.text or "").lower()))


def _norm_desc(s: str) -> str:
    s = re.sub(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", "<время>", s or "")
    return " ".join(s.casefold().replace("ё", "е").split())


def _script_free(key: str) -> str:
    """Итог исполнителя без разницы в записи названий (латиница/кириллица, %-кодирование ссылок)."""
    from urllib.parse import unquote
    return spoken_form(unquote(key))


def _plan_key(route: str, res: dict, home: Path) -> str:
    """Итог исполнителя (без выполнения): то, что реально будет сделано."""
    from jarvis import apps, system
    if route == "system":
        plan = system.make_system_plan(query=res.get("query", ""), home=home, action=res.get("action", ""),
                                               site=res.get("site", ""), text=res.get("text", ""))
        return "нет плана" if plan is None else f"{plan.action} | {_norm_desc(plan.description)} | {plan.target_path} | {plan.dest_path}"
    if route == "app":
        plan = apps.make_app_plan(res.get("site", ""), res.get("query", ""), home=home)
        return "нет плана" if plan is None else f"{plan.kind} | {plan.target} | {' '.join(plan.cmd)} | {plan.query}"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка первого слоя на «голосовом» виде фраз")
    ap.add_argument("--set", choices=["all", "system", "browser"], default="all")
    ap.add_argument("--show", type=int, default=30, help="сколько расхождений каждого вида печатать")
    ap.add_argument("--print", dest="show_text", help="только показать «голосовой» вид фразы")
    ap.add_argument("--model", default="", help="папка другой модели")
    ap.add_argument("--no-itn", action="store_true", help="без обратной нормализации (jarvis.spoken.normalize_spoken) — сырой текст ASR")
    a = ap.parse_args(argv)
    from jarvis.spoken import normalize_spoken
    to_voice = (lambda p: spoken_form(p)) if a.no_itn else (lambda p: normalize_spoken(spoken_form(p)))   # noqa: E731
    if a.show_text:
        print(spoken_form(a.show_text), "->", normalize_spoken(spoken_form(a.show_text)))
        return 0

    from cases import all_cases
    from run_model import SYSTEM_CASES, load_real_parser
    from test_system import make_fake_home
    from ml_spec import norm

    cases = []
    if a.set in ("all", "browser"):
        cases += all_cases(generated=False)
    if a.set in ("all", "system"):
        cases += list(SYSTEM_CASES)
    parse = load_real_parser(a.model)
    home = make_fake_home()

    same = lambda x, y: norm(spoken_form(x or "")) == norm(spoken_form(y or ""))       # noqa: E731
    stats = collections.Counter()
    fails: dict[str, list[str]] = collections.defaultdict(list)
    for c in cases:
        if _english_speech(c):
            stats["english"] += 1
            continue
        v = to_voice(c.phrase)
        r1, r2 = parse(c.phrase), parse(v)
        stats["total"] += 1
        line = f"«{c.phrase}» -> «{v}»"
        if (r1.get("route"), r1.get("action")) != (r2.get("route"), r2.get("action")):
            got = f"{r2.get('route')}/{r2.get('action')}" + (" (не уверена)" if r2.get("fallback") else "")
            fails["другое намерение"].append(f"{line}\n      напечатано {r1.get('route')}/{r1.get('action')}, голосом {got}")
            continue
        stats["intent_ok"] += 1
        if r1.get("route") in ("system", "app"):
            k1, k2 = _plan_key(r1["route"], r1, home), _plan_key(r2["route"], r2, home)
            stats["exec_total"] += 1
            if _norm_desc(k1) == _norm_desc(k2):
                stats["exec_ok"] += 1
            elif _script_free(k1) == _script_free(k2):
                stats["exec_script"] += 1
                fails["то же, но название кириллицей"].append(f"{line}\n      напечатано: {k1}\n      голосом:    {k2}")
            else:
                fails["другой итог исполнителя"].append(f"{line}\n      напечатано: {k1}\n      голосом:    {k2}")
        else:
            slots_ok = all(same(r1.get(k, ""), r2.get(k, "")) for k in ("site", "query", "text"))
            stats["slot_total"] += 1
            if slots_ok:
                stats["slot_ok"] += 1
            else:
                fails["другие слова в слотах"].append(
                    f"{line}\n      напечатано site={r1.get('site')!r} query={r1.get('query')!r} text={r1.get('text')!r}"
                    f"\n      голосом    site={r2.get('site')!r} query={r2.get('query')!r} text={r2.get('text')!r}")

    for kind, items in fails.items():
        print(f"\n=== {kind}: {len(items)}")
        for s in items[:a.show]:
            print("  " + s)
    pct = lambda x, y: f"{100 * x / max(1, y):.1f}%"     # noqa: E731
    print(f"\nФраз: {stats['total']} (+{stats['english']} с английской речью — не в зачёт)")
    print(f"Намерение то же, что у напечатанной: {stats['intent_ok']}/{stats['total']} = {pct(stats['intent_ok'], stats['total'])}")
    print(f"Системные и программы — тот же итог исполнителя: {stats['exec_ok']}/{stats['exec_total']} = "
          f"{pct(stats['exec_ok'], stats['exec_total'])}; ещё {stats['exec_script']} — то же, но название кириллицей")
    print(f"Браузер/перевод/llm — те же слова в слотах: {stats['slot_ok']}/{stats['slot_total']} = "
          f"{pct(stats['slot_ok'], stats['slot_total'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
