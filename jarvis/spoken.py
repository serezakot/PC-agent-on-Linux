"""Знание русского устного языка, общее для всех исполнителей: числительные во всех падежах, буквы латинских
аббревиатур, произносимые расширения файлов. Это словарь языка (как таблица транслитерации), а не правила фраз.

    words_to_digits("убавь до тридцати процентов")         -> "убавь до 30 процентов"
    words_to_digits("тысяча девятьсот двадцать на тысячу восемьдесят") -> "1920 на 1080"
    normalize_spoken("подключи вэ пэ эн")                  -> "подключи vpn"
    normalize_spoken("удали мейн точка пай")                -> "удали мейн.py"
    normalize_spoken("напомни в восемнадцать ноль ноль", digits=True) -> "напомни в 18:00"

normalize_spoken — «обратная нормализация» текста от распознавания речи (ASR пишет всё словами, строчными, без знаков):
приводит его к тому виду, в каком фразу напечатал бы человек. Напечатанный текст ею не трогается.
"""
from __future__ import annotations

import re


# --- числительные: формы всех падежей ---
def _forms(value: int, *words: str) -> dict[str, int]:
    return {w: value for w in words}


_UNITS: dict[str, int] = {
    **_forms(0, "ноль", "нуль", "нуля", "ноля", "нулю", "нолю", "нулем", "нолем", "нуле", "ноле"),
    **_forms(1, "один", "одна", "одно", "одну", "одного", "одной", "одному", "одним", "одном", "одною"),
    **_forms(2, "два", "две", "двух", "двум", "двумя"),
    **_forms(3, "три", "трех", "трем", "тремя"),
    **_forms(4, "четыре", "четырех", "четырем", "четырьмя"),
    **_forms(5, "пять", "пяти", "пятью"),
    **_forms(6, "шесть", "шести", "шестью"),
    **_forms(7, "семь", "семи"),                       # «семью» — чаще «семья», не число
    **_forms(8, "восемь", "восьми", "восемью", "восьмью"),
    **_forms(9, "девять", "девяти", "девятью"),
}
_TEEN_STEMS = ("десят", "одиннадцат", "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат", "семнадцат",
               "восемнадцат", "девятнадцат")
_TEENS: dict[str, int] = {f"{stem}{end}": 10 + i for i, stem in enumerate(_TEEN_STEMS) for end in ("ь", "и", "ью")}
_TENS: dict[str, int] = {
    **_forms(20, "двадцать", "двадцати", "двадцатью"), **_forms(30, "тридцать", "тридцати", "тридцатью"),
    **_forms(40, "сорок", "сорока"), **_forms(50, "пятьдесят", "пятидесяти", "пятьюдесятью"),
    **_forms(60, "шестьдесят", "шестидесяти", "шестьюдесятью"), **_forms(70, "семьдесят", "семидесяти", "семьюдесятью"),
    **_forms(80, "восемьдесят", "восьмидесяти", "восемьюдесятью"), **_forms(90, "девяносто", "девяноста"),
}
_HUNDREDS: dict[str, int] = {
    **_forms(100, "сто", "ста"),
    **_forms(200, "двести", "двухсот", "двумстам", "двумястами", "двухстах"),
    **_forms(300, "триста", "трехсот", "тремстам", "тремястами", "трехстах"),
    **_forms(400, "четыреста", "четырехсот", "четыремстам", "четырьмястами", "четырехстах"),
    **_forms(500, "пятьсот", "пятисот", "пятистам", "пятьюстами", "пятистах"),
    **_forms(600, "шестьсот", "шестисот", "шестистам", "шестьюстами", "шестистах"),
    **_forms(700, "семьсот", "семисот", "семистам", "семьюстами", "семистах"),
    **_forms(800, "восемьсот", "восьмисот", "восьмистам", "восемьюстами", "восьмистах"),
    **_forms(900, "девятьсот", "девятисот", "девятистам", "девятьюстами", "девятистах"),
}
_THOUSAND = {"тысяча", "тысячи", "тысяч", "тысячу", "тысячей", "тысячею", "тысяче", "тысячам", "тысячами", "тысячах"}
_HALF = {"полтора": "1.5", "полторы": "1.5", "полутора": "1.5"}
# порядковые в родительном падеже: «половина седьмого», «четверть восьмого» (часы)
ORDINAL_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6, "седьмого": 7,
               "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12}


def _clean(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").strip(" ,.;:!?«»\"'")
    text = re.sub(r"[-_]+", " ", text)
    return " ".join(text.split())


def words_to_digits(text: str) -> str:
    """Числительные -> цифры, составные складываются: «через двадцать пять минут» -> «через 25 минут»,
    «до тридцати» -> «до 30», «две тысячи пятьсот шестьдесят» -> «2560». «ноль» всегда отдельное число («18 0 0»)."""
    out: list[str] = []
    th: int | None = None          # тысячи текущего числа
    cur: int | None = None         # часть текущего числа меньше тысячи

    def flush() -> None:
        nonlocal th, cur
        if th is not None or cur is not None:
            out.append(str((th or 0) + (cur or 0)))
        th = cur = None

    for w in _clean(text).split():
        if w in _HALF:
            flush()
            out.append(_HALF[w])
        elif w in _THOUSAND:
            mult = cur if cur is not None else 1
            if th is not None:
                flush()
            th, cur = mult * 1000, None
        elif w in _HUNDREDS:
            if cur is not None:
                flush()
            cur = _HUNDREDS[w]
        elif w in _TENS:
            if cur is not None and cur % 100 == 0 and cur >= 100:
                cur += _TENS[w]
            else:
                if cur is not None:
                    flush()
                cur = _TENS[w]
        elif w in _UNITS or w in _TEENS:
            val = _UNITS.get(w, _TEENS.get(w, 0))
            if w in _UNITS and val == 0:
                flush()
                cur = 0
                flush()
            elif cur is not None and ((cur % 10 == 0 and cur >= 20 and val < 10) or (cur % 100 == 0 and cur >= 100)):
                cur += val
            else:
                if cur is not None:
                    flush()
                cur = val
        else:
            flush()
            out.append(w)
    flush()
    return " ".join(out)


# --- латинские буквы, как их называют вслух («вэ пэ эн» = VPN, «эм пэ три» = mp3) ---
LETTER_NAMES = {
    "эй": "a", "би": "b", "бэ": "b", "си": "c", "цэ": "c", "ди": "d", "дэ": "d", "эф": "f", "джи": "g", "гэ": "g",
    "эйч": "h", "аш": "h", "ай": "i", "джей": "j", "кей": "k", "эл": "l", "эль": "l", "эм": "m", "эн": "n", "оу": "o",
    "пи": "p", "пэ": "p", "кью": "q", "ар": "r", "эр": "r", "эс": "s", "ти": "t", "тэ": "t", "ви": "v", "вэ": "v",
    "икс": "x", "уай": "y", "игрек": "y", "зед": "z", "зи": "z", "зэ": "z",
}
# расширения, которые произносят словом, а не по буквам
EXT_WORDS = {"пай": "py", "джейпег": "jpg", "джипег": "jpg", "жпег": "jpg", "джпг": "jpg", "пнг": "png", "пдф": "pdf",
             "докс": "docx", "док": "doc", "тхт": "txt", "цсв": "csv", "зип": "zip", "рар": "rar", "тар": "tar",
             "лог": "log", "джейсон": "json", "ямл": "yaml", "мд": "md", "экзе": "exe", "деб": "deb", "хтмл": "html"}
_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
            "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
            "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": ""}


def _letter_at(tokens: list[str], i: int) -> tuple[str, int] | None:
    """Буква, названная вслух, в позиции i -> (буква, сколько слов заняла)."""
    if tokens[i] == "дабл" and i + 1 < len(tokens) and tokens[i + 1] in ("ю", "в"):
        return "w", 2
    if tokens[i] in LETTER_NAMES:
        return LETTER_NAMES[tokens[i]], 1
    return None


def _letters(tokens: list[str]) -> list[str]:
    """Подряд идущие названия латинских букв (не меньше двух) -> аббревиатура: «вэ пэ эн» -> «vpn»; «эф пять» -> «f5»."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        j, word = i, ""
        while j < len(tokens) and (hit := _letter_at(tokens, j)) is not None:
            word, j = word + hit[0], j + hit[1]
        num = words_to_digits(tokens[j]) if j < len(tokens) else ""
        short_num = bool(re.fullmatch(r"\d{1,2}", num))
        if len(word) >= 2 or (word == "f" and short_num):
            if short_num and len(word) <= 3:         # сразу за буквами короткое число — часть имени: mp3, f5, mp4
                word, j = word + num, j + 1
            out.append(word)
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return out


def _ext(word: str) -> str | None:
    if re.fullmatch(r"[a-z0-9]{1,5}", word):
        return word
    if word in EXT_WORDS:
        return EXT_WORDS[word]
    if re.fullmatch(r"[а-я]{2,4}", word):
        return "".join(_CYR2LAT.get(ch, ch) for ch in word)
    return None


def _dots(tokens: list[str]) -> list[str]:
    """«мейн точка py» -> «мейн.py», «бэкап точка tar точка gz» -> «бэкап.tar.gz»."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "точка" and out and i + 1 < len(tokens) and (ext := _ext(tokens[i + 1])) is not None:
            out[-1] = f"{out[-1]}.{ext}"
            i += 2
            continue
        out.append(tokens[i])
        i += 1
    return out


_UNDERSCORE = re.compile(r"\s+(?:нижнее\s+)?(?:подчеркивани[ея]|андерскор|underscore|anderscore)\s+")
_DASH_WORD = re.compile(r"(?<=\w)\s+(?:дефис|тире)\s+(?=\w)")


def normalize_spoken(text: str, digits: bool = False) -> str:
    """Текст распознавания речи -> текст, как его напечатал бы человек (см. описание модуля).
    digits=True — ещё и все числительные цифрами. По умолчанию нет: исполнители сами понимают числа словами, а в
    свободном тексте («переведи как сказать это одним словом», «у семи нянек») цифры только мешают."""
    t = re.sub(r"[{}]", " ", (text or "").lower().replace("ё", "е"))   # Chirp: «data {нижнее подчёркивание} struct»
    t = _UNDERSCORE.sub("_", t)                                 # «report нижнее подчеркивание final» -> report_final
    t = _DASH_WORD.sub("-", t)
    t = re.sub(r"\bfile\b", "файл", t)                          # распознавание иногда пишет «файл» по-английски
    t = re.sub(r"[«»\"“”!?,;()]", " ", t)
    t = re.sub(r"(?<![\w])[.:]|[.:](?![\w])", " ", t)            # точки и двоеточия на краях слов — знаки препинания
    t = " ".join(_dots(_letters(t.split())))
    if digits:
        t = words_to_digits(t)
        t = re.sub(r"\b(\d{1,2}) 0 0\b", r"\1:00", t)                # «восемнадцать ноль ноль» -> 18:00
        t = re.sub(r"\b(\d{1,2}) 0 ([1-9])\b", r"\1:0\2", t)         # «шесть ноль пять» -> 6:05
    return " ".join(t.split())
