"""Сводка по самому сервису: камеры, ссылки, зрители.

Одни и те же числа нужны в двух местах — в `/metrics` для Prometheus и на
странице мониторинга в панели. Считаются они здесь, чтобы не разъехались:
разные определения «действующей ссылки» в двух запросах никто бы не заметил,
пока графики не начали бы расходиться.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..internal import authz


@dataclass(frozen=True, slots=True)
class ServiceStats:
    #: Сколько включённых камер в каждом состоянии: online, idle, offline…
    cameras: dict[str, int] = field(default_factory=dict)
    #: Ссылки, которые прямо сейчас откроются: не отозваны и не просрочены.
    links: int = 0
    #: Зрители на публичных ссылках. Считаются по Redis, а не по журналу
    #: просмотров: в таблице лежат открытия страницы, а не текущие зрители.
    viewers: int = 0

    @property
    def cameras_total(self) -> int:
        return sum(self.cameras.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "cameras": self.cameras,
            "cameras_total": self.cameras_total,
            "links": self.links,
            "viewers": self.viewers,
        }


async def collect(session: AsyncSession) -> ServiceStats:
    rows = await session.execute(
        text("SELECT status::text, count(*) FROM cameras WHERE is_enabled GROUP BY status")
    )
    cameras = {str(status): int(count) for status, count in rows}

    links = await session.scalar(
        text(
            "SELECT count(*) FROM share_links "
            "WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
        )
    )
    return ServiceStats(
        cameras=cameras,
        links=int(links or 0),
        viewers=await authz.count_all_viewers(),
    )
