"""Contract of the first-layer parser: what it understands and how phrases are labelled (no torch needed).

The first layer of the voice assistant is a small trained model. In ~3-15 ms on CPU (e.g. AMD Ryzen 7 7840HS)
it decides what the user wants (route + canonical action) and pulls the needed arguments out of the phrase.
Anything complicated is handed to the big LLM (second layer). The commands themselves are Russian (they come from
speech recognition); all examples below are Russian on purpose.

FIVE INTENTS (ROUTES) + FOURTH HEAD (ACTION)
  browser   - web sites, web services, plain Google/web searches, YouTube direct video/channel opening, media playback
              without a named platform, and light factual searches (weather, currency rates, tickets & schedules).
              Also hybrid apps when explicitly requested in the browser ("открой телеграм в вебе / на сайте / в браузере").
              Actions:
                • web_open       : open site homepage ("открой ютуб", "перейди на официальный сайт Python")
                • web_search     : search on a site or in Google ("найди на ютубе пайтон за час", "загугли рецепт борща")
                • web_open_first : open/play the first video/item directly ("открой в ютубе пайтон за час", "включи влада а4 в ютубе")
                • web_channel    : open a channel/author page directly ("открой в ютубе канал fireship", "открой канал топлес")
              Pieces: SITE (which site, if named) and QUERY (what to search for / play / channel name, if said).
  app       - desktop applications, hybrid apps (telegram, steam, spotify, discord, claude, whatsapp, etc. without
              a web marker), system utilities (calculator, notepad, terminal, settings, explorer/files), local folders,
              drives and files ("открой калькулятор", "открой телеграм", "открой загрузки и отсортируй по дате").
              Actions:
                • app_open       : open app / settings / folder / file ("открой калькулятор", "открой настройки звука")
                • app_query      : open app / folder with inner search or action ("Открой Steam и найди Elden Ring")
              Pieces: SITE (the application, folder, drive or file name) and QUERY (optional inner query or action,
              without generic wrappers like "музыку" or "игру").
  system    - lightweight PC / OS control commands on Ubuntu 24.04 LTS ("выключи комп", "переведи в спящий режим",
              "сделай скриншот", "убавь яркость на 50", "переключи трек", "скопируй файл диплом на рабочий стол").
              Actions: canonical system action from SYSTEM_ACTIONS (vol_down, vol_up, vol_set, bright_down, bright_up,
              bright_set, media_pause, media_next, media_prev, media_seek_fwd, media_seek_back, fs_copy, fs_mkdir,
              fs_trash, fs_chmod_x, bt_on, bt_off, wifi_on, wifi_off, shot_screen, power_off, power_reboot,
              display_res_set, etc.).
              Pieces: QUERY (optional parameter/target: percentage, duration, file/folder name, app to close) and
              SITE (optional folder/place/destination: "рабочий стол", "флешку", "документы", "desktopa").
              TEXT is used only by reminder_set: the reminder message ("напомни через 20 минут TEXT").
              Command verbs and boilerplate ("убавь", "включи", "скопируй", "файл", "папку", "на компьютере") are O!
  translate - "переведи ...", "переведи: ...", "переведи как будет X на английском", "как будет X по-английски",
              "как сказать X на немецком", "как переводится X".
              Action: translate.
              Pieces: TEXT (what to translate, verbatim) and the TARGET language (a separate head: a code from
              LANGS, "none" if no language was named).
  llm       - everything else: complex questions, explanations ("скажи в чём разница между X и Y",
              "скажи погоду на завтра в 12"), smart home and appliances ("включи будильник", "включи свет",
              "включи чайник"), vague goals ("давай зайдём на гитхаб, мне нужно посмотреть один репозиторий"),
              and any phrase where the model is unsure (fallback=True).
              Action: none.

WORD TAGS (tags): O - command verbs/wrappers/noise, SITE, QUERY, TEXT. Words are never changed: the model only puts
labels on the words of the original phrase, and site/query/text are assembled from those very words.
There are no phrase rules in this module: labels come from the training data (built by construction in the
training lab), the model predicts them, decide() only applies confidence thresholds.
"""
import os
from pathlib import Path

INTENTS = ("browser", "app", "system", "translate", "llm")
TAGS = ("O", "SITE", "QUERY", "TEXT")
LANGS = ("none", "en", "ru", "de", "fr", "es", "it", "pt", "pl", "bg", "tr", "uk", "cs", "nl",
         "sv", "el", "ro", "sr", "zh", "ja", "ko", "ar", "hi", "fi", "da", "hu", "he", "vi", "id", "kk")

BROWSER_ACTIONS = (
    "web_open",
    "web_search",
    "web_open_first",
    "web_channel",
)

APP_ACTIONS = (
    "app_open",
    "app_query",
)

SYSTEM_ACTIONS = (
    # Audio
    "vol_up",
    "vol_down",
    "vol_set",
    "vol_mute",
    "vol_unmute",
    "mic_up",
    "mic_down",
    "mic_set",
    "mic_mute",
    "mic_unmute",
    # Display, Theme, Power Profiles, Peripherals
    "bright_up",
    "bright_down",
    "bright_set",
    "night_light_on",
    "night_light_off",
    "theme_dark",
    "theme_light",
    "scale_up",
    "scale_down",
    "dnd_on",
    "dnd_off",
    "power_perf",
    "power_saver",
    "power_balanced",
    "touchpad_on",
    "touchpad_off",
    "autorotate_on",
    "autorotate_off",
    # Network & Wireless
    "wifi_on",
    "wifi_off",
    "bt_on",
    "bt_off",
    "airplane_on",
    "airplane_off",
    "hotspot_on",
    "hotspot_off",
    "vpn_toggle",
    # Screenshots & Screen Recording
    "shot_screen",
    "shot_window",
    "shot_area",
    "shot_clipboard",
    "rec_start",
    "rec_stop",
    # Session & Power Control
    "power_off",
    "power_reboot",
    "power_cancel",
    "power_sleep",
    "power_hibernate",
    "power_wakeup",
    "session_lock",
    "session_logout",
    "session_switch_user",
    # Media & Video Playback
    "media_pause",
    "media_resume",
    "media_next",
    "media_prev",
    "media_seek_fwd",
    "media_seek_back",
    "video_subtitles",
    "video_fullscreen",
    "video_speed_up",
    "video_speed_down",
    # Window & Workspace Management
    "win_close_active",
    "win_close_all",
    "win_close_app",
    "win_close_browser",
    "win_restart_browser",
    "win_min_active",
    "win_min_all",
    "win_min_except_browser",
    "win_max_active",
    "win_exit_fullscreen",
    "win_switch",
    "win_overview",
    "win_tile_left",
    "win_tile_right",
    "win_always_on_top",
    "ws_next",
    "ws_prev",
    # Keyboard, Browser Tabs & Clipboard Binds
    "kb_layout",
    "tab_new",
    "tab_close",
    "tab_reopen",
    "tab_next",
    "tab_prev",
    "page_reload",
    "page_scroll_down",
    "page_scroll_up",
    "page_scroll_bottom",
    "page_scroll_top",
    "nav_back",
    "nav_forward",
    "zoom_in",
    "zoom_out",
    "zoom_reset",
    "browser_incognito",
    "clip_copy",
    "clip_paste",
    "clip_cut",
    "clip_select_all",
    "clip_clear",
    "edit_undo",
    "edit_redo",
    "key_enter",
    "key_escape",
    "key_space",
    "key_func",
    # File System & Device Operations
    "fs_mkdir",
    "fs_copy",
    "fs_move",
    "fs_rename",
    "fs_trash",
    "fs_empty_trash",
    "fs_clean_downloads",
    "fs_chmod_x",
    "fs_touch",
    "fs_archive",
    "fs_unpack",
    "doc_save",
    "doc_print",
    "dev_mount",
    "dev_eject",
    "sys_update",
    "fs_cut",
    "fs_properties",
    "fs_show_in_files",
    "display_res_set",
    # System questions & everyday Linux tasks (2026-11-11)
    "sys_disk_free",
    "sys_memory",
    "sys_cpu",
    "sys_ip",
    "sys_battery",
    "sys_info",
    "sys_time",
    "timer_set",
    "reminder_set",
    "wallpaper_set",
    "wifi_restart",
    "audio_output_set",
    "screen_off",
    "caffeine_on",
    "caffeine_off",
    "pkg_install",
    "pkg_remove",
    "bt_connect",
    "bt_disconnect",
)

ACTIONS = ("none", "translate") + BROWSER_ACTIONS + APP_ACTIONS + SYSTEM_ACTIONS

INTENT_VALID_ACTIONS = {
    "llm": ("none",),
    "translate": ("translate",),
    "browser": BROWSER_ACTIONS,
    "app": APP_ACTIONS,
    "system": SYSTEM_ACTIONS,
}
INTENT_ACTION_INDICES = {
    intent: tuple(ACTIONS.index(a) for a in acts)
    for intent, acts in INTENT_VALID_ACTIONS.items()
}

# Base encoder presets (all compatible with PreTrainedTokenizerFast.word_ids() and AutoModel):
# - "tiny"      : cointegrated/rubert-tiny2 (~29M params, 3 layers, 312d, ~3-4 ms on Ryzen 7 7840HS)
# - "small"     : intfloat/multilingual-e5-small (~118M params, 12 layers, 384d, ~9-12 ms, strong EN+RU awareness)
# - "base"      : intfloat/multilingual-e5-base (~278M params, 12 layers, 768d, ~15-20 ms, medium EN+RU encoder)
# - "user-base" : deepvk/USER-base (~178M params, 12 layers, 768d, Russian encoder)
MODEL_PRESETS = {
    "tiny": "cointegrated/rubert-tiny2",
    "small": "intfloat/multilingual-e5-small",
    "base": "intfloat/multilingual-e5-base",
    "e5-base": "intfloat/multilingual-e5-base",
    "user-base": "deepvk/USER-base",
}
BASE_MODEL = MODEL_PRESETS["base"]
MODEL_DIR = str(Path(__file__).resolve().parent / "ml_model")   # absolute path: independent of the working directory
MAX_LEN = 64
EDGE = " ,.;:!?«»\"'“”„"
_QUOTES = str.maketrans("", "", "«»\"“”„")


def norm(text: str) -> str:
    """Soft normalization for comparison: case, ё/е, quotes, spaces, punctuation at the edges."""
    cleaned = text.translate(_QUOTES).casefold().replace("ё", "е")
    return " ".join(cleaned.split()).strip(EDGE)


def _span(tags, label):
    """Indices of words tagged `label`; gaps of O between the first and the last one are filled for QUERY/TEXT
    (for SITE, only contiguous SITE tokens are valid). If the gap contains another label, it is not filled."""
    idx = [i for i, t in enumerate(tags) if t == label]
    if not idx:
        return []
    first, last = idx[0], idx[-1]
    if label == "SITE":
        if any(t != "SITE" for t in tags[first:last + 1]):
            return idx
        return list(range(first, last + 1))
    if any(t not in (label, "O") for t in tags[first:last + 1]):
        return idx
    return list(range(first, last + 1))


def assemble(words, tags) -> dict:
    """Word tags -> {"site", "query", "text"}. Text is taken only from the words of the phrase.
    Surrounding or paired guillemets/quotes («Интерстеллар», "Stable Diffusion") are stripped cleanly."""
    def build(label):
        raw = " ".join(words[i] for i in _span(tags, label)).strip(EDGE)
        if label in ("SITE", "QUERY"):
            raw = raw.translate(_QUOTES).strip(EDGE)
        return raw

    return {"site": build("SITE"), "query": build("QUERY"), "text": build("TEXT")}


THRESHOLD_INTENT = 0.80     # calibrated for 5 intents; tuneable via `python3 test_ml.py --sweep`
THRESHOLD_SLOT = 0.60       # below this for any word -> unsure about the tagging -> llm
THRESHOLD_LANG = 0.50       # target language below the threshold -> "none"
THRESHOLD_ACTION = 0.15     # calibrated for 144 actions (uniform is 1/144 = 0.0069; 0.15 is >21x uniform)


def fragmented(tags) -> str:
    """Ragged tagging:
    - For SITE: any non-SITE word (including O) between the first and last SITE word means a torn site span.
    - For QUERY / TEXT: a word of ANOTHER slot label between the first and last word means ragged tagging.
    Returns the label name or ''."""
    for label in ("SITE", "QUERY", "TEXT"):
        idx = [i for i, t in enumerate(tags) if t == label]
        if not idx:
            continue
        span = tags[idx[0]:idx[-1] + 1]
        if label == "SITE":
            if any(t != "SITE" for t in span):
                return label
        elif any(t not in (label, "O") for t in span):
            return label
    return ""




def decide(phrase: str, words, pred: dict, t_intent=THRESHOLD_INTENT, t_slot=THRESHOLD_SLOT,
           t_lang=THRESHOLD_LANG, t_action=THRESHOLD_ACTION) -> dict:
    """Model prediction (probabilities and word tags) -> decision where to route the phrase.

    Safety net: if the model is unsure or the tagging looks odd, the phrase is not executed blindly but
    goes to the big LLM (fallback=True). A pure function - tested without the model. No phrase rules here:
    the network decides, this function only applies confidence thresholds and structural sanity checks.
    """
    best = max(range(len(INTENTS)), key=lambda k: pred["intent_p"][k])
    intent, conf = INTENTS[best], pred["intent_p"][best]
    lbest = max(range(len(LANGS)), key=lambda k: pred["lang_p"][k])
    lang = LANGS[lbest] if pred["lang_p"][lbest] >= t_lang else "none"
    tags = list(pred["tags"])
    parts = assemble(words, tags)

    valid_idx = INTENT_ACTION_INDICES[intent]
    abest = max(valid_idx, key=lambda k: pred["action_p"][k])
    action, action_conf = ACTIONS[abest], float(pred["action_p"][abest])

    # Consistency between the action and the slots (structural, not phrase-based)
    if intent == "browser":
        if parts["site"] and not parts["query"]:
            action = "web_open"
        elif not parts["site"] and parts["query"] and action == "web_open":
            action = "web_search"
    elif intent == "app":
        action = "app_query" if parts["query"] else "app_open"
    elif intent == "translate":
        action = "translate"
    elif intent == "llm":
        action = "none"

    result = {
        "route": intent,
        "action": action,
        "site": parts["site"],
        "query": parts["query"],
        "text": parts["text"],
        "lang": lang if intent == "translate" else "none",
        "confidence": conf,
        "fallback": False,
        "reason": "",
        "phrase": phrase,
    }
    if intent in ("browser", "app"):
        result["text"] = ""
    elif intent == "system":
        pass                                    # TEXT stays: the message of reminder_set
    elif intent == "translate":
        result["site"] = result["query"] = ""
    else:
        result["site"] = result["query"] = result["text"] = ""
        result["action"] = "none"
        if conf < t_intent:
            result.update(fallback=True, reason=f"not sure about the intent ({conf:.2f})")
        return result

    reason = ""
    if pred.get("truncated"):
        reason = "phrase is too long"
    elif conf < t_intent:
        reason = f"not sure about the intent ({conf:.2f})"
    elif min(pred["tag_p"]) < t_slot:
        reason = f"not sure about the word tagging ({min(pred['tag_p']):.2f})"
    elif fragmented(tags):
        reason = f"ragged tagging ({fragmented(tags)})"
    elif intent == "browser" and not (result["site"] or result["query"]):
        reason = "there is a command, but no site or query was found"
    elif intent == "browser" and not result["site"] and "O" not in tags:
        reason = "siteless query without a search or play command verb"
    elif intent == "app" and not result["site"]:
        reason = "there is an app command, but no app or folder was found"
    elif intent == "system" and (action == "none" or action_conf < t_action):
        reason = "there is a system command, but no action was found"
    elif intent == "translate" and not result["text"]:
        reason = "there is a translation request, but no text was found"
    if reason:
        result.update(route="llm", action="none", site="", query="", text="", lang="none", fallback=True, reason=reason)
    return result


def _wkey(word: str) -> str:
    return word.translate(_QUOTES).casefold().replace("ё", "е").strip(EDGE)


def find_words(words, chunk: str, used=()) -> list:
    """Indices of consecutive words of the phrase equal to `chunk` (case, ё/е and edge punctuation do not matter)."""
    target = [_wkey(w) for w in chunk.split() if _wkey(w)]
    if not target:
        return []
    keys = [_wkey(w) for w in words]
    for start in range(len(words) - len(target) + 1):
        span = list(range(start, start + len(target)))
        if keys[start:start + len(target)] == target and not any(i in used for i in span):
            return span
    raise ValueError(f"the phrase {' '.join(words)!r} has no consecutive words {chunk!r}")


def tag_phrase(phrase: str, site: str = "", query: str = "", text: str = ""):
    """Phrase + its pieces -> (words, tags). Needed to turn your real phrases into training examples."""
    words = phrase.split()
    tags = ["O"] * len(words)
    used = set()
    slots = sorted((("SITE", site), ("QUERY", query), ("TEXT", text)), key=lambda kv: -len(kv[1].split()))
    for label, chunk in slots:
        for i in find_words(words, chunk, used):
            tags[i] = label
            used.add(i)
    return words, tags


# =============================================================================
# Hand-labelled data files (training: ml_real.tsv, exam: ml_mine.tsv)
# =============================================================================
def read_tsv(path):
    """TSV -> [(phrase, intent, site, query, text, language, action)]. Lines starting with # are skipped.
    Slots are stored already clean (consecutive words of the phrase); `action` is the canonical action
    ("none" for llm, "translate" for translate). Nothing is inferred here: the data is the contract."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split("\t") + [""] * 7
            phrase, intent, site, query, text, lang, action = (x.strip() for x in p[:7])
            lang = lang or "none"
            if not action:
                action = {"llm": "none", "translate": "translate", "app": "app_query" if query else "app_open"}.get(intent, "")
            rows.append((phrase, intent, site, query, text, lang, action))
    return rows
