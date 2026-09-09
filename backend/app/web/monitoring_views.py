"""Мониторинг: нагрузка сервера в графиках.

Страница отдаёт только каркас, числа приезжают отдельным запросом и потом
подкачиваются каждые несколько секунд. Иначе обновление данных означало бы
перезагрузку страницы, а график, который моргает раз в пять секунд, читать
нельзя.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth.deps import DbSession, RequireAdmin
from ..config import get_settings
from ..metrics import history
from ..metrics import service as service_metrics
from ..metrics.host import HostMetrics
from .templating import render

router = APIRouter(prefix="/admin/monitoring", tags=["monitoring"])

@dataclass(frozen=True, slots=True)
class Window:
    seconds: int
    label: str


#: Окна, которые предлагает переключатель наверху страницы. Список закрытый:
#: значение приходит из адресной строки и определяет, сколько записей уйдёт
#: клиенту, — произвольное число здесь означало бы «отдай всю историю».
RANGES: dict[str, Window] = {
    "5m": Window(300, "5 минут"),
    "15m": Window(900, "15 минут"),
    "1h": Window(3600, "Час"),
}
DEFAULT_RANGE = "15m"


@router.get("", response_class=HTMLResponse)
async def monitoring_page(request: Request, admin: RequireAdmin) -> HTMLResponse:
    settings = get_settings()
    return render(
        request,
        "monitoring.html",
        user=admin,
        ranges=RANGES,
        default_range=DEFAULT_RANGE,
        interval=settings.metrics_interval_seconds,
    )


@router.get("/data", response_class=JSONResponse)
async def monitoring_data(
    db: DbSession,
    admin: RequireAdmin,
    window: Annotated[str, Query(alias="range")] = DEFAULT_RANGE,
    since: Annotated[float, Query(ge=0)] = 0.0,
) -> JSONResponse:
    """Показания для графиков.

    `since` — время последнего замера, который уже есть у страницы. При
    обновлении раз в пять секунд она забирает один-два новых замера вместо
    всего часа истории; полностью история приезжает только при открытии
    страницы и при смене окна.
    """
    settings = get_settings()
    seconds = RANGES.get(window, RANGES[DEFAULT_RANGE]).seconds

    samples = await history.series(seconds, since=since)
    state = await history.state()
    stats = await service_metrics.collect(db)

    payload = {
        "available": state is not None,
        "reason": "" if state is not None else _explain_absence(),
        "interval": settings.metrics_interval_seconds,
        "window": seconds,
        "server_time": round(time.time(), 1),
        "series": samples,
        "state": state,
        "service": stats.as_dict(),
    }
    # no-store страницам панели ставит middleware, но этот ответ ещё и
    # опрашивается по кругу — промежуточный кэш здесь испортил бы график
    # молча и намертво.
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _explain_absence() -> str:
    """Почему графиков нет. Разбирается один раз — при пустой истории.

    Причин ровно две, и лечатся они по-разному: либо сбор невозможен в
    принципе (не Linux, procfs хоста не примонтирован), либо worker только
    что запустился и первого замера ещё нет — накопительным счётчикам ядра
    нужен предыдущий снимок.
    """
    reason = HostMetrics().available()
    if reason:
        return reason
    return (
        "Показания ещё собираются. Первые значения появятся через несколько секунд "
        "после запуска фонового процесса."
    )
