"""Public holidays for the forecast features (forecast.md §3): the `holidays` package for
the country in lot.yaml (`forecast.holidays`), plus the lot's own `extra_holidays`."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from functools import lru_cache


def check_country(country: str, subdiv: str | None = None) -> None:
    """Raise ValueError unless `holidays` knows the country (and its subdivision)."""
    import holidays

    supported = holidays.list_supported_countries()
    if country not in supported:
        raise ValueError(f"forecast.holidays: unknown country code '{country}' (e.g. RO, DE)")
    if subdiv is not None and subdiv not in supported[country]:
        raise ValueError(f"forecast.holidays_subdiv: '{subdiv}' is not a subdivision of {country}")


class HolidayCalendar:
    def __init__(
        self, country: str | None = None, subdiv: str | None = None, extra: Iterable[date] = ()
    ):
        self._extra = frozenset(extra)
        self._country = None
        if country:
            import holidays  # ~0.3 s to import: only where a country is set

            self._country = holidays.country_holidays(country, subdiv=subdiv)

    def is_holiday(self, day: date) -> bool:
        return day in self._extra or (self._country is not None and day in self._country)

    def flags(self, day: date) -> tuple[float, float, float]:
        """`(holiday, the day before one, the day after one)` as 0/1."""
        one = timedelta(days=1)
        return (
            float(self.is_holiday(day)),
            float(self.is_holiday(day + one)),
            float(self.is_holiday(day - one)),
        )


@lru_cache(maxsize=16)
def _calendar(country: str | None, subdiv: str | None, extra: tuple[date, ...]) -> HolidayCalendar:
    return HolidayCalendar(country, subdiv, extra)


def calendar_of(cfg) -> HolidayCalendar:
    """The calendar of a lot's `forecast` config (built once per distinct setting)."""
    return _calendar(cfg.holidays, cfg.holidays_subdiv, tuple(cfg.extra_holidays))
