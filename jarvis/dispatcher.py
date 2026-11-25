"""Фраза -> модель первого слоя -> исполнитель (browser / translate / app / system) или второй слой (llm).

Опасные действия (удалить, переместить, выключить…) выполняются только после «да» — и в первом, и во втором слое.
Для каждой фразы ведётся trace: кто её обработал и сколько заняли разбор и выполнение (печатает describe_trace).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import apps, browser, system
from .parser import ModelError, describe_decision, model_parse


def dispatch_phrase(phrase: str, parser=None, dry_run: bool = False, agent=None, confirm=None, notify=None):
    """Разбирает фразу моделью и направляет в нужный исполнитель (browser/translate/app/system/llm).
    parser — разбор фразы (по умолчанию модель первого слоя); agent — второй слой (llm.LLMAgent), по умолчанию
    общий агент сессии (помнит разговор).
    confirm(вопрос) -> bool — подтверждение опасных действий в обоих слоях (по умолчанию вопрос в терминале).
    notify(фраза) — сказать сразу, не дожидаясь ответа: «думаю», когда фраза уходит второму слою."""
    t0 = time.perf_counter()
    parse = parser or model_parse
    result = dict(parse(phrase))
    # trace — кто и сколько работал над фразой (для отчёта в терминале); исполнители его не читают
    result["trace"] = trace = {"parse_s": time.perf_counter() - t0, "layer": "parser"}
    t_exec = time.perf_counter()
    try:
        return _dispatch(phrase, result, dry_run, agent, confirm, notify)
    finally:
        trace["exec_s"] = time.perf_counter() - t_exec


EARLY_END_CONFIDENCE = 0.98
# Фраза на предлог или союз не кончается: «поставь громкость на…», «скопируй в…», «найди и…» — будет продолжение.
_DANGLING = {"на", "в", "во", "до", "с", "со", "к", "ко", "по", "про", "для", "из", "от", "за", "под", "над", "о", "об",
             "и", "а", "но", "или", "чтобы", "что", "как", "где", "мне", "это"}


def is_complete_command(phrase: str) -> bool:
    """Ясная полная команда — можно не ждать конца паузы (догадка во время паузы, listen.EARLY_END_MS). Только когда
    сомнений нет: модель уверена, это не вопрос, не перевод и не опасное действие, и у команды есть настоящая цель —
    программа или папка, которая на слух совпала со сказанным, сайт, запрос, имя файла, или действие без цели
    («сделай громче»). «Открой папку…», «удали файл…», «поставь таймер на…», «сделай…» — ждём паузу целиком:
    лучше лишние 0.4 с, чем оборвать."""
    from . import llm
    from .parser import peek
    r = peek(phrase)
    route, action = r.get("route"), r.get("action") or ""
    site, query = (r.get("site") or "").strip(), (r.get("query") or "").strip()
    if (route not in ("app", "browser", "system") or r.get("fallback") or (r.get("confidence") or 0) < EARLY_END_CONFIDENCE
            or len(phrase.split()) < 2 or phrase.split()[-1] in _DANGLING or action in llm.DANGEROUS):
        return False
    if route == "app":                                     # только быстрое: программы, стандартные папки, игры
        home = Path.home()                                 # (поиск файлов по дому — долгий; ваши папки ждут паузу)
        if apps._match_standard_folder(apps._norm(site), query, home, apps.xdg_user_dirs(home)) is not None:
            return True
        found = apps.find_desktop_app(site)                # «файл» -> File Roller по ключевым словам — это не «стим»
        if found is not None and max(apps.name_similarity(site, n) for n in found.names) >= 0.9:
            return True
        return apps.find_steam_game(site, min_score=0.9) is not None
    if route == "browser":                                 # «найди на ютубе…» — ждём, что найти
        return bool(query) or (action == "web_open" and bool(site) and len(phrase.split()) == 2)
    try:
        plan = system.make_system_plan(query=query, action=action, site=site, text=r.get("text", ""))
    except system.SystemCommandError:
        return False
    return plan is not None and plan.category != "refused" and (bool(query) or not action.startswith("fs_"))


def _dispatch(phrase: str, result: dict, dry_run: bool, agent, confirm=None, notify=None):
    trace = result["trace"]
    route = result.get("route")
    action = result.get("action") or ""

    # «открой traning ml»: если так называется папка или файл пользователя, это локальный объект, а не сайт
    if route == "browser" and action == "web_open" and result.get("site") and not result.get("query"):
        local = apps.find_local_item(result["site"])
        if local is not None:
            result.update(route="app", action="app_open")
            trace["local_override"] = True
            route, action = "app", "app_open"

    if route in ("browser", "translate"):
        plan = browser.plan_for(result)        # распознавание сайта только читает сеть: и в dry-run URL настоящий
        if plan is None:
            return result, None, "Искать нечего." if route == "browser" else "Переводить нечего."
        if not dry_run:
            browser.open_in_browser(plan.url)
        return result, plan, f"[dry-run] {plan.url}" if dry_run else browser._describe(plan)

    if route == "app":
        plan = apps.make_app_plan(result.get("site", ""), result.get("query", ""))
        if plan is None:
            return result, None, "Не указана программа или папка для открытия."
        if plan.target != "app_store_search":
            return result, plan, apps.execute_app_plan(plan, dry_run=dry_run)
        # по словам пользователя программа не нашлась («блокнот», «проводник»): по смыслу её найдёт второй слой
        result.update(fallback=True, reason=f"первый слой не нашёл программу «{result.get('site', '')}» на компьютере")

    if route == "system":
        plan = system.make_system_plan(query=result.get("query", ""), action=action, site=result.get("site", ""),
                                       text=result.get("text", ""))
        if plan is None:
            return result, None, f"Системная команда не распознана: {result.get('query') or phrase}"
        # Опасное действие (удалить, переместить, выключить...) — только после «да», как и во втором слое: модель бывает
        # уверенно неправа («выруби звук» -> power_off), а вопрос показывает, ЧТО именно будет сделано.
        from . import llm
        if action in llm.DANGEROUS and not dry_run and plan.category != "refused":
            ask = confirm or llm._ask_yes_no
            trace["confirm_asked"] = True
            if not ask(f"{plan.description}. Выполнить?"):
                trace["confirmed"] = False
                return result, plan, "Хорошо, не выполняю."
            trace["confirmed"] = True
        msg = system.execute_system_plan(plan, dry_run=dry_run)
        return result, plan, msg

    # route == "llm": второй слой (вопросы, многошаговые и неуверенные команды)
    from . import llm
    hint = result.get("reason", "") if result.get("fallback") else ""
    trace["layer"] = "llm"
    if notify is not None:
        from . import speak
        notify(speak.thinking())
    try:
        agent = agent or llm.get_agent(dry_run)
        if confirm is not None:
            agent.executor.confirm = confirm
        return result, None, agent.handle(phrase, hint=hint)
    except llm.LLMUnavailable as exc:
        trace["llm_error"] = str(exc)
        return result, None, f"Это задача для большой LLM, но она недоступна: {exc}."
    finally:
        if agent is not None:
            prov = getattr(agent, "provider", None)
            trace["llm_model"] = f"{prov.name}/{prov.model}" if prov else "?"
            executor = getattr(agent, "executor", None)
            trace["tools"] = list(getattr(executor, "log", []) or [])


def describe_trace(result: dict) -> str:
    """Отчёт: кто обработал фразу и сколько это заняло."""
    tr = result.get("trace") or {}
    parse_s, exec_s = tr.get("parse_s", 0.0), tr.get("exec_s", 0.0)
    conf = result.get("confidence")
    conf_s = f", уверенность {conf:.2f}" if isinstance(conf, (int, float)) else ""
    if tr.get("layer") != "llm":
        what = f"{result.get('route')}/{result.get('action') or '—'}"
        note = " (локальная папка/файл вместо сайта)" if tr.get("local_override") else ""
        if tr.get("confirm_asked"):
            note += " (подтверждено)" if tr.get("confirmed") else " (отменено пользователем)"
        return (f"⏱ ответил ПЕРВЫЙ СЛОЙ — парсер: {what}{conf_s}{note}. "
                f"Разбор {parse_s:.2f} с + выполнение {exec_s:.2f} с = {parse_s + exec_s:.2f} с")
    why = f"парсер не уверен: {result.get('reason')}" if result.get("fallback") else "парсер решил, что это задача для LLM"
    lines = [f"⏱ парсер ({parse_s:.2f} с{conf_s}) передал фразу ВТОРОМУ СЛОЮ — {why}"]
    if tr.get("llm_error"):
        lines.append(f"   LLM недоступна: {tr['llm_error']}")
    else:
        tools = tr.get("tools") or []
        lines.append(f"   ответила LLM {tr.get('llm_model', '?')}: "
                     + (f"инструментов вызвано {len(tools)}" if tools else "ответ без действий"))
        lines += [f"     · {t[:160]}" for t in tools]
    lines.append(f"   LLM {exec_s:.2f} с, всего {parse_s + exec_s:.2f} с")
    return "\n".join(lines)


def _crash_log(phrase: str, exc: BaseException) -> str:
    """Полная трассировка неожиданной ошибки — в ~/.cache/pc_agent/errors.log (для разбора), путь возвращается."""
    import traceback
    from pathlib import Path
    try:
        path = Path.home() / ".cache" / "pc_agent" / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} «{phrase}»\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return str(path)
    except OSError:
        return ""


def run_phrase(phrase: str, confirm=None, notify=None) -> str:
    """Выполняет одну фразу, печатает отчёт и возвращает ответ для голоса."""
    from . import speak
    try:
        result, plan, msg = dispatch_phrase(phrase, confirm=confirm, notify=notify)
    except (ModelError, browser.BrowserError, apps.AppLaunchError,
            system.SystemCommandError) as exc:
        print(f"Ошибка: {exc}")
        return f"Не получилось: {exc}"
    except Exception as exc:                          # noqa: BLE001 — одна сломанная команда не должна ронять ассистента
        log = _crash_log(phrase, exc)
        print(f"Не получилось: {type(exc).__name__}: {exc}" + (f" (подробности: {log})" if log else ""))
        return "Что-то пошло не так, подробности в журнале ошибок."
    print(describe_decision(result))
    if msg:
        print(msg)
    print(describe_trace(result))
    return speak.spoken_reply(result, plan, msg)
