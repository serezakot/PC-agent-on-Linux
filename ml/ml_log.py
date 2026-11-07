#!/usr/bin/env python3
"""Parse log: automatic recording of all phrases sent to the assistant.

By default, every phrase handled by `gogle_search.py` or `ml_infer.py` is appended to `parse_log.jsonl`
next to the scripts (one JSON line per command: timestamp, phrase, route, site, query, text, lang,
confidence, fallback, reason, and optional url).

Control via environment variable (optional):
    export ML_PARSE_LOG=0                       # disable logging
    export ML_PARSE_LOG=/path/to/custom.jsonl   # custom log file path

Reviewing the log (e.g. after a month of real usage):
    python3 ml_log.py --stats                   # summary statistics across all logged phrases
    python3 ml_log.py                           # stats summary + doubtful phrases in ml_real.tsv format
    python3 ml_log.py --all > ml_review.tsv     # export all unique phrases in ml_real.tsv format

Open ml_review.tsv, fix any wrong rows (TAB-separated: phrase, intent, site, query, text, language)
and append them to ml_real.tsv before retraining.
"""
from collections import Counter
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().with_name("parse_log.jsonl")
DOUBT = 0.90        # confidence below this value marks a phrase as "doubtful" for review


def log_path():
    """Returns the active log file path (DEFAULT_PATH by default, or None if ML_PARSE_LOG=0/off/false/no)."""
    v = os.environ.get("ML_PARSE_LOG", "").strip()
    if v.lower() in {"0", "off", "false", "no"}:
        return None
    if not v or v.lower() in {"1", "on", "true", "yes"}:
        return DEFAULT_PATH
    return Path(v)


def log_result(result: dict, path=None, url: str = "") -> bool:
    """Appends one parsed command to parse_log.jsonl. Never crashes the caller on I/O errors."""
    path = path if path is not None else log_path()
    if path is None or not result or not (result.get("phrase") or "").strip():
        return False
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S")}
    row.update({k: result.get(k, "") for k in ("phrase", "route", "site", "query", "text", "lang", "confidence",
                                               "fallback", "reason")})
    if url:
        row["url"] = url
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_log(path=None) -> list:
    path = Path(path if path is not None else (log_path() or DEFAULT_PATH))
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


def summarize(rows: list) -> str:
    """Human-readable statistics across all logged commands (for evaluating a month of real usage)."""
    if not rows:
        return "Лог пуст."
    total = len(rows)
    unique = len({_key(r.get("phrase", "")) for r in rows if r.get("phrase")})
    routes = Counter(r.get("route", "llm") for r in rows)
    fallbacks = sum(1 for r in rows if r.get("fallback"))
    low_conf = sum(1 for r in rows if not r.get("fallback") and float(r.get("confidence") or 0) < DOUBT)
    confs = [float(r.get("confidence") or 0) for r in rows]
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    t_first = rows[0].get("t", "?")
    t_last = rows[-1].get("t", "?")
    route_str = ", ".join(f"{k}: {v} ({v / total:.0%})" for k, v in routes.most_common())
    return (
        f"# Статистика parse_log.jsonl ({t_first} .. {t_last})\n"
        f"# Всего команд: {total} (уникальных фраз: {unique}) | средняя уверенность: {avg_conf:.2f}\n"
        f"# Маршруты: {route_str}\n"
        f"# На подстраховку в LLM (fallback): {fallbacks} ({fallbacks / total:.1%}) | "
        f"сомнительных (<{DOUBT:.2f}): {low_conf}"
    )


def to_tsv(rows, only_doubtful=True) -> list:
    """Log -> rows in ml_real.tsv format (one per unique phrase; doubtful ones first, marked with #)."""
    seen, doubtful, sure = set(), [], []
    for r in rows:
        phrase = (r.get("phrase") or "").strip()
        k = _key(phrase)
        if not phrase or k in seen or "\t" in phrase:
            continue
        seen.add(k)
        route = r.get("route", "llm")
        lang = r.get("lang", "none") if route == "translate" else "none"
        site = r.get("site", "") if route in ("browser", "app") else ""
        query = r.get("query", "") if route in ("browser", "app", "system") else ""
        text = r.get("text", "") if route == "translate" else ""
        line = "\t".join([phrase, route, site, query, text, lang or "none"])
        unsure = bool(r.get("fallback")) or float(r.get("confidence") or 0) < DOUBT
        if unsure:
            why = r.get("reason") or f"confidence {float(r.get('confidence') or 0):.2f}"
            doubtful.append(f"# review: {why}\n{line}")
        elif not only_doubtful:
            sure.append(line)
    return doubtful + sure


def main(argv) -> int:
    path = Path(argv[argv.index("--file") + 1]) if "--file" in argv else (log_path() or DEFAULT_PATH)
    rows = read_log(path)
    if not rows:
        print(f"Лог пуст или ещё не создан: {path}", file=sys.stderr)
        return 1
    print(summarize(rows), file=sys.stdout if "--stats" in argv else sys.stderr)
    if "--stats" in argv:
        return 0
    for line in to_tsv(rows, only_doubtful="--all" not in argv):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
