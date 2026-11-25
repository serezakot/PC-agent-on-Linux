"""Сведения о системе: место на диске, память, процессор, IP, батарея, версия системы, время."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .core import SystemCommandError, SystemPlan, _find_removable_media, _register

# --- Сведения о системе: диск, память, процессор, IP, батарея, версия, время ---
# Вопросы о системе и повседневные задачи: диск, память, процессор, IP, батарея, время, таймеры, напоминания,
#       обои, Wi-Fi, вывод звука, экран, «кофеин», установка программ, Bluetooth
# =============================================================================
def _human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def _disk_free_op(home: Path) -> str:
    parts = []
    seen = set()
    for label, path in (("Система (/)", Path("/")), ("Домашняя папка", home)):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        key = (st.f_fsid if hasattr(st, "f_fsid") else 0, st.f_blocks)
        if key in seen:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        parts.append(f"{label}: свободно {_human_bytes(free)} из {_human_bytes(total)} ({free / total:.0%})")
    rem = _find_removable_media()
    if rem.is_dir() and rem != Path(f"/media/{os.environ.get('USER', '')}"):
        try:
            st = os.statvfs(rem)
            parts.append(f"{rem.name}: свободно {_human_bytes(st.f_bavail * st.f_frsize)} из {_human_bytes(st.f_blocks * st.f_frsize)}")
        except OSError:
            pass
    return "; ".join(parts) if parts else "Не удалось прочитать информацию о дисках"


@_register("sys_disk_free")
def _h_sys_disk_free(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="disk_free", description="Проверяю свободное место на диске",
                      commands=(("df", "-h", "/"),), python_op=lambda: _disk_free_op(home))


def _memory_op() -> str:
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0]) * 1024
    except (OSError, ValueError):
        raise SystemCommandError("не удалось прочитать /proc/meminfo")
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    used = total - avail
    swap_t, swap_f = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    msg = f"Оперативная память: занято {_human_bytes(used)} из {_human_bytes(total)} ({used / total:.0%}), свободно {_human_bytes(avail)}"
    if swap_t:
        msg += f"; подкачка: занято {_human_bytes(swap_t - swap_f)} из {_human_bytes(swap_t)}"
    return msg


@_register("sys_memory")
def _h_sys_memory(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="memory", description="Проверяю оперативную память", commands=(("free", "-h"),), python_op=_memory_op)


def _cpu_op() -> str:
    def _snap():
        line = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = list(map(int, line))
        return sum(vals), vals[3] + vals[4]
    try:
        t1, i1 = _snap()
        time.sleep(0.6)
        t2, i2 = _snap()
        load = 100.0 * (1 - (i2 - i1) / max(1, (t2 - t1)))
    except (OSError, ValueError, IndexError):
        load = -1
    top = ""
    try:
        res = subprocess.run(["ps", "-eo", "pcpu,comm", "--sort=-pcpu"], capture_output=True, text=True, timeout=3.0, check=False)
        rows = [r.split(None, 1) for r in res.stdout.splitlines()[1:4]]
        top = ", ".join(f"{name} ({float(pc):.0f}%)" for pc, name in rows if float(pc) > 0.5)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    temp = ""
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            if "cpu" in (zone / "type").read_text().lower() or "x86_pkg_temp" in (zone / "type").read_text().lower() or zone.name == "thermal_zone0":
                temp = f"{int((zone / 'temp').read_text().strip()) / 1000:.0f}°C"
                break
        except (OSError, ValueError):
            continue
    if not temp:
        for hw in Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                name = (hw / "name").read_text().strip().lower()
                if name in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
                    temp = f"{int((hw / 'temp1_input').read_text().strip()) / 1000:.0f}°C"
                    break
            except (OSError, ValueError):
                continue
    msg = f"Загрузка процессора: {load:.0f}%" if load >= 0 else "Загрузка процессора: не удалось измерить"
    if temp:
        msg += f", температура {temp}"
    if top:
        msg += f". Больше всего грузят: {top}"
    return msg


@_register("sys_cpu")
def _h_sys_cpu(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="cpu", description="Проверяю загрузку процессора", commands=(("top", "-bn1"),), python_op=_cpu_op)


def _ip_op() -> str:
    local = []
    try:
        res = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=3.0, check=False)
        for iface in json.loads(res.stdout or "[]"):
            if iface.get("ifname") == "lo":
                continue
            for a in iface.get("addr_info", []):
                if a.get("family") == "inet":
                    local.append(f"{iface['ifname']}: {a['local']}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    external = ""
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=2.5) as resp:
            external = resp.read().decode().strip()
    except Exception:
        external = ""
    msg = "Локальный IP: " + ("; ".join(local) if local else "нет активных подключений")
    if external:
        msg += f". Внешний IP: {external}"
    return msg


@_register("sys_ip")
def _h_sys_ip(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="ip", description="Узнаю IP-адрес", commands=(("ip", "-4", "addr"),), python_op=_ip_op)


def _battery_op() -> str:
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            cap = (bat / "capacity").read_text().strip()
            status = (bat / "status").read_text().strip().lower()
        except OSError:
            continue
        st = {"charging": "заряжается", "discharging": "работает от батареи", "full": "полностью заряжен", "not charging": "не заряжается"}.get(status, status)
        msg = f"Батарея: {cap}%, {st}"
        try:
            res = subprocess.run(["upower", "-i", f"/org/freedesktop/UPower/devices/battery_{bat.name}"], capture_output=True, text=True, timeout=3.0, check=False)
            m = re.search(r"time to (empty|full):\s+([\d.,]+)\s*(\w+)", res.stdout or "")
            if m:
                num = float(m.group(2).replace(",", "."))
                unit = m.group(3).lower()
                mins = num * 60 if unit.startswith("hour") else num * 24 * 60 if unit.startswith("day") else num
                h, mm = divmod(int(round(mins)), 60)
                span = (f"{h} ч {mm} мин" if h else f"{mm} мин") if mins >= 1 else "меньше минуты"
                msg += f", до полной зарядки примерно {span}" if m.group(1) == "full" else f", хватит примерно на {span}"
        except (OSError, subprocess.TimeoutExpired):
            pass
        return msg
    return "Батарея не найдена: похоже, это настольный компьютер"


@_register("sys_battery")
def _h_sys_battery(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="battery", description="Проверяю заряд батареи", commands=(("upower", "-e"),), python_op=_battery_op)


def _sysinfo_op() -> str:
    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3.0, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    os_name = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    kernel = _run(["uname", "-r"])
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    cores = os.cpu_count() or 0
    mem = ""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem = _human_bytes(int(line.split()[1]) * 1024)
    except (OSError, ValueError):
        pass
    gpu = ", ".join(sorted({m.split(":", 2)[-1].strip() for m in _run(["lspci"]).splitlines() if "VGA" in m or "3D" in m or "Display" in m}))
    gnome = _run(["gnome-shell", "--version"])
    host = _run(["hostname"])
    session = os.environ.get("XDG_SESSION_TYPE", "")
    return (f"{os_name}, ядро {kernel}, {gnome} ({session}). Компьютер: {host}. Процессор: {cpu} ({cores} потоков), "
            f"память: {mem}" + (f", видео: {gpu}" if gpu else ""))


@_register("sys_info")
def _h_sys_info(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="info", action="sysinfo", description="Собираю информацию о системе", commands=(("uname", "-a"),), python_op=_sysinfo_op)


_RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


@_register("sys_time")
def _h_sys_time(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    def _op() -> str:
        now = dt.datetime.now()
        return f"Сейчас {now:%H:%M}, {_RU_DAYS[now.weekday()]}, {now.day} {_RU_MONTHS[now.month - 1]} {now.year}"
    return SystemPlan(category="info", action="time", description="Смотрю время и дату", commands=(("date",),), python_op=_op)
