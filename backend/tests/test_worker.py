"""Циклы фонового процесса: расписание, живучесть и остановка.

Проверяется именно расписание, а не работа самих циклов: они ходят в БД,
к MediaMTX и запускают ffprobe, и подменяются здесь целиком. Смысл в том,
что реконсиляция обязана идти по своему интервалу независимо от пробы —
раньше они жили в одном цикле, и три неотвечающие камеры растягивали тик
до двух минут при заявленных пятнадцати секундах.
"""

from __future__ import annotations

import asyncio

import pytest

from app import worker


# ─── Живучесть одного захода ─────────────────────────────────────────────────
async def test_a_crashing_cycle_does_not_take_down_the_worker() -> None:
    async def boom() -> None:
        raise RuntimeError("ffprobe не найден в образе")

    await worker._guarded("probe", boom())


async def test_a_network_failure_is_logged_but_not_raised() -> None:
    async def unreachable() -> None:
        raise OSError("Connection refused")

    await worker._guarded("reconcile", unreachable())


async def test_cancellation_passes_through_guarded() -> None:
    """Остановка worker'а не должна выглядеть как падение цикла."""

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await worker._guarded("probe", cancelled())


# ─── Цикл пробы ──────────────────────────────────────────────────────────────
async def test_probe_loop_runs_a_cycle_and_honours_the_stop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls: list[str] = []

    async def fake_probe(limit: int = 0) -> None:
        calls.append("probe")
        stop.set()

    monkeypatch.setattr(worker, "_probe_cycle", fake_probe)
    await asyncio.wait_for(worker._probe_loop(stop), timeout=5)

    assert calls == ["probe"]


async def test_probe_loop_refreshes_previews_on_its_own_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Превью обновляются раз в SNAPSHOT_EVERY_TICKS заходов, а не каждый."""
    stop = asyncio.Event()
    ticks = 0
    snapshots: list[int] = []

    async def fake_probe(limit: int = 0) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= worker.SNAPSHOT_EVERY_TICKS:
            stop.set()

    async def fake_snapshot() -> None:
        snapshots.append(ticks)

    monkeypatch.setattr(worker, "_probe_cycle", fake_probe)
    monkeypatch.setattr(worker, "_snapshot_cycle", fake_snapshot)
    monkeypatch.setattr(worker, "PROBE_INTERVAL_SECONDS", 0)
    await asyncio.wait_for(worker._probe_loop(stop), timeout=5)

    assert snapshots == [worker.SNAPSHOT_EVERY_TICKS]


async def test_probe_loop_survives_a_broken_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неудачная проба не должна останавливать пробу остальных камер."""
    stop = asyncio.Event()
    attempts = 0

    async def fake_probe(limit: int = 0) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            stop.set()
        raise RuntimeError("камера не ответила")

    monkeypatch.setattr(worker, "_probe_cycle", fake_probe)
    monkeypatch.setattr(worker, "PROBE_INTERVAL_SECONDS", 0)
    await asyncio.wait_for(worker._probe_loop(stop), timeout=5)

    assert attempts == 3


# ─── Цикл реконсиляции ───────────────────────────────────────────────────────
async def test_reconcile_loop_never_waits_for_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ради этого разделение и сделано: ffprobe больше не в этом цикле."""
    stop = asyncio.Event()
    calls: list[str] = []

    async def fake_reconcile() -> None:
        calls.append("reconcile")
        stop.set()

    async def must_not_run(limit: int = 0) -> None:
        calls.append("probe")

    monkeypatch.setattr(worker, "_reconcile_cycle", fake_reconcile)
    monkeypatch.setattr(worker, "_probe_cycle", must_not_run)
    monkeypatch.setattr(worker, "_snapshot_cycle", must_not_run)
    await asyncio.wait_for(worker._reconcile_loop(stop, interval=0), timeout=5)

    assert calls == ["reconcile"]


async def test_cleanup_runs_once_in_twenty_reconciliations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Уборка перебирает несколько таблиц целиком — не на каждом тике."""
    stop = asyncio.Event()
    ticks = 0
    cleanups: list[int] = []

    async def fake_reconcile() -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 20:
            stop.set()

    async def fake_cleanup() -> None:
        cleanups.append(ticks)

    monkeypatch.setattr(worker, "_reconcile_cycle", fake_reconcile)
    monkeypatch.setattr(worker, "_cleanup_cycle", fake_cleanup)
    await asyncio.wait_for(worker._reconcile_loop(stop, interval=0), timeout=5)

    assert cleanups == [20]
