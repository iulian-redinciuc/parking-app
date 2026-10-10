"""Training, the held-out check and serving of the forecast model (forecast.md §4-§6).

The model predicts how far an hour's `free_avg` ends up from the P7.6 median of the same
weekday and hour (`history.forecast_values`), from the calendar, the holidays and the
weather. It is trained on the hours before the last 4 weeks, compared with the median alone
on those 4 weeks, and marked `active` only if its mean absolute error there is at least 3%
lower; an active model is then refitted on all hours. `forecast_model` keeps one row per
zone with the comparison, whether it passed or not.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from sqlmodel import Session, col, func, select

from parking.db import history as hist
from parking.db.models import ForecastModel, ZoneHour
from parking.db.rollups import floor_hour
from parking.forecast import gbm
from parking.forecast.days import HolidayCalendar

log = logging.getLogger(__name__)

FEATURES = [
    "hour",
    "weekday",
    "month",
    "holiday",
    "holiday_eve",
    "holiday_after",
    "median",
    "spread",
    "temp_c",
    "precip_mm",
]
WEATHER_FEATURES = {FEATURES.index("temp_c"), FEATURES.index("precip_mm")}
HOLDOUT = timedelta(days=28)  # the "held-out month": the last 4 whole weeks
MIN_TRAIN_ROWS = 24 * 14  # hours with a median before the held-out weeks
MIN_HOLDOUT_ROWS = 24 * 14
MIN_HOUR_MINUTES = 30  # an hour with fewer minutes of data is not a target
MUST_BEAT = 0.97  # active only if MAE(model) < 0.97 × MAE(median)
MAX_AGE = timedelta(days=3)  # a model the nightly job stopped renewing is not served
NAN = float("nan")

Weather = tuple[float | None, float | None]


def features(
    at: datetime, values: list[float], tz: ZoneInfo, cal: HolidayCalendar, wx: Weather
) -> list[float]:
    """The feature row of the lot-local hour `at` is in; `values` = the median's samples."""
    local = at.astimezone(tz)
    temp, precip = wx
    return [
        float(local.hour),
        float(local.weekday()),
        float(local.month),
        *cal.flags(local.date()),
        statistics.median(values),
        max(values) - min(values),
        NAN if temp is None else float(temp),
        NAN if precip is None else float(precip),
    ]


@dataclass(frozen=True)
class Dataset:
    ts: list[datetime]
    x: np.ndarray
    median: np.ndarray
    free: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    def part(self, mask: np.ndarray) -> Dataset:
        ts = [t for t, keep in zip(self.ts, mask, strict=True) if keep]
        return Dataset(ts, self.x[mask], self.median[mask], self.free[mask])


def dataset(
    session: Session,
    zone_id: str,
    tz: ZoneInfo,
    cal: HolidayCalendar,
    wx: dict[datetime, Weather],
    end: datetime,
) -> Dataset:
    """One row per `zone_hour` before `end` that has a median (3 of the 8 weeks before it)."""
    rows = session.exec(
        select(ZoneHour)
        .where(ZoneHour.zone_id == zone_id, ZoneHour.bucket_ts < end)
        .order_by(col(ZoneHour.bucket_ts))
    ).all()
    free_at = {r.bucket_ts: r.free_avg for r in rows}
    ts, x, median, free = [], [], [], []
    for row in rows:
        if row.samples < MIN_HOUR_MINUTES:
            continue
        starts = hist.same_hour_last_weeks(row.bucket_ts, tz)
        values = [free_at[s] for s in starts if s in free_at]
        if len(values) < hist.FORECAST_MIN_POINTS:
            continue
        ts.append(row.bucket_ts)
        x.append(features(row.bucket_ts, values, tz, cal, wx.get(row.bucket_ts, (None, None))))
        median.append(statistics.median(values))
        free.append(row.free_avg)
    return Dataset(
        ts,
        np.array(x, dtype=float).reshape(len(ts), len(FEATURES)),
        np.array(median),
        np.array(free),
    )


def _whole(values: np.ndarray, top: float) -> np.ndarray:
    """As served: whole spaces within 0…top."""
    return np.clip(np.rint(values), 0, top)


@dataclass(frozen=True)
class Report:
    zone_id: str
    active: bool
    rows: int
    holdout_rows: int
    mae_model: float | None
    mae_median: float | None
    note: str


def train(
    session: Session,
    zone_id: str,
    tz: ZoneInfo,
    cal: HolidayCalendar,
    wx: dict[datetime, Weather],
    now: datetime,
) -> Report:
    """Train and check the zone's model, and write its `forecast_model` row."""
    end = floor_hour(now)
    data = dataset(session, zone_id, tz, cal, wx, end)
    held = np.array([t >= end - HOLDOUT for t in data.ts], dtype=bool)
    fit_part, test = data.part(~held), data.part(held)
    model: gbm.Gbm | None = None
    mae_model = mae_median = None
    if len(fit_part) < MIN_TRAIN_ROWS or len(test) < MIN_HOLDOUT_ROWS:
        note = (
            f"not enough history: {len(fit_part)} hours to train on (need {MIN_TRAIN_ROWS}) "
            f"and {len(test)} in the last 4 weeks (need {MIN_HOLDOUT_ROWS})"
        )
    else:
        top = float(data.free.max())
        candidate = gbm.fit(fit_part.x, fit_part.free - fit_part.median)
        predicted = _whole(test.median + candidate.predict(test.x), top)
        mae_model = float(np.abs(predicted - test.free).mean())
        mae_median = float(np.abs(_whole(test.median, top) - test.free).mean())
        if mae_model < MUST_BEAT * mae_median:
            model = gbm.fit(data.x, data.free - data.median)
            note = "beats the median on the last 4 weeks"
        else:
            note = "does not beat the median on the last 4 weeks"
    row = ForecastModel(
        zone_id=zone_id,
        trained_at=now,
        active=model is not None,
        rows=len(data),
        holdout_rows=len(test),
        mae_model=mae_model,
        mae_median=mae_median,
        note=note,
        model=None
        if model is None
        else json.dumps({"features": FEATURES, **model.to_dict()}, separators=(",", ":")),
    )
    session.merge(row)
    log.info(
        "forecast model %s: %s (MAE model %s, median %s, %d hours)",
        zone_id,
        note,
        "-" if mae_model is None else f"{mae_model:.2f}",
        "-" if mae_median is None else f"{mae_median:.2f}",
        len(data),
    )
    return Report(zone_id, row.active, row.rows, row.holdout_rows, mae_model, mae_median, note)


@dataclass(frozen=True)
class Active:
    model: gbm.Gbm
    uses_weather: bool

    def expected_free(
        self,
        at: datetime,
        values: list[float],
        tz: ZoneInfo,
        cal: HolidayCalendar,
        wx: Weather,
        capacity: int,
    ) -> int:
        """The median of `values` plus the model's correction, as whole spaces."""
        row = np.array([features(at, values, tz, cal, wx)])
        free = statistics.median(values) + float(self.model.predict(row)[0])
        if not math.isfinite(free):
            free = statistics.median(values)
        return min(max(round(free), 0), capacity)


def load_active(session: Session, zone_id: str, now: datetime) -> Active | None:
    """The zone's model if it passed its check, is recent and fits this code's features."""
    row = session.get(ForecastModel, zone_id)
    if row is None or not row.active or row.model is None or now - row.trained_at > MAX_AGE:
        return None
    try:
        data = json.loads(row.model)
        if data.get("features") != FEATURES:
            return None
        model = gbm.Gbm.from_dict(data)
    except (ValueError, KeyError, TypeError):
        log.warning("forecast model %s can't be read: using the median", zone_id)
        return None
    return Active(model, bool(model.used_features() & WEATHER_FEATURES))


def oldest_hour(session: Session, zone_ids: list[str]) -> datetime | None:
    return session.exec(
        select(func.min(ZoneHour.bucket_ts)).where(col(ZoneHour.zone_id).in_(zone_ids))
    ).one()
