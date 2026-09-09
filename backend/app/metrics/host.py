"""Нагрузка сервера: процессор, память, диски, сеть, температура.

Читаем procfs и sysfs напрямую — ни psutil, ни node_exporter в зависимости не
берём. Причина простая: всё, что нужно панели, лежит в полудюжине текстовых
файлов, а лишняя зависимость в образе — это ещё один источник обновлений
безопасности ради двух десятков строк разбора.

Счётчики ядра — накопительные: `/proc/stat` отдаёт время с загрузки, а
`/proc/net/dev` — байты с момента появления интерфейса. Мгновенных значений в
них нет вовсе, поэтому измерение всегда делается по разнице двух снимков, и
первый снимок после старта не даёт ничего, кроме точки отсчёта.

Контейнер и хост. Приложение работает в контейнере, а показывать нужно
нагрузку машины целиком, поэтому пути к procfs и sysfs задаются настройками
(HOST_PROC, HOST_SYS, HOST_ROOTFS) и в compose туда монтируется хозяйское
дерево. Часть файлов хозяйская и без монтирования: `/proc/stat`,
`/proc/meminfo` и `/proc/diskstats` не разделяются по контейнерам, и внутри
видны значения всей машины. А вот `/proc/net/dev` разделяется — он всегда
принадлежит сетевому пространству имён того, кто читает, — поэтому счётчики
интерфейсов берутся из `<HOST_PROC>/1/net/dev`: процесс с номером 1 живёт в
корневом пространстве имён хоста. Без монтирования там окажутся интерфейсы
самого контейнера; страница об этом честно предупреждает.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger("metrics")

#: Диски в procfs всегда считаются в секторах по 512 байт — независимо от
#: того, какой размер сектора у самого устройства.
SECTOR_BYTES = 512

#: Такты планировщика в секунде и размер страницы памяти. sysconf есть только
#: на POSIX; на Windows разбор всё равно работает по тестовым данным, поэтому
#: вместо падения берём общепринятые значения.
CLOCK_TICKS = int(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100
PAGE_SIZE = int(os.sysconf("SC_PAGE_SIZE")) if hasattr(os, "sysconf") else 4096

#: Интерфейсы, которые не показываем никогда.
#:
#: `lo` — это трафик машины с самой собой, и на нём одном не видно ничего,
#: кроме собственных healthcheck'ов. `veth*` — половинки виртуальных пар,
#: которые docker создаёт и удаляет вместе с контейнерами: имена у них
#: случайные и живут ровно до перезапуска, поэтому график по ним — это
#: череда линий, обрывающихся в никуда. Мосты (`docker0`, `br-*`) оставляем:
#: через них идёт весь трафик контейнеров, и это настоящая нагрузка.
IGNORED_INTERFACES = re.compile(r"^(lo|veth|tun|tap|dummy|sit|ip6tnl|gre)")

#: Мосты docker'а. Их видно на графиках, но в общий итог они не попадают:
#: трафик контейнера проходит и через мост, и через настоящую сетевую карту,
#: поэтому сумма по всем интерфейсам показывала бы двойной объём.
BRIDGE_INTERFACES = re.compile(r"^(docker|br-|virbr|lxcbr|cni)")

#: Устройства, которых в списке дисков быть не должно: образы (loop), диски в
#: памяти (ram, zram), приводы (sr) и разделы устройств mapper'а.
IGNORED_DISKS = re.compile(r"^(loop|ram|zram|sr|fd|md|dm-)")

#: Файловые системы, занятость которых имеет смысл показывать. Всё остальное
#: (tmpfs, overlay, procfs, cgroup и ещё десятка три) — либо память, либо
#: служебные деревья ядра: «свободно 0 из 0» в списке дисков только мешает.
REAL_FILESYSTEMS = frozenset(
    {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "vfat", "ntfs"}
)

#: Сколько процессов показываем в таблице «кто ест ресурсы».
TOP_PROCESSES = 8

#: Занятость раздела спрашивают у самой файловой системы, и на Windows такого
#: вызова нет вовсе. Берём его через getattr, чтобы модуль оставался
#: импортируемым там, где идёт разработка: разбор procfs проверяется тестами
#: на любой системе, а размеры разделов — только на Linux.
_statvfs = getattr(os, "statvfs", None)


class MetricsUnavailable(RuntimeError):
    """На этой машине метрик хоста нет — например, это не Linux."""


# ─── Разбор файлов ядра ──────────────────────────────────────────────────────
# Каждая функция принимает текст, а не путь: так их можно проверить тестами на
# любой системе, включая ту, где разработчик пишет код.
@dataclass(frozen=True, slots=True)
class CpuTimes:
    """Время процессора в тактах, накопленное с загрузки машины."""

    user: int
    nice: int
    system: int
    idle: int
    iowait: int
    irq: int
    softirq: int
    steal: int

    @property
    def total(self) -> int:
        return (
            self.user + self.nice + self.system + self.idle
            + self.iowait + self.irq + self.softirq + self.steal
        )

    @property
    def busy(self) -> int:
        """Всё, кроме простоя. iowait — тоже простой: ядро ждёт диск."""
        return self.total - self.idle - self.iowait


def parse_stat(text: str) -> dict[str, CpuTimes]:
    """`/proc/stat` → времена по процессору целиком (`cpu`) и по ядрам.

    Строки короче восьми чисел встречаются на старых ядрах, где ещё не было
    `steal`, — недостающие поля дополняем нулями, а не отбрасываем строку.
    """
    result: dict[str, CpuTimes] = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        name = parts[0]
        try:
            values = [int(v) for v in parts[1:9]]
        except ValueError:
            continue
        values += [0] * (8 - len(values))
        result[name] = CpuTimes(*values)
    return result


def parse_stat_counters(text: str) -> dict[str, int]:
    """Счётчики процессов из того же `/proc/stat`: сколько всего и сколько бежит."""
    counters: dict[str, int] = {}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name in ("processes", "procs_running", "procs_blocked", "ctxt"):
            try:
                counters[name] = int(value.strip().split()[0])
            except (ValueError, IndexError):
                continue
    return counters


def parse_meminfo(text: str) -> dict[str, int]:
    """`/proc/meminfo` → байты. В файле килобайты, и это единственная причина ×1024."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        result[name] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return result


@dataclass(frozen=True, slots=True)
class NetCounters:
    rx_bytes: int
    rx_packets: int
    rx_errors: int
    rx_dropped: int
    tx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_dropped: int


def parse_net_dev(text: str) -> dict[str, NetCounters]:
    """`/proc/net/dev` → счётчики по интерфейсам.

    Формат нечитаемый по-человечески: два заголовка, имя интерфейса с
    двоеточием и шестнадцать чисел, из которых первые восемь — приём, вторые
    восемь — передача. Имя отделяем по двоеточию, а не по пробелу: у
    интерфейса с большим счётчиком пробела перед числом может не быть вовсе
    (`eth0:12345678`), и разбор по пробелам на такой строке молча теряет
    интерфейс.
    """
    result: dict[str, NetCounters] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        if not separator:
            continue
        name = name.strip()
        parts = rest.split()
        if len(parts) < 16:
            continue
        try:
            numbers = [int(v) for v in parts[:16]]
        except ValueError:
            continue
        result[name] = NetCounters(
            rx_bytes=numbers[0], rx_packets=numbers[1],
            rx_errors=numbers[2], rx_dropped=numbers[3],
            tx_bytes=numbers[8], tx_packets=numbers[9],
            tx_errors=numbers[10], tx_dropped=numbers[11],
        )
    return result


@dataclass(frozen=True, slots=True)
class DiskCounters:
    read_sectors: int
    write_sectors: int
    #: Миллисекунды, в течение которых устройство было занято хоть чем-то.
    #: Из них считается «загруженность диска» — тот же показатель, что %util
    #: в iostat.
    io_ms: int


def parse_diskstats(text: str) -> dict[str, DiskCounters]:
    """`/proc/diskstats` → счётчики по устройствам."""
    result: dict[str, DiskCounters] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        try:
            result[name] = DiskCounters(
                read_sectors=int(parts[5]),
                write_sectors=int(parts[9]),
                io_ms=int(parts[12]),
            )
        except ValueError:
            continue
    return result


def parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = text.split()
    if len(parts) < 3:
        return (0.0, 0.0, 0.0)
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return (0.0, 0.0, 0.0)


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """`/proc/mounts` → (устройство, точка монтирования, тип) для настоящих ФС.

    Один и тот же раздел может быть примонтирован дважды (bind-монтирования
    docker'а этим и занимаются), поэтому устройство запоминается: показывать
    один диск под несколькими именами незачем.
    """
    mounts: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype not in REAL_FILESYSTEMS:
            continue
        # Пробелы в путях procfs экранирует восьмеричной последовательностью.
        mountpoint = mountpoint.replace("\\040", " ")
        mounts.append((device, mountpoint, fstype))
    return mounts


@dataclass(frozen=True, slots=True)
class ProcCounters:
    name: str
    cpu_ticks: int
    rss_bytes: int


def parse_process_stat(text: str) -> ProcCounters | None:
    """Строка `/proc/<pid>/stat` → имя, потраченное время и занятая память.

    Имя процесса берётся из второго поля, а не из cmdline, и это осознанно:
    в командной строке чужих процессов хоста могут лежать пароли и токены, а
    страницу мониторинга видит администратор панели — не обязательно тот же
    человек, что администратор сервера. Имя исполняемого файла для ответа на
    вопрос «кто ест процессор» достаточно.

    Разбор идёт от последней скобки: имя в скобках может содержать и пробелы,
    и сами скобки, поэтому обычный split по пробелам смещает все поля.
    """
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end < start:
        return None
    name = text[start + 1 : end]
    fields = text[end + 2 :].split()
    # Нумерация в man 5 proc начинается с pid = 1; после отрезания первых двух
    # полей поле N оказывается на позиции N - 3.
    if len(fields) < 22:
        return None
    try:
        utime, stime, rss_pages = int(fields[11]), int(fields[12]), int(fields[21])
    except ValueError:
        return None
    return ProcCounters(name=name, cpu_ticks=utime + stime, rss_bytes=rss_pages * PAGE_SIZE)


# ─── Снятие показаний ────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RawReading:
    """Сырые накопительные счётчики одного момента времени."""

    at: float
    cpu: dict[str, CpuTimes]
    net: dict[str, NetCounters]
    disk: dict[str, DiskCounters]
    processes: dict[int, ProcCounters]
    #: Текст `/proc/stat` целиком: из него же берутся счётчики процессов, и
    #: перечитывать файл второй раз незачем.
    stat_text: str


@dataclass(frozen=True, slots=True)
class Reading:
    """Готовые к показу значения.

    Разделены надвое не для красоты: `series` ложится в историю и рисуется
    графиком, `state` описывает положение дел прямо сейчас и историей быть не
    может — занятость диска и список процессов на графике не нужны, а вес
    ответа они утраивают.
    """

    series: dict[str, Any]
    state: dict[str, Any]


class HostMetrics:
    """Сборщик показаний. Хранит предыдущий снимок, чтобы считать скорости."""

    def __init__(
        self, proc: Path | None = None, sys: Path | None = None, rootfs: Path | None = None
    ) -> None:
        # Пути можно задать явно — этим пользуются тесты, которые подсовывают
        # сборщику дерево из нескольких файлов вместо настоящего procfs.
        settings = get_settings()
        self.proc = proc or Path(settings.host_proc)
        self.sys = sys or Path(settings.host_sys)
        self.rootfs = rootfs or Path(settings.host_rootfs)
        self._previous: RawReading | None = None
        #: Читаем ли мы интерфейсы хоста или только свои. Влияет на
        #: предупреждение в панели, а не на сам сбор.
        self.host_network = False

    # --- чтение файлов ---------------------------------------------------
    def _read(self, *relative: str) -> str:
        return (self.proc.joinpath(*relative)).read_text(encoding="utf-8", errors="replace")

    def _read_optional(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _net_dev_text(self) -> str | None:
        """Счётчики интерфейсов — хозяйские, если до них можно дотянуться.

        `/proc/net` — это ссылка на `/proc/self/net`, то есть на сетевое
        пространство имён того, кто читает. Внутри контейнера там всегда его
        собственные интерфейсы, сколько хозяйского procfs ни монтируй.
        Обойти это можно единственным способом: прочитать `net/dev` у
        процесса номер 1, который живёт в корневом пространстве имён.
        """
        host = self._read_optional(self.proc / "1" / "net" / "dev")
        if host is not None:
            self.host_network = self.proc != Path("/proc")
            return host
        self.host_network = False
        return self._read_optional(self.proc / "net" / "dev")

    def _mounts_text(self) -> str | None:
        for candidate in (self.proc / "1" / "mounts", self.proc / "mounts"):
            text = self._read_optional(candidate)
            if text is not None:
                return text
        return None

    def available(self) -> str:
        """Пустая строка, если метрики доступны, иначе причина отказа."""
        if not (self.proc / "stat").exists():
            return (
                f"Файла {self.proc / 'stat'} нет: сбор метрик работает только на Linux "
                f"и требует, чтобы procfs хоста был примонтирован в контейнер."
            )
        return ""

    # --- сырые счётчики --------------------------------------------------
    def read_raw(self) -> RawReading:
        try:
            stat_text = self._read("stat")
        except OSError as exc:
            raise MetricsUnavailable(str(exc)) from exc

        net_text = self._net_dev_text()
        disk_text = self._read_optional(self.proc / "diskstats")
        return RawReading(
            at=time.time(),
            cpu=parse_stat(stat_text),
            net=parse_net_dev(net_text) if net_text else {},
            disk=parse_diskstats(disk_text) if disk_text else {},
            processes=self._read_processes(),
            stat_text=stat_text,
        )

    def _read_processes(self) -> dict[int, ProcCounters]:
        """Счётчики всех процессов. Отсутствие каталога — норма, а не ошибка.

        Процесс может завершиться между чтением списка каталогов и чтением
        его stat: на машине с сотней процессов это происходит регулярно.
        """
        if TOP_PROCESSES <= 0:
            return {}
        result: dict[int, ProcCounters] = {}
        try:
            entries = os.listdir(self.proc)
        except OSError:
            return result
        for entry in entries:
            if not entry.isdigit():
                continue
            text = self._read_optional(self.proc / entry / "stat")
            if text is None:
                continue
            counters = parse_process_stat(text)
            if counters is not None:
                result[int(entry)] = counters
        return result

    # --- то, что не требует истории --------------------------------------
    def _filesystems(self) -> list[dict[str, Any]]:
        """Занятость разделов.

        Полный список получается, только если в контейнер примонтирован
        корень хоста (HOST_ROOTFS): точки монтирования из списка ядра —
        это пути хоста, и внутри контейнера их попросту нет.

        Без него остаётся запасной путь — собственный корень контейнера. Он
        показывает не пустой overlay, как можно подумать, а тот раздел хоста,
        на котором docker держит свои данные: именно туда растут тома с базой
        и снимками, и именно он кончается первым.
        """
        if _statvfs is None:
            return []
        rows = self._mounted_filesystems()
        return rows if rows else self._own_filesystem()

    def _own_filesystem(self) -> list[dict[str, Any]]:
        usage = self._usage(Path("/"))
        if usage is None:
            return []
        return [{"mount": "диск сервера", "device": "", "fstype": "", **usage}]

    def _usage(self, path: Path) -> dict[str, int] | None:
        if _statvfs is None:
            return None
        try:
            stat = _statvfs(path)
        except OSError:
            return None
        total = stat.f_blocks * stat.f_frsize
        if total <= 0:
            return None
        # Свободное для обычного пользователя (bavail), а не для root
        # (bfree): 5% ext4 бережёт под себя, и «свободно» из bfree обещает
        # место, которого сервису не достанется.
        free = stat.f_bavail * stat.f_frsize
        return {"total": total, "free": free, "used": total - free}

    def _mounted_filesystems(self) -> list[dict[str, Any]]:
        mounts_text = self._mounts_text()
        if mounts_text is None:
            return []

        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for device, mountpoint, fstype in parse_mounts(mounts_text):
            if device in seen:
                continue
            # Хозяйская точка монтирования внутри контейнера видна по
            # префиксу HOST_ROOTFS. Без монтирования такого пути нет вовсе,
            # и раздел пропускается — за него ответит запасной путь.
            path = self.rootfs / mountpoint.lstrip("/") if mountpoint != "/" else self.rootfs
            if self.rootfs == Path("/") and not path.is_mount():
                # Корень контейнера — не корень хоста. Совпадение имён здесь
                # обмануло бы: «/» внутри контейнера существует всегда, и без
                # этой проверки его размер выдавался бы за размер хозяйского
                # раздела с тем же именем.
                continue
            usage = self._usage(path)
            if usage is None:
                continue
            seen.add(device)
            result.append({"mount": mountpoint, "device": device, "fstype": fstype, **usage})
        result.sort(key=lambda item: str(item["mount"]))
        return result

    def _temperatures(self) -> list[dict[str, Any]]:
        """Датчики из sysfs. Их может не быть вовсе — на виртуальной машине их нет."""
        result: list[dict[str, Any]] = []
        zones = self.sys / "class" / "thermal"
        try:
            entries = sorted(p for p in zones.iterdir() if p.name.startswith("thermal_zone"))
        except OSError:
            return result
        for zone in entries:
            raw = self._read_optional(zone / "temp")
            if raw is None:
                continue
            try:
                celsius = int(raw.strip()) / 1000
            except ValueError:
                continue
            # Датчик, отдающий явную бессмыслицу, лучше не показывать вовсе:
            # «-273 °C» в панели выглядит как поломка панели.
            if not -50 < celsius < 200:
                continue
            label = (self._read_optional(zone / "type") or zone.name).strip()
            result.append({"label": label, "celsius": round(celsius, 1)})
        return result

    def _uptime(self) -> float:
        text = self._read_optional(self.proc / "uptime")
        if not text:
            return 0.0
        try:
            return float(text.split()[0])
        except (ValueError, IndexError):
            return 0.0

    def _load(self) -> list[float]:
        text = self._read_optional(self.proc / "loadavg")
        return list(parse_loadavg(text)) if text else [0.0, 0.0, 0.0]

    def _whole_disks(self, names: list[str]) -> list[str]:
        """Отсеивает разделы: нужны диски целиком, иначе всё считается дважды.

        Признак диска — каталог в `/sys/block`. Когда sysfs хоста не
        примонтирован, остаётся правило по имени: `sda1` — раздел `sda`,
        `nvme0n1p1` — раздел `nvme0n1`.
        """
        block = self.sys / "block"
        if block.is_dir():
            return [name for name in names if (block / name).exists()]
        partition = re.compile(r"^(?:.*\d+p\d+|[a-z]+\d+)$")
        return [name for name in names if not partition.match(name)]

    # --- собственно измерение --------------------------------------------
    def sample(self) -> Reading | None:
        """Показания за промежуток между этим вызовом и предыдущим.

        Первый вызов возвращает None: счётчики ядра накопительные, и по
        одному снимку скорость не считается никак.
        """
        current = self.read_raw()
        previous, self._previous = self._previous, current
        if previous is None:
            return None

        elapsed = current.at - previous.at
        # Часы могли перевести назад, а сам сборщик — простоять слишком долго
        # (машина спала, контейнер был приостановлен). И то и другое даёт
        # бессмысленные скорости, поэтому промежуток просто пропускаем.
        if elapsed <= 0 or elapsed > 600:
            return None

        meminfo = parse_meminfo(self._read_optional(self.proc / "meminfo") or "")
        counters = parse_stat_counters(current.stat_text)

        series: dict[str, Any] = {
            "at": round(current.at, 1),
            "cpu": _cpu_usage(previous.cpu, current.cpu),
            "memory": _memory_usage(meminfo),
            "load": self._load(),
            "net": self._network_rates(previous, current, elapsed),
            "disk": self._disk_rates(previous, current, elapsed),
        }
        state: dict[str, Any] = {
            "at": round(current.at, 1),
            "uptime": round(self._uptime()),
            "cores": max(len(current.cpu) - 1, 1),
            "host_network": self.host_network,
            "filesystems": self._filesystems(),
            "temperatures": self._temperatures(),
            "processes": {
                "total": counters.get("processes", 0),
                "running": counters.get("procs_running", 0),
                "blocked": counters.get("procs_blocked", 0),
                "top": _top_processes(previous, current, elapsed),
            },
        }
        return Reading(series=series, state=state)

    def _network_rates(
        self, previous: RawReading, current: RawReading, elapsed: float
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, now in current.net.items():
            if IGNORED_INTERFACES.match(name):
                continue
            was = previous.net.get(name)
            if was is None:
                continue
            rx = _rate(was.rx_bytes, now.rx_bytes, elapsed)
            tx = _rate(was.tx_bytes, now.tx_bytes, elapsed)
            # Интерфейс, который за всё время не передал ни байта, — это
            # поднятая, но никуда не ведущая железка. В списке она только
            # отнимает место у настоящей.
            if now.rx_bytes == 0 and now.tx_bytes == 0:
                continue
            rows.append(
                {
                    "name": name,
                    "bridge": bool(BRIDGE_INTERFACES.match(name)),
                    "rx": rx,
                    "tx": tx,
                    "rx_packets": _rate(was.rx_packets, now.rx_packets, elapsed),
                    "tx_packets": _rate(was.tx_packets, now.tx_packets, elapsed),
                    "errors": (
                        _rate(was.rx_errors, now.rx_errors, elapsed)
                        + _rate(was.tx_errors, now.tx_errors, elapsed)
                    ),
                    "dropped": (
                        _rate(was.rx_dropped, now.rx_dropped, elapsed)
                        + _rate(was.tx_dropped, now.tx_dropped, elapsed)
                    ),
                }
            )
        rows.sort(key=lambda row: str(row["name"]))
        return rows

    def _disk_rates(
        self, previous: RawReading, current: RawReading, elapsed: float
    ) -> list[dict[str, Any]]:
        names = [name for name in current.disk if not IGNORED_DISKS.match(name)]
        rows: list[dict[str, Any]] = []
        for name in self._whole_disks(names):
            was, now = previous.disk.get(name), current.disk[name]
            if was is None:
                continue
            busy_ms = max(now.io_ms - was.io_ms, 0)
            rows.append(
                {
                    "name": name,
                    "read": _rate(was.read_sectors, now.read_sectors, elapsed) * SECTOR_BYTES,
                    "write": _rate(was.write_sectors, now.write_sectors, elapsed) * SECTOR_BYTES,
                    # Доля времени, когда у диска была хоть одна операция
                    # в работе. Больше 100 не бывает по определению, но
                    # округление и дрожание часов туда дотягивают.
                    "busy": round(min(busy_ms / (elapsed * 1000) * 100, 100), 1),
                }
            )
        rows.sort(key=lambda row: str(row["name"]))
        return rows


# ─── Вычисления ──────────────────────────────────────────────────────────────
def _rate(before: int, after: int, elapsed: float) -> int:
    """Скорость по двум показаниям накопительного счётчика.

    Счётчик может обнулиться — интерфейс подняли заново, диск переподключили,
    32-битное поле переполнилось. Отрицательная разница означает ровно это,
    и превращать её в отрицательную скорость нельзя: график уйдёт в минус
    и утащит за собой всю шкалу.
    """
    delta = after - before
    if delta < 0:
        return 0
    return round(delta / elapsed)


def _cpu_usage(before: dict[str, CpuTimes], after: dict[str, CpuTimes]) -> dict[str, Any]:
    """Загрузка процессора в процентах: целиком и по ядрам."""
    total = _cpu_breakdown(before.get("cpu"), after.get("cpu"))
    cores: list[float] = []
    index = 0
    while (name := f"cpu{index}") in after:
        cores.append(_cpu_breakdown(before.get(name), after.get(name))["busy"])
        index += 1
    total["cores"] = cores
    return total


def _cpu_breakdown(before: CpuTimes | None, after: CpuTimes | None) -> dict[str, Any]:
    if before is None or after is None:
        return {"busy": 0.0, "user": 0.0, "system": 0.0, "iowait": 0.0, "steal": 0.0}
    span = after.total - before.total
    if span <= 0:
        return {"busy": 0.0, "user": 0.0, "system": 0.0, "iowait": 0.0, "steal": 0.0}

    def share(value: int) -> float:
        return round(max(value, 0) / span * 100, 1)

    return {
        "busy": share(after.busy - before.busy),
        # nice — то же пользовательское время, просто с пониженным
        # приоритетом; отдельной полосой на графике оно только дробит картину.
        "user": share((after.user - before.user) + (after.nice - before.nice)),
        "system": share(
            (after.system - before.system)
            + (after.irq - before.irq)
            + (after.softirq - before.softirq)
        ),
        "iowait": share(after.iowait - before.iowait),
        # steal больше нуля означает, что процессорное время отняли у нашей
        # виртуальной машины в пользу соседа по гипервизору. Причина «всё
        # тормозит, а нагрузки нет» чаще всего именно здесь.
        "steal": share(after.steal - before.steal),
    }


def _memory_usage(meminfo: dict[str, int]) -> dict[str, Any]:
    total = meminfo.get("MemTotal", 0)
    free = meminfo.get("MemFree", 0)
    buffers = meminfo.get("Buffers", 0)
    # Кэш страниц минус разделяемая память: Shmem учтён в Cached, но памятью
    # под кэш не является — освободить его нельзя.
    cached = meminfo.get("Cached", 0) + meminfo.get("SReclaimable", 0) - meminfo.get("Shmem", 0)
    # MemAvailable ядро оценивает само с версии 3.14 и делает это точнее
    # любой арифметики снаружи: часть кэша освободить нельзя.
    available = meminfo.get("MemAvailable", free + buffers + max(cached, 0))
    swap_total = meminfo.get("SwapTotal", 0)
    return {
        "total": total,
        "available": available,
        "used": max(total - available, 0),
        "cached": max(cached, 0),
        "buffers": buffers,
        "swap_total": swap_total,
        "swap_used": max(swap_total - meminfo.get("SwapFree", 0), 0),
    }


def _top_processes(
    previous: RawReading, current: RawReading, elapsed: float
) -> list[dict[str, Any]]:
    """Процессы, которые больше всех ели процессор за прошедший промежуток.

    Проценты — как в top: сто процентов равны одному полностью занятому ядру,
    поэтому на восьмиядерной машине сумма может доходить до восьмисот.
    """
    rows: list[dict[str, Any]] = []
    for pid, now in current.processes.items():
        was = previous.processes.get(pid)
        # Процесс родился только что: считать ему долю не от чего, а
        # приписывать всё время с рождения — значит выдать «300 %» любому,
        # кто просто запустился.
        if was is None or was.name != now.name:
            continue
        ticks = max(now.cpu_ticks - was.cpu_ticks, 0)
        rows.append(
            {
                "pid": pid,
                "name": now.name,
                "cpu": round(ticks / CLOCK_TICKS / elapsed * 100, 1),
                "rss": now.rss_bytes,
            }
        )
    rows.sort(key=lambda row: (-float(row["cpu"]), -int(row["rss"])))
    return rows[:TOP_PROCESSES]
