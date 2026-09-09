"""Разбор procfs и подсчёт скоростей.

Ошибки в этом коде тихие: график рисуется, значения правдоподобны, а числа
неверны — и заметить это можно, только сверив панель с `top` на сервере.
Поэтому каждый формат ядра проверяется на образце, а скорости — на паре
снимков с заранее известной разницей.
"""

from __future__ import annotations

import types
import uuid
from pathlib import Path

import pytest

from app.metrics import history, host
from app.models import Role, User

# ─── Разбор ──────────────────────────────────────────────────────────────────
STAT = """cpu  1000 0 500 8000 100 0 50 0 0 0
cpu0 500 0 250 4000 50 0 25 0 0 0
cpu1 500 0 250 4000 50 0 25 0 0 0
intr 1234
ctxt 987654
processes 4321
procs_running 3
procs_blocked 0
"""


def test_stat_gives_totals_and_every_core() -> None:
    parsed = host.parse_stat(STAT)
    assert set(parsed) == {"cpu", "cpu0", "cpu1"}
    assert parsed["cpu"].total == 9650
    # iowait — это простой: ядро ждёт диск, а не считает.
    assert parsed["cpu"].busy == 9650 - 8000 - 100


def test_old_kernels_without_steal_are_still_read() -> None:
    """На ядрах до 2.6.11 в строке было меньше полей. Строку не теряем."""
    parsed = host.parse_stat("cpu  10 0 5 80 1 0 2\n")
    assert parsed["cpu"].steal == 0
    assert parsed["cpu"].total == 98


def test_process_counters_come_from_the_same_file() -> None:
    counters = host.parse_stat_counters(STAT)
    assert counters["processes"] == 4321
    assert counters["procs_running"] == 3


def test_meminfo_is_converted_to_bytes() -> None:
    parsed = host.parse_meminfo("MemTotal:  2048 kB\nHugePagesize: 2\n")
    assert parsed["MemTotal"] == 2048 * 1024
    # Строки без «kB» — это счётчики, а не размеры: их умножать нельзя.
    assert parsed["HugePagesize"] == 2


def test_interface_is_found_even_without_a_space_before_the_number() -> None:
    """У большого счётчика пробела после двоеточия может не быть вовсе.

    Разбор по пробелам на такой строке молча терял интерфейс целиком — и
    именно тот, через который прошло больше всего трафика.
    """
    text = (
        "Inter-|   Receive                        |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes ...\n"
        "  eth0:12345678 90 1 2 0 0 0 0 87654321 70 3 4 0 0 0 0\n"
    )
    parsed = host.parse_net_dev(text)
    assert parsed["eth0"].rx_bytes == 12345678
    assert parsed["eth0"].tx_bytes == 87654321
    assert parsed["eth0"].rx_errors == 1
    assert parsed["eth0"].tx_dropped == 4


def test_diskstats_reads_sectors_and_busy_time() -> None:
    line = "   8       0 sda 10 0 100 20 30 0 200 40 0 2500 60 0 0 0 0\n"
    parsed = host.parse_diskstats(line)
    assert parsed["sda"].read_sectors == 100
    assert parsed["sda"].write_sectors == 200
    assert parsed["sda"].io_ms == 2500


def _process_stat(name: str, utime: int, stime: int, rss_pages: int) -> str:
    """Строка /proc/<pid>/stat: pid, имя в скобках и дальше по номерам полей."""
    fields = ["0"] * 22
    fields[0] = "S"
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[21] = str(rss_pages)
    return f"100 ({name}) " + " ".join(fields) + "\n"


def test_process_name_may_contain_spaces_and_brackets() -> None:
    """Имя берётся между первой и ПОСЛЕДНЕЙ скобкой.

    Обычный split по пробелам на процессе вроде `(Web Content)` сдвигает все
    поля, и вместо процессорного времени в таблицу попадает что попало.
    """
    parsed = host.parse_process_stat(_process_stat("Web Content (2)", 100, 50, 3))
    assert parsed is not None
    assert parsed.name == "Web Content (2)"
    assert parsed.cpu_ticks == 150
    assert parsed.rss_bytes == 3 * host.PAGE_SIZE


def test_mounts_keep_only_real_filesystems() -> None:
    text = (
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/sdb1 /mnt/big\\040disk xfs rw 0 0\n"
    )
    mounts = host.parse_mounts(text)
    assert [row[1] for row in mounts] == ["/", "/mnt/big disk"]


# ─── Скорости ────────────────────────────────────────────────────────────────
def test_counter_reset_does_not_become_a_negative_rate() -> None:
    """Интерфейс подняли заново — счётчик пошёл с нуля.

    Отрицательная скорость утащила бы вниз всю шкалу графика, и вместе с ней
    все остальные ряды.
    """
    assert host._rate(1000, 5, 5.0) == 0
    assert host._rate(1000, 1500, 5.0) == 100


def test_cpu_breakdown_is_measured_against_the_whole_interval() -> None:
    before = host.parse_stat(STAT)["cpu"]
    after = host.parse_stat("cpu  1100 0 550 8330 120 0 50 0\n")["cpu"]
    usage = host._cpu_breakdown(before, after)
    assert usage["busy"] == 30.0
    assert usage["user"] == 20.0
    assert usage["system"] == 10.0
    assert usage["iowait"] == 4.0


def test_available_memory_comes_from_the_kernel_not_from_arithmetic() -> None:
    """MemAvailable ядро оценивает точнее, чем «свободно плюс кэш»."""
    usage = host._memory_usage(
        {
            "MemTotal": 1000,
            "MemFree": 100,
            "MemAvailable": 400,
            "Buffers": 50,
            "Cached": 350,
            "Shmem": 50,
            "SReclaimable": 20,
            "SwapTotal": 200,
            "SwapFree": 150,
        }
    )
    assert usage["available"] == 400
    assert usage["used"] == 600
    # Разделяемая память учтена в Cached, но освободить её нельзя.
    assert usage["cached"] == 350 + 20 - 50
    assert usage["swap_used"] == 50


def test_a_process_that_has_just_started_gets_no_percentage() -> None:
    """Иначе любой только что запущенный процесс возглавлял бы таблицу.

    Времени с рождения у него сколько угодно, а промежутка для сравнения нет.
    """
    old = host.RawReading(at=0, cpu={}, net={}, disk={}, stat_text="", processes={})
    new = host.RawReading(
        at=5,
        cpu={},
        net={},
        disk={},
        stat_text="",
        processes={7: host.ProcCounters(name="ffmpeg", cpu_ticks=99999, rss_bytes=1)},
    )
    assert host._top_processes(old, new, 5.0) == []


# ─── Сбор целиком ────────────────────────────────────────────────────────────
class FakeClock:
    """Часы, которые идут ровно на пять секунд за замер."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        self.now += 5.0
        return self.now


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fake_proc(root: Path, *, tick: int) -> None:
    """Дерево из нескольких файлов вместо настоящего procfs.

    Второй вызов с tick=1 подкручивает все счётчики так, чтобы разница за
    пять секунд была круглой: 30 % процессора, 1 КБ/с приёма, 2 КБ/с
    передачи, 1 КБ/с чтения с диска и половина времени занятости.
    """
    step = tick
    _write(
        root / "stat",
        f"cpu  {1000 + 100 * step} 0 {500 + 50 * step} {8000 + 330 * step} "
        f"{100 + 20 * step} 0 50 0\n"
        f"cpu0 {500 + 50 * step} 0 {250 + 25 * step} {4000 + 165 * step} "
        f"{50 + 10 * step} 0 25 0\n"
        "processes 4321\nprocs_running 3\nprocs_blocked 0\n",
    )
    _write(root / "meminfo", "MemTotal: 1024 kB\nMemFree: 256 kB\nMemAvailable: 512 kB\n")
    _write(
        root / "1" / "net" / "dev",
        "Inter-|   Receive |  Transmit\n face |bytes\n"
        f"  eth0:{1000 + 5120 * step} 90 0 0 0 0 0 0 "
        f"{2000 + 10240 * step} 70 0 0 0 0 0 0\n"
        f"  lo:{999 + step} 1 0 0 0 0 0 0 {999 + step} 1 0 0 0 0 0 0\n",
    )
    _write(
        root / "diskstats",
        f"   8  0 sda 10 0 {100 + 10 * step} 20 30 0 {200 + 20 * step} 40 0 "
        f"{2500 * step} 60 0 0 0 0\n",
    )
    _write(root / "uptime", "123456.78 987654.32\n")
    _write(root / "loadavg", "0.50 0.40 0.30 3/321 4321\n")
    _write(root / "100" / "stat", _process_stat("uvicorn", 100 + 200 * step, 100 + 50 * step, 64))


@pytest.fixture
def collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> host.HostMetrics:
    monkeypatch.setattr(host, "time", FakeClock())
    proc = tmp_path / "proc"
    sysfs = tmp_path / "sys"
    (sysfs / "block" / "sda").mkdir(parents=True)
    _fake_proc(proc, tick=0)
    return host.HostMetrics(proc=proc, sys=sysfs, rootfs=tmp_path)


def test_the_first_reading_gives_nothing(collector: host.HostMetrics) -> None:
    """Счётчики ядра накопительные: по одному снимку скорость не считается."""
    assert collector.sample() is None


def test_rates_are_measured_between_two_readings(collector: host.HostMetrics) -> None:
    assert collector.sample() is None
    _fake_proc(collector.proc, tick=1)
    reading = collector.sample()

    assert reading is not None
    series = reading.series
    assert series["cpu"]["busy"] == 30.0
    assert series["cpu"]["cores"] == [30.0]
    assert series["memory"]["total"] == 1024 * 1024
    assert series["load"] == [0.5, 0.4, 0.3]

    interfaces = {row["name"]: row for row in series["net"]}
    # lo не показываем никогда: это трафик машины с самой собой.
    assert set(interfaces) == {"eth0"}
    assert interfaces["eth0"]["rx"] == 1024
    assert interfaces["eth0"]["tx"] == 2048

    disks = {row["name"]: row for row in series["disk"]}
    assert disks["sda"]["read"] == 1024
    assert disks["sda"]["write"] == 2048
    assert disks["sda"]["busy"] == 50.0


def test_state_carries_what_a_graph_cannot_show(collector: host.HostMetrics) -> None:
    assert collector.sample() is None
    _fake_proc(collector.proc, tick=1)
    reading = collector.sample()

    assert reading is not None
    state = reading.state
    assert state["uptime"] == 123457
    assert state["cores"] == 1
    assert state["processes"]["total"] == 4321
    assert state["processes"]["running"] == 3

    top = state["processes"]["top"]
    assert [row["name"] for row in top] == ["uvicorn"]
    # 250 тактов за пять секунд — половина одного ядра.
    assert top[0]["cpu"] == round(250 / host.CLOCK_TICKS / 5 * 100, 1)
    assert top[0]["rss"] == 64 * host.PAGE_SIZE


def test_a_clock_jump_is_thrown_away(collector: host.HostMetrics, monkeypatch) -> None:
    """Машина спала или часы перевели — скорости за такой промежуток лгут."""
    assert collector.sample() is None
    monkeypatch.setattr(host, "time", types.SimpleNamespace(time=lambda: 1_000_000.0 + 5000))
    assert collector.sample() is None


def test_missing_procfs_is_explained_not_hidden(tmp_path: Path) -> None:
    reason = host.HostMetrics(proc=tmp_path / "nope").available()
    assert "Linux" in reason


# ─── История в Redis ─────────────────────────────────────────────────────────
async def test_history_keeps_the_newest_readings_and_returns_them_in_order() -> None:
    for at in (10.0, 20.0, 30.0):
        await history.push(
            host.Reading(series={"at": at, "cpu": {"busy": at}}, state={"at": at})
        )

    rows = await history.series(3600)
    assert [row["at"] for row in rows] == [10.0, 20.0, 30.0]

    current = await history.state()
    assert current is not None and current["at"] == 30.0


async def test_history_gives_only_what_the_page_has_not_seen() -> None:
    """Страница обновляется раз в пять секунд и не должна забирать весь час."""
    for at in (10.0, 20.0, 30.0):
        await history.push(host.Reading(series={"at": at}, state={"at": at}))

    assert [row["at"] for row in await history.series(3600, since=20.0)] == [30.0]


async def test_history_does_not_grow_past_its_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history, "capacity", lambda: 3)
    for at in range(10):
        await history.push(host.Reading(series={"at": float(at)}, state={}))

    rows = await history.series(3600)
    assert [row["at"] for row in rows] == [7.0, 8.0, 9.0]


# ─── Страница ────────────────────────────────────────────────────────────────
def test_monitoring_page_offers_every_range_and_loads_its_script() -> None:
    from app.web.monitoring_views import DEFAULT_RANGE, RANGES
    from app.web.templating import templates

    html = templates.env.get_template("monitoring.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/monitoring")),
        user=User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin),
        ranges=RANGES,
        default_range=DEFAULT_RANGE,
        interval=5,
        csrf_token="t",
    )
    for key, window in RANGES.items():
        assert f'data-mon-range="{key}"' in html
        assert window.label in html
    assert "/static/monitoring.js" in html
    assert "/static/monitoring.css" in html
    # Раздел меню отмечен как текущий — иначе непонятно, где находишься.
    assert 'href="/admin/monitoring"' in html


def test_chart_palette_is_defined_for_both_themes() -> None:
    """Ряд, забытый в одной из тем, достаётся ей от другой.

    На светлом полотне тёмная ступень выцветает до нечитаемой, и график
    выглядит как пустая карточка — но только у половины пользователей.
    """
    from app.web.templating import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for slot in range(1, 6):
        assert css.count(f"--chart-{slot}:") == 3, f"--chart-{slot} задан не во всех темах"
    assert css.count("--chart-grid:") == 3
