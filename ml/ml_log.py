#!/usr/bin/env python3
"""Журнал разобранных команд: лучший источник новых обучающих фраз — ваши настоящие команды.

Включение (по умолчанию журнал выключен, ничего не пишется):
    export ML_PARSE_LOG=1                       # писать в parse_log.jsonl рядом со скриптами
    export ML_PARSE_LOG=/путь/к/файлу.jsonl     # или в свой файл
ml_infer.parse() сам допишет строку на каждую фразу: что сказали и что решила модель.

Разбор журнала:
    python3 ml_log.py                            # сомнительные (ушли в LLM или низкая уверенность) сверху
    python3 ml_log.py --all > ml_review.tsv      # все фразы в формате ml_real.tsv
Откройте ml_review.tsv, ИСПРАВЬТЕ неверные строки (поля через TAB: фраза, намерение, site, query, text, язык)
и вставьте их в ml_real.tsv. Исправлять нужно только ошибки, верные строки тоже можно оставить: они лишними не будут.
"""
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("parse_log.jsonl")
DOUBT = 0.90        # уверенность ниже — «сомнительная» фраза


def log_path():
    v = os.environ.get("ML_PARSE_LOG", "").strip()
    if not v or v.lower() in {"0", "off", "false", "no"}:
        return None
    return DEFAULT_PATH if v.lower() in {"1", "on", "true", "yes"} else Path(v)


def log_result(result: dict, path=None) -> bool:
    """Дописывает строку в журнал. Никогда не роняет программу: ошибка записи просто игнорируется."""
    path = path or log_path()
    if path is None:
        return False
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S")}
    row.update({k: result.get(k, "") for k in ("phrase", "route", "site", "query", "text", "lang", "confidence",
                                               "fallback", "reason")})
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_log(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _key(phrase: str) -> str:
    return " ".join(phrase.casefold().replace("ё", "е").split())


def to_tsv(rows, only_doubtful=True) -> list:
    """Журнал -> строки в формате ml_real.tsv (по одной на фразу; сомнительные первыми, с пометкой #)."""
    seen, doubtful, sure = set(), [], []
    for r in rows:
        phrase = (r.get("phrase") or "").strip()
        k = _key(phrase)
        if not phrase or k in seen or "\t" in phrase:
            continue
        seen.add(k)
        route = r.get("route", "llm")
        lang = r.get("lang", "none") if route == "translate" else "none"
        site = r.get("site", "") if route == "browser" else ""
        query = r.get("query", "") if route == "browser" else ""
        text = r.get("text", "") if route == "translate" else ""
        line = "\t".join([phrase, route, site, query, text, lang or "none"])
        unsure = bool(r.get("fallback")) or float(r.get("confidence") or 0) < DOUBT
        if unsure:
            why = r.get("reason") or f"уверенность {float(r.get('confidence') or 0):.2f}"
            doubtful.append(f"# проверить: {why}\n{line}")
        elif not only_doubtful:
            sure.append(line)
    return doubtful + sure


def main(argv) -> int:
    path = Path(argv[argv.index("--file") + 1]) if "--file" in argv else (log_path() or DEFAULT_PATH)
    rows = read_log(path)
    if not rows:
        print(f"журнал пуст или не найден: {path}\nВключите его: export ML_PARSE_LOG=1", file=sys.stderr)
        return 1
    for line in to_tsv(rows, only_doubtful="--all" not in argv):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
