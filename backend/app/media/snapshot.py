"""Снимок одного кадра — превью камеры в панели."""

from __future__ import annotations

import asyncio
import uuid

from ..config import get_settings
from ..logging_setup import get_logger
from .ssrf import strip_credentials

log = get_logger("snapshot")

SNAPSHOT_TIMEOUT = 25


def _ffmpeg_frame_args(rtsp_url: str, output: str, width: int) -> list[str]:
    """Один кадр из RTSP в JPEG. output = путь либо «-» для stdout."""
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        # Высота по пропорции и кратна двум — иначе ffmpeg ругается на нечётность.
        "-vf", f"scale={width}:-2",
        "-q:v", "6",
        "-f", "image2",
        "-y",
        output,
    ]


async def grab_frame(
    rtsp_url: str,
    width: int = 640,
    timeout: int = SNAPSHOT_TIMEOUT,  # noqa: ASYNC109
) -> tuple[bytes | None, str]:
    """Кадр прямо в память: (jpeg, сообщение об ошибке).

    Отдельно от capture_snapshot, потому что предпросмотр и диагностика
    работают до того, как камера появилась в БД, — файлу превью взяться
    неоткуда, да и записывать на диск непроверенный источник незачем.
    """
    args = _ffmpeg_frame_args(rtsp_url, "pipe:1", width)

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return None, "ffmpeg не найден в образе"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None, f"кадр не пришёл за {timeout} с"

    if process.returncode != 0 or not stdout:
        detail = strip_credentials(stderr.decode("utf-8", "replace").strip())
        return None, (detail.splitlines() or ["ffmpeg не отдал кадр"])[-1][:300]

    return stdout, ""


async def capture_snapshot(camera_id: uuid.UUID, rtsp_url: str) -> bool:
    """Кладёт JPEG в snapshot_dir/<camera_id>.jpg. Ошибка не критична."""
    settings = get_settings()
    target = settings.snapshot_dir / f"{camera_id}.jpg"
    tmp = target.with_suffix(".tmp.jpg")

    try:
        settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("snapshot_dir_unavailable", error=str(exc))
        return False

    args = _ffmpeg_frame_args(rtsp_url, str(tmp), width=480)

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        log.warning("ffmpeg_missing")
        return False

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=SNAPSHOT_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        tmp.unlink(missing_ok=True)
        return False

    if process.returncode != 0 or not tmp.exists():
        # ffmpeg охотно печатает исходный URL целиком — вырезаем пароль,
        # иначе он осядет в логах docker.
        detail = strip_credentials(stderr.decode("utf-8", "replace").strip())[:200]
        log.info("snapshot_failed", camera_id=str(camera_id), detail=detail)
        tmp.unlink(missing_ok=True)
        return False

    # Замена одним движением: панель никогда не увидит наполовину записанный файл.
    tmp.replace(target)
    return True
