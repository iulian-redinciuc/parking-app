"""The forecast model (P9.6, docs/design/forecast.md): the 8-week median of P7.6 plus a small
gradient-boosted correction from the calendar, public holidays and the weather, trained
nightly on `zone_hour` and served only while it beats the median on the last 4 weeks."""
