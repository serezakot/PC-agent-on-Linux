"""Второй слой: безопасные ЧИТАЮЩИЕ команды Linux — ответы на вопросы о компьютере, для которых нет готового действия
(сколько работает без перезагрузки, сколько весит папка, какие Wi-Fi сети рядом, кто занял микрофон, версия Python...).

Безопасность — белый список программ, у каждой своя проверка аргументов: «nmcli device wifi list» можно, а
«nmcli device wifi connect» нельзя; «systemctl status» можно, «systemctl stop» нельзя. Оболочки нет (subprocess со
списком аргументов): конвейеры, перенаправления, подстановки и sudo невозможны. Не показываем то, что может содержать
секреты: аргументы командной строки процессов, пароли Wi-Fi, переменные окружения, журнал systemd, ключи и профили браузеров.
Вывод ограничен по длине и времени.

    run_query("uptime -p")                      -> "$ uptime -p\\nup 3 hours, 12 minutes"
    run_query("nmcli device wifi connect X")    -> QueryRefused
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

MAX_OUTPUT = 4000
TIMEOUT = 10.0


class QueryRefused(ValueError):
    """Команда не из белого списка или с недопустимыми аргументами."""


_BAD_CHARS = re.compile(r"[;&|`$<>\n\\*?]")
# пути, которые не показываем даже списком: ключи, пароли, профили браузеров, настройки ассистента с ключами API
SENSITIVE = (".ssh", ".gnupg", "keyrings", ".config/pc_agent", ".password-store", ".mozilla", "google-chrome", "chromium",
              ".pki", ".aws", ".kube", ".docker", ".netrc", ".git-credentials", "vertex-sa", "/etc/shadow",
              "/etc/sudoers", "/root", "/proc/", "/sys/firmware", ".local/share/keyrings")


def _home() -> Path:
    return Path.home()


def _path_ok(arg: str, inside_home: bool = False) -> bool:
    p = Path(arg).expanduser()
    try:
        real = str(p.resolve())
    except (OSError, RuntimeError):
        return False
    low = (str(p) + " " + real).lower()
    if any(m in low for m in SENSITIVE):
        return False
    if inside_home:
        roots = (str(_home().resolve()), "/media", "/run/media", "/mnt")
        return any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots)
    return True


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _flags_only(allowed: set[str], value_flags: set[str] = frozenset(), allow_positional: bool = False,
                paths: str = "") -> Callable[[list[str]], str | None]:
    """Все флаги из allowed (или «--флаг=значение» для value_flags); позиционные — только если разрешены.
    paths: "" — позиционные не пути, "any" — путь где угодно (кроме секретных), "home" — только дом и носители."""
    def check(args: list[str]) -> str | None:
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("-"):
                name = a.split("=", 1)[0]
                if name in value_flags:
                    if "=" not in a:
                        i += 1                         # значение следующим словом
                elif a not in allowed and not (re.fullmatch(r"-[A-Za-z]+", a) and all(f"-{c}" in allowed for c in a[1:])):
                    return f"флаг {a} не разрешён"
            else:
                if not allow_positional and not paths:
                    return f"лишний аргумент {a}"
                if paths and ("/" in a or a.startswith("~") or a == ".") and not _path_ok(a, inside_home=(paths == "home")):
                    return f"путь {a} недоступен"
            i += 1
        return None
    return check


def _verbs(first: set[str], extra: Callable[[list[str]], str | None] | None = None, max_args: int = 8,
           allow_empty: bool = True) -> Callable[[list[str]], str | None]:
    """Первый неопциональный аргумент — из списка разрешённых «читающих» подкоманд."""
    def check(args: list[str]) -> str | None:
        pos = _positional(args)
        if not pos:
            return None if allow_empty else "нужна подкоманда"
        if pos[0] not in first:
            return f"подкоманда {pos[0]} не разрешена (можно: {', '.join(sorted(first))})"
        if len(args) > max_args:
            return "слишком много аргументов"
        return extra(args) if extra else None
    return check


def _no_words(*bad: str) -> Callable[[list[str]], str | None]:
    def check(args: list[str]) -> str | None:
        hit = [a for a in args if a.lower() in bad or a.split("=", 1)[0].lower() in bad]
        return f"{hit[0]} не разрешено" if hit else None
    return check


def _all(*checks: Callable[[list[str]], str | None]) -> Callable[[list[str]], str | None]:
    def check(args: list[str]) -> str | None:
        for c in checks:
            err = c(args)
            if err:
                return err
        return None
    return check


_PS_FIELDS = {"pid", "ppid", "comm", "%cpu", "pcpu", "%mem", "pmem", "rss", "vsz", "etime", "etimes", "user", "stat",
              "nlwp", "start", "time", "ni", "pri", "psr", "lstart", "tty"}


def _ps(args: list[str]) -> str | None:
    """ps только с явным списком полей без командной строки (в ней бывают пароли и токены)."""
    i, has_fmt = 0, False
    while i < len(args):
        a = args[i]
        if a in ("-o", "-eo", "-Ao", "--format", "-e", "-A", "--no-headers", "-p", "-u", "-C", "--pid", "-U"):
            if a in ("-o", "-eo", "-Ao", "--format"):
                if i + 1 >= len(args):
                    return "нет списка полей"
                fields = {f.split("=", 1)[0].strip().lower() for f in args[i + 1].split(",") if f.strip()}
                if not fields <= _PS_FIELDS:
                    return f"поля {', '.join(sorted(fields - _PS_FIELDS))} не разрешены (командная строка процессов закрыта)"
                has_fmt = True
                i += 1
            elif a in ("-p", "-u", "-C", "--pid", "-U"):
                i += 1
        elif a.startswith("--sort="):
            pass
        else:
            return f"аргумент {a} не разрешён (используйте ps -eo pid,comm,%cpu,%mem --sort=-%cpu)"
        i += 1
    return None if has_fmt else "нужен явный список полей: ps -eo pid,comm,%cpu,%mem --sort=-%cpu"


def _nmcli(args: list[str]) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-f", "--fields", "-g", "--get-values", "-c", "--colors", "-m", "--mode"):
            i += 2
        elif args[i] in ("-t", "--terse", "-p", "--pretty", "-e", "--escape"):
            i += 1
        else:
            return f"флаг {args[i]} не разрешён"
    rest = args[i:]
    if not rest:
        return None
    obj, verbs = rest[0], rest[1:]
    if any(v in ("--show-secrets", "-s", "show-password") for v in rest):
        return "пароли сетей не показываю"
    if obj in ("general", "g", "gen"):
        return None if verbs in ([], ["status"], ["permissions"], ["hostname"]) else "nmcli general: только status/permissions"
    if obj in ("radio", "r"):
        return None if len(verbs) <= 1 else "nmcli radio: только посмотреть, без on/off"
    if obj in ("networking", "n"):
        return None if verbs in ([], ["connectivity"]) else "nmcli networking: только посмотреть"
    if obj in ("device", "dev", "d"):
        if not verbs or verbs[0] in ("status", "show"):
            return None
        if verbs[0] == "wifi" and (len(verbs) == 1 or verbs[1] in ("list", "rescan")):
            return None
        return "nmcli device: только status, show, wifi list"
    if obj in ("connection", "con", "c"):
        return None if (not verbs or verbs[0] == "show") else "nmcli connection: только show"
    return f"nmcli {obj}: не разрешено"


def _ip(args: list[str]) -> str | None:
    pos = [a for a in args if not a.startswith("-")]
    if pos and pos[0] not in ("addr", "address", "a", "link", "l", "route", "r", "neigh", "n", "rule", "maddr"):
        return f"ip {pos[0]}: не разрешено"
    if any(a in ("add", "del", "delete", "set", "flush", "change", "replace", "append", "up", "down", "save", "restore")
           for a in pos[1:]):
        return "ip: только просмотр"
    return None


def _ping(args: list[str]) -> str | None:
    if "-c" not in args:
        return "нужно -c N (не больше 4 пакетов)"
    i = args.index("-c")
    if i + 1 >= len(args) or not args[i + 1].isdigit() or not 1 <= int(args[i + 1]) <= 4:
        return "-c от 1 до 4"
    other = [a for j, a in enumerate(args) if j not in (i, i + 1)]
    for a in other:
        if a.startswith("-") and a not in ("-4", "-6", "-q", "-n"):
            return f"флаг {a} не разрешён"
    return None if len(_positional(other)) == 1 else "нужен один адрес"


def _version(args: list[str]) -> str | None:
    return None if args in (["--version"], ["-V"], ["-version"], ["version"]) else "только --version"


def _top(args: list[str]) -> str | None:
    """Только один снимок: top -b -n 1 [-o %CPU] [-w 160]; без -c (командные строки процессов)."""
    if "-c" in args or "-b" not in args or "-n" not in args:
        return "только top -b -n 1 (без -c)"
    i = args.index("-n")
    if args[i + 1:i + 2] != ["1"]:
        return "только -n 1"
    return _flags_only({"-b"}, {"-n", "-o", "-w"})(args)


RULES: dict[str, Callable[[list[str]], str | None]] = {
    "uptime": _flags_only({"-p", "-s", "--pretty", "--since"}),
    "df": _flags_only({"-h", "-H", "-T", "-l", "-i", "-a", "--total", "--local"}, {"--output", "-x", "-t", "--type"},
                      paths="any"),
    "du": _flags_only({"-s", "-h", "-c", "-a", "-S", "--apparent-size", "--si", "-x"}, {"-d", "--max-depth", "--exclude"},
                      paths="home"),
    "free": _flags_only({"-h", "-m", "-g", "-k", "-b", "--si", "-t", "-w", "--total", "--human"}),
    "ps": _ps,
    "top": _top,
    "vmstat": _flags_only({"-s", "-w", "-S", "-a", "-d"}),
    "lsblk": _flags_only({"-f", "-p", "-d", "-b", "-J", "-l", "-n", "-S", "-a", "-m", "-t", "--fs"}, {"-o", "--output"},
                         allow_positional=True),
    "lscpu": _flags_only({"-e", "-p", "-J", "-x", "-y", "-B", "-C", "--extended"}),
    "lspci": _flags_only({"-k", "-v", "-nn", "-n", "-t", "-mm", "-D"}),
    "lsusb": _flags_only({"-t", "-v"}),
    "lsmod": _flags_only(set()),
    "uname": _flags_only({"-a", "-r", "-s", "-n", "-v", "-m", "-o", "-p", "-i"}),
    "nproc": _flags_only({"--all"}),
    "arch": _flags_only(set()),
    "whoami": _flags_only(set()),
    "id": _flags_only({"-u", "-g", "-G", "-n"}),
    "groups": _flags_only(set()),
    "hostname": _flags_only({"-I", "-f", "-s", "-d", "-A", "-i"}),
    "date": _flags_only({"-u", "-R", "-I", "--iso-8601", "--rfc-3339"}, allow_positional=True),
    "cal": _flags_only({"-3", "-y", "-m"}, allow_positional=True),
    "locale": _flags_only({"-a"}),
    "who": _flags_only({"-a", "-b", "-H", "-q", "-r", "-u"}),
    "w": _flags_only({"-h", "-s", "-f"}),
    "users": _flags_only(set()),
    "last": _flags_only({"-x", "-w", "-F"}, {"-n"}, allow_positional=True),
    "hostnamectl": _verbs({"status"}, max_args=2),
    "timedatectl": _verbs({"status", "show", "timesync-status", "list-timezones", "show-timesync"}, max_args=3),
    "localectl": _verbs({"status", "list-keymaps", "list-x11-keymap-layouts", "list-locales"}, max_args=3),
    "loginctl": _verbs({"list-sessions", "list-users", "session-status", "show-session", "user-status", "show-user",
                        "list-seats", "seat-status"}),
    "ip": _ip,
    "nmcli": _nmcli,
    "systemctl": _all(_verbs({"status", "list-units", "list-unit-files", "list-timers", "is-active", "is-enabled",
                              "is-failed", "show", "list-sockets", "list-dependencies"}, max_args=10),
                      _no_words("start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "kill",
                                "isolate", "poweroff", "reboot", "suspend", "hibernate", "set-property", "edit", "daemon-reload")),
    "systemd-analyze": _verbs({"time", "blame", "critical-chain"}, max_args=4),
    "sensors": _flags_only({"-j", "-A", "-u", "-f"}),
    "upower": _flags_only({"-e", "--enumerate", "-d", "--dump", "-v", "--version"}, {"-i", "--show-info"}),
    "pactl": _all(_verbs({"info", "stat", "list", "get-default-sink", "get-default-source", "get-sink-volume",
                          "get-source-volume", "get-sink-mute", "get-source-mute"}, allow_empty=False),
                  _no_words("set-default-sink", "load-module", "unload-module", "exit", "send-message")),
    "wpctl": _verbs({"status", "inspect", "get-volume"}, allow_empty=False),
    "snap": _verbs({"list", "info", "version", "connections", "services", "changes"}, allow_empty=False),
    "flatpak": _verbs({"list", "info", "remotes", "--version", "history"}, allow_empty=False),
    "dpkg-query": _flags_only({"-W", "--show", "-l", "--list", "-s", "--status", "-L", "--listfiles"},
                              {"-f", "--showformat"}, allow_positional=True),
    "dpkg": _flags_only({"-l", "--list", "-s", "--status", "-L", "--listfiles", "-S", "--search", "--print-architecture"},
                        allow_positional=True),
    "apt": _all(_verbs({"list", "show", "policy", "search", "depends", "rdepends"}, allow_empty=False),
                _no_words("install", "remove", "purge", "upgrade", "full-upgrade", "autoremove", "update", "edit-sources")),
    "apt-cache": _verbs({"search", "show", "policy", "depends", "rdepends", "madison", "pkgnames", "showpkg"}, allow_empty=False),
    "gsettings": _verbs({"get", "list-keys", "list-recursively", "range", "list-schemas", "list-children", "describe",
                         "writable"}, allow_empty=False),
    "xdg-mime": _verbs({"query"}, allow_empty=False),
    "xdg-settings": _verbs({"get", "check"}, allow_empty=False),
    "ls": _flags_only({"-l", "-a", "-A", "-h", "-t", "-S", "-r", "-1", "-d", "-F", "-R", "--full-time", "--group-directories-first"},
                      {"--sort", "--time-style"}, paths="any"),
    "stat": _flags_only({"-L", "-f", "-t"}, {"-c", "--format"}, paths="any"),
    "wc": _flags_only({"-l", "-w", "-c", "-m", "-L"}, paths="home"),
    "file": _flags_only({"-b", "-i", "-L", "--mime-type"}, paths="home"),
    "pgrep": _flags_only({"-l", "-c", "-x", "-i", "-n", "-o", "--count"}, {"-u"}, allow_positional=True),
    "pidof": _flags_only({"-s"}, allow_positional=True),
    "ping": _ping,
    "dig": _flags_only({"+short", "-4", "-6"}, allow_positional=True),
    "host": _flags_only({"-4", "-6", "-t"}, allow_positional=True),
    "nslookup": _flags_only(set(), allow_positional=True),
    "which": _flags_only({"-a"}, allow_positional=True),
    "fc-list": _flags_only(set(), allow_positional=True),
}
# версии программ: «python3 --version»
for _prog in ("python3", "python", "pip3", "pip", "node", "npm", "git", "gcc", "g++", "java", "docker", "code", "go",
              "rustc", "cargo", "gnome-shell", "nautilus", "ffmpeg", "firefox", "bash", "perl", "php", "make", "cmake",
              "nvim", "vim", "gnome-terminal", "snap", "flatpak", "ollama", "pipewire", "wireplumber"):
    if _prog in RULES:
        _old = RULES[_prog]
        RULES[_prog] = (lambda old: lambda a: None if _version(a) is None else old(a))(_old)
    else:
        RULES[_prog] = _version


def check_query(command: str) -> list[str]:
    """Разбирает и проверяет команду -> argv; QueryRefused с объяснением, если так нельзя."""
    command = (command or "").strip()
    if not command:
        raise QueryRefused("пустая команда")
    if _BAD_CHARS.search(command):
        raise QueryRefused("без | ; & > < $ ` * ? и переносов строк: только одна простая команда")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise QueryRefused(f"не разобрала команду: {exc}") from exc
    prog, args = argv[0], argv[1:]
    if prog in ("sudo", "su", "pkexec", "doas"):
        raise QueryRefused("с правами администратора ничего не запускаю")
    rule = RULES.get(Path(prog).name if "/" not in prog else "")
    if rule is None or "/" in prog:
        raise QueryRefused(f"программа {prog} не из списка читающих команд")
    err = rule(args)
    if err:
        raise QueryRefused(f"{prog}: {err}")
    return argv


def run_query(command: str) -> str:
    """Выполняет проверенную читающую команду и возвращает её вывод (обрезанный) для LLM."""
    argv = check_query(command)
    argv = [argv[0]] + [os.path.expanduser(a) if a.startswith("~") else a for a in argv[1:]]   # оболочки нет: «~» сами
    exe = shutil.which(argv[0])
    if exe is None:
        return f"$ {command}\nпрограмма {argv[0]} не установлена"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "LANGUAGE", "USER", "LOGNAME",
                                                         "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY",
                                                         "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "XDG_DATA_DIRS", "TERM")}
    env.setdefault("TERM", "dumb")
    if argv[0] in ("top",):
        env["COLUMNS"] = "160"
    try:
        res = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=TIMEOUT, check=False,
                             stdin=subprocess.DEVNULL, env=env, errors="replace")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return f"$ {command}\n{out[:MAX_OUTPUT]}\n(остановлено: дольше {TIMEOUT:.0f} с)"
    except OSError as exc:
        return f"$ {command}\nне запустилась: {exc}"
    out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr.strip() else "")
    out = out.strip() or "(пустой вывод)"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n… (обрезано, всего {len(out)} символов)"
    code = f"\n(код возврата {res.returncode})" if res.returncode else ""
    return f"$ {command}\n{out}{code}"


def read_text_file(path: str, readable: Callable[[Path], bool], limit: int = 6000) -> str:
    """Начало текстового файла пользователя (только то, что разрешает readable: дом/носители, без скрытых)."""
    p = Path((path or "").strip()).expanduser()
    if not (path or "").strip():
        raise QueryRefused("не указан файл")
    if not p.is_file():
        raise QueryRefused(f"нет такого файла: {p}")
    if not readable(p) or not _path_ok(str(p)):
        raise QueryRefused("этот файл читать нельзя (только обычные файлы в домашней папке и на носителях)")
    with p.open("rb") as f:
        data = f.read(min(limit * 4, 256 * 1024))
    if b"\x00" in data[:4096]:
        return f"{p}: это не текстовый файл ({p.stat().st_size} байт)"
    text = data.decode("utf-8", errors="replace")
    more = p.stat().st_size > len(data) or len(text) > limit
    return f"{p}:\n{text[:limit]}" + ("\n… (дальше не показано)" if more else "")
