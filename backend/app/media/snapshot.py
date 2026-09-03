"""Снимок одного кадра — превью камеры в панели."""

from __future__ import annotations

import asyncio
import uuid

from ..config import get_settings
from ..logging_setup import get_logger
from .ssrf import strip_credentials

log = get_logger("snapshot")

SNAPSHOT_TIMEOUT = 25


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

    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        # Ширина 480, высота по пропорции и кратна двум.
        "-vf", "scale=480:-2",
        "-q:v", "6",
        "-y",
        str(tmp),
    ]

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
