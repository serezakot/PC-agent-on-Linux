"""Корпус системных и app-команд, написанный вручную (независимо от генератора обучающих данных).

Каждый случай — фраза, как её сказал бы человек, и что ДОЛЖНА вернуть модель:
    action — каноническое действие (ml_spec.SYSTEM_ACTIONS или app_open / app_query)
    query  — аргумент: имя файла/папки, процент, длительность, приложение (дословно слова фразы)
    site   — место назначения / новое имя / программа для route == "app"
    text   — только для reminder_set: текст напоминания
    check  — подстрока, которая обязана быть в ответе исполнителя в --dry-run (проверка аргументов, не только маршрута)
tag — группа для отчёта; «reach» — формулировки, далёкие от обучающих данных (в зачёт модели не идут).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SysCase:
    phrase: str
    action: str
    query: str = ""
    site: str = ""
    text: str = ""
    check: str = ""
    tag: str = ""
    route: str = "system"


def S(phrase, action, query="", site="", text="", check="", tag=""):
    return SysCase(phrase, action, query, site, text, check, tag or action.split("_")[0])


def A(phrase, site, query="", check="", tag="app"):
    return SysCase(phrase, "app_query" if query else "app_open", query, site, "", check, tag, route="app")


FILES = [
    S("скопируй файл ml_log", "fs_copy", "ml_log", check="буфер", tag="files"),
    S("скопируй папку traning ml", "fs_copy", "traning ml", check="буфер", tag="files"),
    S("скопируй report_final.pdf на рабочий стол", "fs_copy", "report_final.pdf", "рабочий стол", check="Desktop", tag="files"),
    S("сделай копию папки проекты в документы", "fs_copy", "проекты", "документы", tag="files"),
    S("продублируй файл main.py", "fs_copy", "main.py", tag="files"),
    S("скопируй файл main.py с рабочего стола", "fs_copy", "main.py", "рабочего стола", check="буфер", tag="files"),
    S("перемести rubert-first-layer-intent-parser-lab в Desktop", "fs_move", "rubert-first-layer-intent-parser-lab", "Desktop", check="Desktop", tag="files"),
    S("перенеси папку education на рабочий стол", "fs_move", "education", "рабочий стол", tag="files"),
    S("перекинь отчёт.docx в загрузки", "fs_move", "отчёт.docx", "загрузки", check="Downloads", tag="files"),
    S("вырежи папку ml_model", "fs_cut", "ml_model", check="Вырезаю", tag="files"),
    S("вставь в папку education", "clip_paste", site="education", check="education", tag="files"),
    S("вставь скопированное на рабочий стол", "clip_paste", site="рабочий стол", check="Desktop", tag="files"),
    S("переименуй папку education в учёба", "fs_rename", "education", "учёба", check="учёба", tag="files"),
    S("переименуй файл заметки.txt в todo", "fs_rename", "заметки.txt", "todo", tag="files"),
    S("удали файл ml_log", "fs_trash", "ml_log", check="Корзину", tag="files"),
    S("отправь папку traning ml в корзину", "fs_trash", "traning ml", check="Корзину", tag="files"),
    S("выкинь старый_бэкап.zip", "fs_trash", "старый_бэкап.zip", tag="files"),
    S("сделай зип из папки education", "fs_archive", "education", check=".zip", tag="files"),
    S("заархивируй папку проекты", "fs_archive", "проекты", check=".zip", tag="files"),
    S("распакуй архив lab.zip на рабочий стол", "fs_unpack", "lab.zip", "рабочий стол", check="Desktop", tag="files"),
    S("покажи свойства папки education", "fs_properties", "education", check="свойства", tag="files"),
    S("сколько весит папка загрузки", "fs_properties", "загрузки", tag="files"),
    S("где лежит файл assistant.py", "fs_show_in_files", "assistant.py", check="assistant.py", tag="files"),
    S("создай папку новый проект на рабочем столе", "fs_mkdir", "новый проект", "рабочем столе", check="Desktop", tag="files"),
    S("создай папку скрины в загрузках", "fs_mkdir", "скрины", "загрузках", check="Downloads", tag="files"),
    S("создай файл todo.txt в папке education", "fs_touch", "todo.txt", "education", check="todo.txt", tag="files"),
    S("создай текстовый файл заметки на рабочем столе", "fs_touch", "заметки", "рабочем столе", check="заметки.txt", tag="files"),
    S("сделай файл setup.sh исполняемым", "fs_chmod_x", "setup.sh", check="chmod", tag="files"),
    S("очисти корзину", "fs_empty_trash", check="Корзин", tag="files"),
    S("почисти загрузки", "fs_clean_downloads", check="Загрузки", tag="files"),
    S("слушай закинь фотку море.jpg в папку отпуск", "fs_move", "море.jpg", "отпуск", tag="reach"),
    S("грохни файл temp_old.log", "fs_trash", "temp_old.log", tag="reach"),
]

AUDIO = [
    S("выключи звук", "vol_mute", check="звук", tag="audio"),
    S("включи звук", "vol_unmute", tag="audio"),
    S("поставь громкость на 30", "vol_set", "30", check="30%", tag="audio"),
    S("поставь звук на 20 процентов", "vol_set", "20 процентов", check="20%", tag="audio"),
    S("сделай погромче", "vol_up", check="10%", tag="audio"),
    S("убавь громкость на 50", "vol_down", "50", check="50%", tag="audio"),
    S("выключи микрофон", "mic_mute", tag="audio"),
    S("включи микрофон", "mic_unmute", tag="audio"),
    S("переключи звук на наушники", "audio_output_set", "наушники", check="наушники", tag="audio"),
    S("выведи звук на колонки", "audio_output_set", "колонки", tag="audio"),
    S("звук через hdmi", "audio_output_set", "hdmi", tag="audio"),
]

DISPLAY = [
    S("убавь яркость на 50", "bright_down", "50", check="50", tag="display"),
    S("поставь яркость на 70", "bright_set", "70", check="70", tag="display"),
    S("сделай экран ярче", "bright_up", tag="display"),
    S("включи ночной свет", "night_light_on", tag="display"),
    S("выключи ночной свет", "night_light_off", tag="display"),
    S("включи тёмную тему", "theme_dark", check="dark", tag="display"),
    S("поставь светлую тему", "theme_light", tag="display"),
    S("поставь разрешение 1920 на 1080", "display_res_set", "1920 на 1080", check="1920x1080", tag="display"),
    S("переключи разрешение экрана на 2560x1440", "display_res_set", "2560x1440", check="2560x1440", tag="display"),
    S("поставь разрешение фулл хд", "display_res_set", "фулл хд", check="1920x1080", tag="display"),
    S("верни родное разрешение", "display_res_set", "родное", check="preferred", tag="display"),
    S("выключи уведомления", "dnd_on", check="уведомления", tag="display"),
    S("включи уведомления", "dnd_off", tag="display"),
    S("выключи экран", "screen_off", check="экран", tag="display"),
    S("не выключай экран", "caffeine_on", tag="display"),
    S("верни затухание экрана", "caffeine_off", tag="display"),
    S("смени обои", "wallpaper_set", check="фона", tag="display"),
    S("поставь обои закат.jpg", "wallpaper_set", "закат.jpg", tag="display"),
]

NETWORK = [
    S("включи вайфай", "wifi_on", check="Wi-Fi", tag="network"),
    S("выключи wifi", "wifi_off", tag="network"),
    S("перезапусти вайфай", "wifi_restart", tag="network"),
    S("включи блютуз", "bt_on", tag="network"),
    S("выключи bluetooth", "bt_off", tag="network"),
    S("подключи наушники", "bt_connect", "наушники", check="наушники", tag="network"),
    S("отключи колонку jbl", "bt_disconnect", "колонку jbl", tag="network"),
    S("включи режим полёта", "airplane_on", tag="network"),
    S("какой у меня айпи", "sys_ip", check="IP", tag="network"),
]

POWER = [
    S("выключи компьютер", "power_off", check="poweroff", tag="power"),
    S("выключи компьютер через час", "power_off", "час", check="+60", tag="power"),
    S("перезагрузи компьютер", "power_reboot", check="reboot", tag="power"),
    S("отмени выключение компьютера", "power_cancel", tag="power"),
    S("переведи в спящий режим", "power_sleep", check="suspend", tag="power"),
    S("заблокируй компьютер", "session_lock", tag="power"),
    S("включи режим энергосбережения", "power_saver", check="power-saver", tag="power"),
    S("поставь режим производительности", "power_perf", check="performance", tag="power"),
]

WINDOWS = [
    S("закрой окно", "win_close_active", tag="windows"),
    S("закрой все окна", "win_close_all", tag="windows"),
    S("сверни окно", "win_min_active", tag="windows"),
    S("сверни все окна", "win_min_all", tag="windows"),
    S("разверни окно на весь экран", "win_max_active", tag="windows"),
    S("переключи на прошлое окно", "win_switch", tag="windows"),
    S("покажи все окна", "win_overview", tag="windows"),
    S("прикрепи окно влево", "win_tile_left", tag="windows"),
    S("окно вправо", "win_tile_right", tag="windows"),
    S("переключи на следующий рабочий стол", "ws_next", tag="windows"),
    S("закрой телеграм", "win_close_app", "телеграм", check="телеграм", tag="windows"),
    S("закрой калькулятор", "win_close_app", "калькулятор", check="calculator", tag="windows"),
    S("закрой доту", "win_close_app", "доту", tag="windows"),
    S("выруби стим", "win_close_app", "стим", check="steam", tag="windows"),
    S("закрой браузер", "win_close_browser", tag="windows"),
]

MEDIA_KEYS = [
    S("поставь на паузу", "media_pause", tag="media"),
    S("переключи трек", "media_next", tag="media"),
    S("включи предыдущее видео", "media_prev", tag="media"),
    S("перемотай видео на 20 секунд вперёд", "media_seek_fwd", "20 секунд", check="20", tag="media"),
    S("включи субтитры", "video_subtitles", tag="media"),
    S("открой новую вкладку", "tab_new", tag="keys"),
    S("закрой вкладку", "tab_close", tag="keys"),
    S("восстанови закрытую вкладку", "tab_reopen", tag="keys"),
    S("обнови страницу", "page_reload", tag="keys"),
    S("прокрути вниз", "page_scroll_down", tag="keys"),
    S("переключи раскладку", "kb_layout", tag="keys"),
    S("отмени последнее действие", "edit_undo", tag="keys"),
    S("сделай скриншот", "shot_screen", check="Снимок", tag="screenshots"),
    S("сделай скриншот области", "shot_area", tag="screenshots"),
]

INFO_EVERYDAY = [
    S("сколько места на диске", "sys_disk_free", tag="info"),
    S("сколько оперативки свободно", "sys_memory", tag="info"),
    S("что грузит процессор", "sys_cpu", tag="info"),
    S("сколько заряда батареи", "sys_battery", tag="info"),
    S("какая версия убунту", "sys_info", tag="info"),
    S("который час", "sys_time", tag="info"),
    S("поставь таймер на 10 минут", "timer_set", "10 минут", check="10 мин", tag="everyday"),
    S("засеки 30 секунд", "timer_set", "30 секунд", check="30 сек", tag="everyday"),
    S("напомни через 20 минут позвонить маме", "reminder_set", "20 минут", text="позвонить маме", check="20 минут", tag="everyday"),
    S("напомни в 18:00 про встречу", "reminder_set", "18:00", text="про встречу", check="18:00", tag="everyday"),
    S("разбуди меня в 7 утра", "reminder_set", "7 утра", check="7 утра", tag="everyday"),
    S("установи obsidian", "pkg_install", "obsidian", tag="everyday"),
    S("удали программу discord", "pkg_remove", "discord", tag="everyday"),
]

APPS = [
    A("открой калькулятор", "калькулятор", check="Calculator"),
    A("запусти блокнот", "блокнот", check="Text"),
    A("открой проводник", "проводник", check="Nautilus"),
    A("открой терминал", "терминал"),
    A("открой браузер", "браузер"),
    A("открой календарь", "календарь", check="alendar"),
    A("открой настройки звука", "настройки звука", check="sound"),
    A("открой загрузки", "загрузки", check="Downloads"),
    A("открой загрузки и отсортируй по дате", "загрузки", "отсортируй по дате", check="Downloads"),
    A("открой traning ml", "traning ml", check="traning ml"),
    A("открой папку education", "education", check="education"),
    A("запусти доту", "доту", check="rungameid/570"),
    A("открой стим и найди игру контр страйк", "стим", "контр страйк", check="steam"),
    A("запусти пайчарм", "пайчарм", check="pycharm"),
    A("открой обсидиан", "обсидиан", check="obsidian"),
    A("открой терминал в папке education", "терминал", "в папке education", check="education"),
    A("запусти спотифай и включи the weeknd", "спотифай", "the weeknd", check="spotify:search", tag="app_reach"),
]


MORE = [
    S("поставь микрофон на 40", "mic_set", "40", check="40%", tag="audio"),
    S("прибавь микрофон", "mic_up", tag="audio"),
    S("убавь микрофон", "mic_down", tag="audio"),
    S("увеличь масштаб экрана", "scale_up", tag="display"),
    S("сбрось масштаб экрана", "scale_down", tag="display"),
    S("выключи тачпад", "touchpad_off", tag="display"),
    S("включи точку доступа", "hotspot_on", tag="network"),
    S("включи впн", "vpn_toggle", tag="network"),
    S("переведи в режим гибернации", "power_hibernate", tag="power"),
    S("выйди из системы", "session_logout", tag="power"),
    S("включи запись экрана", "rec_start", tag="screenshots"),
    S("сделай снимок окна", "shot_window", tag="screenshots"),
    S("сделай скриншот в буфер обмена", "shot_clipboard", tag="screenshots"),
    S("сними с паузы", "media_resume", tag="media"),
    S("отмотай на 10 секунд назад", "media_seek_back", "10 секунд", check="10", tag="media"),
    S("сделай видео на весь экран", "video_fullscreen", tag="media"),
    S("ускорь видео", "video_speed_up", tag="media"),
    S("закрепи окно поверх всех окон", "win_always_on_top", tag="windows"),
    S("переключи на предыдущий рабочий стол", "ws_prev", tag="windows"),
    S("выйди из полноэкранного режима", "win_exit_fullscreen", tag="windows"),
    S("следующая вкладка", "tab_next", tag="keys"),
    S("предыдущая вкладка", "tab_prev", tag="keys"),
    S("прокрути в начало страницы", "page_scroll_top", tag="keys"),
    S("вернись назад в браузере", "nav_back", tag="keys"),
    S("увеличь масштаб страницы", "zoom_in", tag="keys"),
    S("открой окно инкогнито", "browser_incognito", tag="keys"),
    S("скопируй выделенный текст", "clip_copy", tag="keys"),
    S("выдели весь текст", "clip_select_all", tag="keys"),
    S("повтори действие", "edit_redo", tag="keys"),
    S("нажми эскейп", "key_escape", tag="keys"),
    S("сохрани документ", "doc_save", tag="keys"),
    S("извлеки флешку", "dev_eject", tag="power"),
    S("обнови систему", "sys_update", tag="power"),
]

ALL_SYSTEM = MORE + FILES + AUDIO + DISPLAY + NETWORK + POWER + WINDOWS + MEDIA_KEYS + INFO_EVERYDAY
ALL = ALL_SYSTEM + APPS
