"""Весь корпус: фразы вручную (живые, как сказал бы человек) + воспроизводимо сгенерированные.

    from cases import all_cases, HAND, GENERATED
    all_cases()                    # без повторов; сначала ручные
    all_cases(generated=False)     # только ручные
"""
from cases_base import Case, expected_kind          # noqa: F401  (реэкспорт)
from cases_gen import (google_cases, play_cases, site_only_cases, site_query_cases, translate_cases)
from cases_hand_browser import GOOGLE, PLAY, SITE_ONLY, SITE_QUERY
from cases_hand_translate import LLM, TRANSLATE

HAND = SITE_QUERY + SITE_ONLY + GOOGLE + PLAY + TRANSLATE + LLM
GENERATED = (site_query_cases(380) + site_only_cases(70) + google_cases(120) + play_cases(60) + translate_cases(330))


def _key(phrase: str) -> str:
    return " ".join(phrase.casefold().replace("ё", "е").split())


def all_cases(generated: bool = True) -> list:
    seen, out = set(), []
    for c in HAND + (GENERATED if generated else []):
        k = _key(c.phrase)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out
