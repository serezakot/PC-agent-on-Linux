"""Контракт парсера первого слоя: что он понимает и как это размечается (без torch).

Первый слой JARVIS — маленькая обученная модель. Она за ~20 мс решает, что хочет пользователь,
и достаёт из фразы нужные куски. Всё сложное она отдаёт большой LLM (второй слой).

ТРИ НАМЕРЕНИЯ (intent)
  browser   — во фразе есть команда для браузера: открой / запусти / зайди / перейди / включи /
              поставь / найди / поищи / покажи / посмотри / узнай / погугли / загугли ...
              Куски: SITE (какой сайт, если назван) и QUERY (что искать, если сказано).
              Нет сайта, но названо «где искать» (гугл / интернет / сеть / поиск / браузер, или «погугли X»)
              -> обычный поиск в Google. «Включи / поставь X» (музыка, фильм) без площадки — тоже поиск в Google.
              Есть только сайт -> открыть сайт. Есть оба -> сайт с запросом.
  translate — «переведи ...», «как будет X по-английски», «как сказать X на немецком».
              Куски: TEXT (что переводить, дословно) и язык ПЕРЕВОДА (отдельная голова: код из LANGS,
              «none», если язык не назван). Исходный язык не нужен: переводчики определяют его сами.
  llm       — всё остальное: вопросы, просьбы рассказать/объяснить/написать/посчитать,
              болтовня. Если команды для браузера нет — это не к браузеру, а к большой LLM.

РАЗМЕТКА СЛОВ (tags): O — мусор, SITE, QUERY, TEXT. Слова не меняются никогда: модель только
ставит метки словам исходной фразы, а site/query/text собираются из этих же слов.

ПРАВИЛА (их можно и нужно поправить — от них зависит качество; всё, что здесь записано, генератор
данных выполняет одинаково везде)
  1. Команды, вежливость, обращение к ассистенту («джарвис», «пожалуйста», «ну», «плиз») — O.
  2. Предлог перед сайтом («на», «в», «через») и слова «сайт», «страница», «приложение» — O.
     Исключение: предлог, который сам входит в название, — SITE.
  3. Название сайта из нескольких слов — все слова SITE («яндекс карты», «саунд клауд»).
     Название пишется как сказано: падеж, опечатки и язык не трогаются.
  4. «Погугли / загугли / нагугли / поищи в гугле / поищи в интернете X» — browser без сайта:
     «в гугле», «в интернете», «в сети» — O. «Открой гугл» — открыть сайт: «гугл» — SITE.
  5. QUERY — непрерывный кусок: всё, что описывает объект поиска, включая предлоги внутри
     («чехол для телефона», «комнату в софии»), фильтры («за час», «новые»). Команда («найди»,
     «включи») в QUERY никогда не входит, даже «и включи radiohead».
  6. «Включи / поставь X» без названного сайта — QUERY (browser без сайта).
     «Открой / запусти / зайди на X» — X всегда SITE, даже незнакомое.
  7. Название сайта внутри самого вопроса («найди как отключить рекламу в ютубе», «найди отзывы
     о ютуб премиум») — часть QUERY, а не SITE. Для фраз «найди <вопрос> на/в сайте» однозначного
     правила нет: такие фразы в обучение не входят, а при сомнении модель отдаёт фразу большой LLM.
  8. Вопросы и просьбы без команды браузера («кто такой павел первый», «что такое гитхаб»,
     «расскажи про чёрные дыры», «как дела») — llm, даже если в них названы сайты.
  9. translate: TEXT — всё, что надо перевести, дословно (даже если это похоже на команду:
     «переведи на английский открой ютуб» -> TEXT «открой ютуб»). Слова «слово», «фразу», «на
     английский», «с русского», «по-английски» — O.
 10. Если модель не уверена (низкая вероятность или странная разметка) — llm.
 11. ГДЕ ИСКАТЬ. «Найди / поищи / ищи / покажи / посмотри / глянь / узнай / вбей / сделай запрос X» без
     указания места поиска (ни сайта, ни «в гугле», «в интернете», «в сети», «в поиске», «в браузере») — llm:
     «найди формулу площади круга» -> llm, а «найди в гугле формулу площади круга» и «найди формулу
     площади круга на википедии» -> browser. «Погугли / загугли» уже содержат место. Название сайта внутри
     самого вопроса («найди как отключить рекламу в ютубе») местом не считается -> llm.
     «Включи / поставь X» (музыка, фильм) — не поиск, правило 6 остаётся: browser без сайта.
 12. Быт и техника (свет, шторы, будильник, таймер, напоминание, звонок, смс, телевизор, климат, бытовая
     техника, «как…»-вопросы) — llm, даже если команда выглядит как «включи / открой / запусти / поставь».
     Системные задачи компьютера (калькулятор, блокнот, папки, файлы, окна, скриншот, выключение и
     перезагрузка ПК, запуск скриптов) — тоже не браузер и не перевод -> llm. В данных это отдельная
     категория (шаблон llm.system, файлы system*.txt), с бытом она не смешивается.
 13. «Пожалуйста» / «плиз» в переводе. Сразу после команды («переведи пожалуйста …») или перед ней — O.
     После переводимого текста («переведи на чешский два чая пожалуйста») и внутри него («как сказать помогите
     пожалуйста по-английски») — часть TEXT: переводчик получит фразу вместе с вежливым словом.
     Язык-наречие «по болгарски» внутри переводимой фразы («я не говорю по болгарски») тоже TEXT; тот «по X»,
     которым назван язык ПЕРЕВОДА, — O.
 14. «Что значит / что означает X». Без указания языка («что означает выражение by the way») — просьба объяснить, это llm.
     С языком («что значит слово gorgeous на русском», «что значит awkward по-русски») — translate.
 15. Глаголы-действия между сайтом и запросом («на вольфрам альфа реши интеграл», «в спидтесте проверь пинг»,
     «на десмосе построй график») — O, а не QUERY. Если сайта и места поиска нет («реши уравнение», «закажи пиццу»,
     «проверь пинг») — llm. Если разметка получилась рваной (кусок SITE / QUERY / TEXT разорван словами другой
     метки) — модель ошиблась, фраза уходит в llm (decide это проверяет).
"""
import re
from pathlib import Path

import ml_home

INTENTS = ("browser", "translate", "llm")
TAGS = ("O", "SITE", "QUERY", "TEXT")
LANGS = ("none", "en", "ru", "de", "fr", "es", "it", "pt", "pl", "bg", "tr", "uk", "cs", "nl",
         "sv", "el", "ro", "sr", "zh", "ja", "ko", "ar", "hi", "fi", "da", "hu", "he", "vi", "id", "kk")

BASE_MODEL = "cointegrated/rubert-tiny2"
MODEL_DIR = str(Path(__file__).resolve().parent / "ml_model")   # абсолютный путь: не зависит от папки запуска
MAX_LEN = 64
EDGE = " ,.;:!?«»\"'"


def norm(text: str) -> str:
    """Мягкая нормализация для сравнения: регистр, ё/е, пробелы, знаки по краям."""
    return " ".join(text.casefold().replace("ё", "е").split()).strip(EDGE)


def _span(tags, label):
    """Индексы слов с меткой label; дыры из O между первым и последним заполняются
    (запрос и название сайта — непрерывные куски). Если в дыре другая метка — не заполняем."""
    idx = [i for i, t in enumerate(tags) if t == label]
    if not idx:
        return []
    first, last = idx[0], idx[-1]
    if any(t not in (label, "O") for t in tags[first:last + 1]):
        return idx
    return list(range(first, last + 1))


def assemble(words, tags) -> dict:
    """Метки слов -> {"site", "query", "text"}. Текст берётся только из слов фразы."""
    def build(label):
        return " ".join(words[i] for i in _span(tags, label)).strip(EDGE)

    return {"site": build("SITE"), "query": build("QUERY"), "text": build("TEXT")}


THRESHOLD_INTENT = 0.90     # ниже — не уверены в намерении -> llm   (подобрано test_ml.py --sweep на финальной модели)
THRESHOLD_SLOT = 0.75       # ниже у любого слова -> не уверены в разметке -> llm
THRESHOLD_LANG = 0.50       # язык перевода ниже порога -> "none"


def fragmented(tags) -> str:
    """Рваная разметка: между первым и последним словом SITE / QUERY / TEXT стоит слово ДРУГОЙ метки.
    Возвращает название метки или ''. Такого в обучающих данных не бывает, значит это ошибка модели."""
    for label in ("SITE", "QUERY", "TEXT"):
        idx = [i for i, t in enumerate(tags) if t == label]
        if idx and any(t not in (label, "O") for t in tags[idx[0]:idx[-1] + 1]):
            return label
    return ""


# Глагол-команда внутри поискового запроса без сайта («у меня руки заняты включи чайник»): так бывает, когда модель
# приняла команду устройству за медиа-запрос. В настоящем запросе таких слов почти не бывает -> лучше спросить большую LLM.
_COMMAND_IN_QUERY = re.compile(r"(?<!\w)(включи|выключи|включай|выключай|включите|выключите|запусти|запускай|открой|открывай|закрой)(?!\w)", re.I)


def decide(phrase: str, words, pred: dict, t_intent=THRESHOLD_INTENT, t_slot=THRESHOLD_SLOT,
           t_lang=THRESHOLD_LANG) -> dict:
    """Предсказание модели (вероятности и метки слов) -> решение, куда направить фразу.

    Страховка: если модель сомневается или разметка странная, фраза не исполняется наугад, а уходит
    большой LLM (fallback=True). Чистая функция — проверяется тестами без модели.
    """
    best = max(range(len(INTENTS)), key=lambda k: pred["intent_p"][k])
    intent, conf = INTENTS[best], pred["intent_p"][best]
    lbest = max(range(len(LANGS)), key=lambda k: pred["lang_p"][k])
    lang = LANGS[lbest] if pred["lang_p"][lbest] >= t_lang else "none"
    parts = assemble(words, pred["tags"])
    result = {"route": intent, "site": parts["site"], "query": parts["query"], "text": parts["text"],
              "lang": lang if intent == "translate" else "none", "confidence": conf,
              "fallback": False, "reason": "", "phrase": phrase}
    if intent == "browser":
        result["text"] = ""
    elif intent == "translate":
        result["site"] = result["query"] = ""
    else:
        result["site"] = result["query"] = result["text"] = ""
    if intent == "llm":
        return result
    reason = ""
    if pred.get("truncated"):
        reason = "слишком длинная фраза"
    elif conf < t_intent:
        reason = f"не уверена в намерении ({conf:.2f})"
    elif min(pred["tag_p"]) < t_slot:
        reason = f"не уверена в разметке слов ({min(pred['tag_p']):.2f})"
    elif intent != "llm" and fragmented(pred["tags"]):
        reason = f"разметка рваная ({fragmented(pred['tags'])})"
    elif intent == "browser" and not (result["site"] or result["query"]):
        reason = "команда есть, а сайта и запроса не нашлось"
    elif (intent == "browser" and not result["site"] and result["query"]
          and not (ml_home.has_where(phrase) or ml_home.is_play(phrase))):
        reason = "поиск без указания, где искать (правило 11)"
    elif intent == "browser" and not result["site"] and _COMMAND_IN_QUERY.search(result["query"]):
        reason = "в запросе осталась команда (включи/открой…): похоже на команду устройству"
    elif intent == "translate" and not result["text"]:
        reason = "перевод есть, а текста не нашлось"
    if reason:
        result.update(route="llm", site="", query="", text="", lang="none", fallback=True, reason=reason)
    return result


def _wkey(word: str) -> str:
    return word.casefold().replace("ё", "е").strip(EDGE)


def find_words(words, chunk: str, used=()) -> list:
    """Индексы подряд идущих слов фразы, равных chunk (регистр, ё/е и знаки по краям не важны)."""
    target = [_wkey(w) for w in chunk.split()]
    if not target:
        return []
    keys = [_wkey(w) for w in words]
    for start in range(len(words) - len(target) + 1):
        span = list(range(start, start + len(target)))
        if keys[start:start + len(target)] == target and not any(i in used for i in span):
            return span
    raise ValueError(f"в фразе {' '.join(words)!r} нет подряд слов {chunk!r}")


def tag_phrase(phrase: str, site: str = "", query: str = "", text: str = ""):
    """Фраза + её куски -> (слова, метки). Нужна, чтобы ваши реальные фразы превратить в обучающие."""
    words = phrase.split()
    tags = ["O"] * len(words)
    used = set()
    for label, chunk in (("SITE", site), ("QUERY", query), ("TEXT", text)):
        for i in find_words(words, chunk, used):
            tags[i] = label
            used.add(i)
    return words, tags


def read_tsv(path):
    """ml_real.tsv / ml_mine.tsv -> [(фраза, намерение, site, query, text, язык)]. Строки с # пропускаются."""
    import os
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split("\t") + [""] * 6
            rows.append((p[0].strip(), p[1].strip(), p[2].strip(), p[3].strip(), p[4].strip(), p[5].strip() or "none"))
    return rows
