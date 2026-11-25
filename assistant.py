#!/usr/bin/env python3
"""Джарвис — голосовой ассистент для Ubuntu 24.04. Единственная команда запуска (подробно — README.md):

    python3 assistant.py                  # слушает «Джарвис» (в терминале можно и печатать)
    python3 assistant.py "открой стим"    # одна команда текстом
    python3 assistant.py --install        # работать в фоне (запуск при входе в систему)
    python3 assistant.py --uninstall      # убрать из фона
"""
import os
import sys

# numpy (звук, признаки речи) — в один поток: иначе 16 потоков OpenBLAS крутятся вхолостую после каждой фразы, и
# распознавание музыки из динамиков съедало треть ядра. Задаётся до первого импорта numpy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from jarvis.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
