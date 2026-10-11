"""Control-quality report for the shared diagnostic snapshot (Issue #1066).

One pure function, ``build_control_quality()``, turns in-memory history into a small,
identifier-free report that any user can attach to a bug report: how often the thermostat
was armed on the wrong side of the comfort band, how long the equipment ran, how much of
that running a "defer a little while the house drifts back" rule could have skipped, and
how accurate the archived indoor prediction is. No Home Assistant imports, no I/O, never
raises on odd input -- fields are ``None`` when there is not enough data (never a made-up 0).

Confirmed input shapes (read from the code, Issue #1066):

* Chart-log entries (``chart_log.ChartStateLog.append``): ``ts`` (ISO), ``hvac`` (the
  thermostat's *hvac_action* string -- ``"heating"``/``"cooling"``/``"idle"``/``"fan"``/``""``
  -- NOT the mode; only ``heating``/``cooling`` count as running, same set as
  ``hvac_action.HVAC_ACTION_COMPRESSOR_ACTIVE``), ``indoor``, ``outdoor`` (``None`` on
  ``hvac_action_change`` event rows), ``lower``/``upper`` (resolved target band at that
  instant), ``pred_indoor`` (the archived ODE prediction made ~4 h earlier **for that
  entry's own timestamp**, not a forward forecast), all internal Fahrenheit.
  ``get_entries("3d")`` and shorter return raw rows; ``"7d"`` returns hourly averages
  (``hvac`` = dominant action in the hour, so short runs older than 3 days are undercounted).
  ``merge_chart_resolutions()`` stitches raw (last 3 days) + hourly (older) rows.
* Indoor history (``coordinator._indoor_temp_history``): ``list[(iso_ts, float_F)]``.
* Mode changes (``AutomationEngine.comfort_mode_changes``): list of dicts, newest last,
  ``{ts, from_mode, to_mode, indoor_f, floor_f, ceiling_f, margin_f, reason}``.

All temperature values in the output are internal Fahrenheit (``temp_unit`` only reports the
user's display unit so a reader knows what the thermostat UI shows).

Definitions
-----------
* Interval: two consecutive chart rows (a, b). It is credited to row ``a``'s state and lasts
  ``min(b.ts - a.ts, 60 min)``. The 60 min cap stops a data gap (HA down, log hole) from being
  billed as run time; a capped interval also ends any run in progress.
* Run: consecutive running intervals with the same action. Short run = under 10 minutes.
* Misarmed: the armed mode (from the ring buffer, in force until the next change) is on the
  wrong side of the band stored in that change's record: ``cool`` while indoor < floor or
  ``heat`` while indoor > ceiling. Indoor samples come from chart rows, indoor history and the
  ring-buffer records themselves; samples sharing a timestamp are counted once (the chart row wins
  over indoor history, which wins over the ring-buffer record). A sample whose segment straddles the
  window start is clipped to the window rather than dropped. An episode is a contiguous misarmed
  stretch under one arming.
  Misarmed time is measured from the COMMANDED mode (the ring buffer), so it reads 0 when the
  thermostat ignores a command and stays in the wrong mode: it detects bad arming decisions, not
  a thermostat that fails to follow them.
* Edge-to-armed latency: for a change to ``cool`` (``heat``), time from the first sample of the
  run of indoor > ceiling (< floor) that ends at the change, to the change. Changes made while
  indoor is still inside the band (pre-arming) are not measurable and are skipped.
* Run context: cooling with outdoor >= indoor = house gaining heat, else passive loss; heating
  with outdoor <= indoor = house losing heat, else passive gain. Outdoor on event rows is
  forward-filled from the previous row (at most 120 min old).
* Would-have-deferred (candidate rule, nothing is deferred in production): a running interval
  where the indoor reading is strictly past the row's own edge but by at most the 1 F tolerance (cooling:
  the excess over the edge is positive and within the 1 F tolerance, i.e. indoor - upper for
  cooling, lower - indoor for heating; a reading inside the band, as during TOU pre-cool banking,
  is NOT deferrable), outdoor is on the self-correcting side of indoor, outdoor moved that way
  over the last 60 min, and the archived ``pred_indoor`` of the row nearest +30 min
  (+/-15 min) is back inside the edge. One direction-parameterised helper serves both sides.
* Prediction error: ``pred_indoor`` is a prediction *for its own timestamp*, so the horizon
  figures compare the predicted *change* over +30/+60/+120 min (``pred(b) - pred(a)``) with the
  actual change (``indoor(b) - indoor(a)``) for the nearest later row b within +/-15 min of
  the horizon -- the quantity a drift-based deferral relies on. Signed = predicted - actual.
  ``archive_lead`` is the plain ``pred_indoor - indoor`` at the same row (the ~4 h lead error).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

CONTROL_QUALITY_SCHEMA = 1

# (window label, hours)
_WINDOWS: tuple[tuple[str, int], ...] = (("24h", 24), ("7d", 168))
_GAP_CAP = timedelta(minutes=60)
_SHORT_RUN_MIN = 10.0
_OUTDOOR_FILL_MAX = timedelta(minutes=120)
_DEFER_TOLERANCE_F = 1.0
_DEFER_WINDOW = timedelta(minutes=30)
_DEFER_TREND_LOOKBACK = timedelta(minutes=60)
_DEFER_TREND_SLACK = timedelta(minutes=20)
_PRED_TOLERANCE = timedelta(minutes=15)
_HORIZONS: tuple[tuple[str, int], ...] = (("30m", 30), ("60m", 60), ("120m", 120))
_RUNNING = ("heating", "cooling")
_RAW_RANGE_DAYS = 3


@dataclass(frozen=True, slots=True)
class _Row:
    """One normalised chart-log row."""

    t: datetime
    hvac: str
    indoor: float | None
    outdoor: float | None
    lower: float | None
    upper: float | None
    pred: float | None


@dataclass(frozen=True, slots=True)
class _Change:
    """One validated mode-change record."""

    t: datetime
    to_mode: str | None
    indoor: float | None
    floor: float | None
    ceiling: float | None

    @property
    def scorable(self) -> bool:
        """Armed heat/cool with a stored band: the only records misarmed/latency can score."""
        return self.to_mode in ("heat", "cool") and self.floor is not None and self.ceiling is not None


# ----------------------------------------------------------------------
# Parsing helpers (defensive: bad input is skipped, never raised)
# ----------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _r1(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0


def merge_chart_resolutions(raw: Any, hourly: Any, now: datetime) -> list[dict[str, Any]]:
    """Raw rows (last 3 days) plus hourly-bucket rows older than that, for the 7 d window."""
    cutoff = _aware(now) - timedelta(days=_RAW_RANGE_DAYS)
    raw_rows = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    old_rows = []
    if isinstance(hourly, list):
        for e in hourly:
            t = _ts(e.get("ts")) if isinstance(e, dict) else None
            if t is not None and t < cutoff:
                old_rows.append(e)
    return old_rows + raw_rows


def _normalise_rows(chart_entries: Any) -> list[_Row]:
    """Parse, sort and forward-fill outdoor (<= 120 min stale) on chart rows."""
    parsed: list[_Row] = []
    if not isinstance(chart_entries, list):
        return parsed
    for e in chart_entries:
        if not isinstance(e, dict):
            continue
        t = _ts(e.get("ts"))
        if t is None:
            continue
        hvac = e.get("hvac")
        parsed.append(
            _Row(
                t,
                hvac.lower() if isinstance(hvac, str) else "",
                _num(e.get("indoor")),
                _num(e.get("outdoor")),
                _num(e.get("lower")),
                _num(e.get("upper")),
                _num(e.get("pred_indoor")),
            )
        )
    parsed.sort(key=lambda r: r.t)
    filled: list[_Row] = []
    last_out: tuple[datetime, float] | None = None
    for r in parsed:
        out = r.outdoor
        if out is not None:
            last_out = (r.t, out)
        elif last_out is not None and r.t - last_out[0] <= _OUTDOOR_FILL_MAX:
            out = last_out[1]
        filled.append(_Row(r.t, r.hvac, r.indoor, out, r.lower, r.upper, r.pred))
    return filled


def _normalise_changes(mode_changes: Any) -> list[_Change]:
    out: list[_Change] = []
    if not isinstance(mode_changes, list):
        return out
    for c in mode_changes:
        if not isinstance(c, dict):
            continue
        t = _ts(c.get("ts"))
        to_mode = c.get("to_mode")
        if t is None:
            continue
        # "off"/None/other = unarmed (e.g. a windows-open pause): kept as a timeline marker so
        # the previous arming stops being scored, but never scored itself.
        out.append(
            _Change(
                t,
                to_mode if isinstance(to_mode, str) else None,
                _num(c.get("indoor_f")),
                _num(c.get("floor_f")),
                _num(c.get("ceiling_f")),
            )
        )
    out.sort(key=lambda c: c.t)
    return out


def _indoor_series(rows: list[_Row], changes: list[_Change], indoor_history: Any) -> list[tuple[datetime, float]]:
    """All indoor samples (chart rows, indoor history, ring-buffer records), time-sorted."""
    by_ts: dict[datetime, float] = {}
    for r in rows:  # chart rows first: they win a timestamp tie
        if r.indoor is not None:
            by_ts.setdefault(r.t, r.indoor)
    if isinstance(indoor_history, (list, tuple)):
        for item in indoor_history:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                t, v = _ts(item[0]), _num(item[1])
                if t is not None and v is not None:
                    by_ts.setdefault(t, v)
    for c in changes:
        if c.indoor is not None:
            by_ts.setdefault(c.t, c.indoor)
    return sorted(by_ts.items())


# ----------------------------------------------------------------------
# Interval iteration
# ----------------------------------------------------------------------


def _intervals(rows: list[_Row], start: datetime, end: datetime) -> list[tuple[_Row, float, bool]]:
    """(row, capped minutes, capped?) for each interval whose start row is inside [start, end)."""
    out: list[tuple[_Row, float, bool]] = []
    for a, b in zip(rows, rows[1:], strict=False):
        if a.t < start or a.t >= end:
            continue
        gap = min(b.t, end) - a.t
        capped = gap > _GAP_CAP
        out.append((a, _minutes(min(gap, _GAP_CAP)), capped))
    return out


# ----------------------------------------------------------------------
# Metric blocks
# ----------------------------------------------------------------------


def _runtime(intervals: list[tuple[_Row, float, bool]]) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = {
        "heating": {"minutes": 0.0, "runs": [], "cur": None},
        "cooling": {"minutes": 0.0, "runs": [], "cur": None},
    }
    for row, mins, capped in intervals:
        for action, s in stats.items():
            if row.hvac == action:
                s["minutes"] += mins
                s["cur"] = (s["cur"] or 0.0) + mins
                if capped:
                    s["runs"].append(s["cur"])
                    s["cur"] = None
            elif s["cur"] is not None:
                s["runs"].append(s["cur"])
                s["cur"] = None
    result: dict[str, Any] = {}
    for action, s in stats.items():
        if s["cur"] is not None:
            s["runs"].append(s["cur"])
        runs = s["runs"]
        result[action] = {
            "minutes": _r1(s["minutes"]),
            "runs": len(runs),
            "median_run_minutes": _r1(median(runs)) if runs else None,
            "short_runs_lt_10m": sum(1 for r in runs if r < _SHORT_RUN_MIN),
        }
    return result


def _empty_runtime() -> dict[str, Any]:
    blank = {"minutes": None, "runs": None, "median_run_minutes": None, "short_runs_lt_10m": None}
    return {"heating": dict(blank), "cooling": dict(blank)}


def _run_context(intervals: list[tuple[_Row, float, bool]]) -> dict[str, Any]:
    gaining = loss = h_losing = h_gain = 0.0
    for row, mins, _capped in intervals:
        if row.indoor is None or row.outdoor is None:
            continue
        if row.hvac == "cooling":
            if row.outdoor >= row.indoor:
                gaining += mins
            else:
                loss += mins
        elif row.hvac == "heating":
            if row.outdoor <= row.indoor:
                h_losing += mins
            else:
                h_gain += mins
    return {
        "cooling_minutes_house_gaining_heat": _r1(gaining),
        "cooling_minutes_passive_loss": _r1(loss),
        "heating_minutes_house_losing_heat": _r1(h_losing),
        "heating_minutes_passive_gain": _r1(h_gain),
    }


def _empty_run_context() -> dict[str, Any]:
    return {
        "cooling_minutes_house_gaining_heat": None,
        "cooling_minutes_passive_loss": None,
        "heating_minutes_house_losing_heat": None,
        "heating_minutes_passive_gain": None,
    }


def _nearest(times: list[datetime], rows: list[_Row], target: datetime, tol: timedelta) -> _Row | None:
    """Row whose timestamp is closest to ``target`` within ``tol`` (rows sorted by time)."""
    i = bisect.bisect_left(times, target)
    best: _Row | None = None
    for j in (i - 1, i):
        if (
            0 <= j < len(rows)
            and abs(rows[j].t - target) <= tol
            and (best is None or abs(rows[j].t - target) < abs(best.t - target))
        ):
            best = rows[j]
    return best


def _deferrable_minutes(
    intervals: list[tuple[_Row, float, bool]], rows: list[_Row], times: list[datetime], *, cooling: bool
) -> float:
    """Running minutes meeting the candidate deferral rule, for ONE direction.

    ``sign`` mirrors every comparison so cooling and heating share this single code path:
    cooling uses sign=+1 against the ceiling, heating sign=-1 against the floor.
    """
    sign = 1.0 if cooling else -1.0
    action = "cooling" if cooling else "heating"
    total = 0.0
    for row, mins, _capped in intervals:
        edge = row.upper if cooling else row.lower
        if row.hvac != action or edge is None or row.indoor is None or row.outdoor is None:
            continue
        excess = sign * (row.indoor - edge)
        if excess <= 0 or excess > _DEFER_TOLERANCE_F:  # must be strictly past the edge, by <= 1 F
            continue
        if sign * (row.outdoor - row.indoor) >= 0:
            continue
        earlier = _nearest(times, rows, row.t - _DEFER_TREND_LOOKBACK, _DEFER_TREND_SLACK)
        if earlier is None or earlier.outdoor is None or sign * (row.outdoor - earlier.outdoor) >= 0:
            continue
        ahead = _nearest(times, rows, row.t + _DEFER_WINDOW, _PRED_TOLERANCE)
        if ahead is None or ahead.pred is None or sign * (ahead.pred - edge) > 0:
            continue
        total += mins
    return total


def _prediction_error(rows: list[_Row], times: list[datetime], start: datetime, end: datetime) -> dict[str, Any]:
    def stat(errors: list[float]) -> dict[str, Any]:
        n = len(errors)
        return {
            "mean_abs_f": _r1(sum(abs(e) for e in errors) / n) if n else None,
            "mean_signed_f": _r1(sum(errors) / n) if n else None,
            "n": n,
        }

    horizons: dict[str, Any] = {}
    for label, minutes in _HORIZONS:
        errors: list[float] = []
        for a in rows:
            if a.t < start or a.t >= end or a.pred is None or a.indoor is None:
                continue
            b = _nearest(times, rows, a.t + timedelta(minutes=minutes), _PRED_TOLERANCE)
            if b is None or b.t <= a.t or b.pred is None or b.indoor is None:
                continue
            errors.append((b.pred - a.pred) - (b.indoor - a.indoor))
        horizons[label] = stat(errors)
    lead = [r.pred - r.indoor for r in rows if start <= r.t < end and r.pred is not None and r.indoor is not None]
    return {"horizons": horizons, "archive_lead": stat(lead)}


def _misarmed(
    changes: list[_Change], series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> dict[str, Any]:
    """Minutes/episodes the armed mode sat on the wrong side of its stored band."""
    none = {"minutes": None, "episodes": None, "longest_minutes": None, "percent_of_window": None}
    if not changes:
        return none
    change_times = [c.t for c in changes]
    evaluated = 0
    minutes = 0.0
    episodes: list[float] = []
    cur_len: float | None = None
    cur_key: int | None = None
    cur_end: datetime | None = None
    for i, (t, indoor) in enumerate(series):
        if t > end:
            break
        k = bisect.bisect_right(change_times, t) - 1
        if k < 0:
            continue
        nxt_sample = series[i + 1][0] if i + 1 < len(series) else end  # series is deduped + sorted
        nxt_change = change_times[k + 1] if k + 1 < len(change_times) else end
        seg_end = min(nxt_sample, nxt_change, t + _GAP_CAP, end)
        seg_start = max(t, start)  # clip a segment that straddles the window start
        if seg_end < seg_start or (t < start and seg_end <= start):
            continue
        dur = _minutes(seg_end - seg_start)
        c = changes[k]
        if not c.scorable:
            if c.to_mode not in ("heat", "cool"):
                evaluated += 1  # unarmed: a real 0 misarmed minutes, not missing data
            continue
        evaluated += 1
        wrong = indoor < c.floor if c.to_mode == "cool" else indoor > c.ceiling
        if wrong:
            minutes += dur
            contiguous = (
                cur_len is not None
                and cur_key == k
                and cur_end is not None
                and seg_start <= cur_end + timedelta(seconds=1)
            )
            if contiguous:
                cur_len += dur  # type: ignore[operator]
            else:
                if cur_len is not None:
                    episodes.append(cur_len)
                cur_len = dur
            cur_key, cur_end = k, seg_end
        elif cur_len is not None:
            episodes.append(cur_len)
            cur_len = None
    if cur_len is not None:
        episodes.append(cur_len)
    if evaluated == 0:
        return none
    window_min = _minutes(end - start)
    return {
        "minutes": _r1(minutes),
        "episodes": len(episodes),
        "longest_minutes": _r1(max(episodes)) if episodes else 0.0,
        "percent_of_window": _r1(100.0 * minutes / window_min) if window_min > 0 else None,
    }


def _latencies(
    changes: list[_Change], series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> dict[str, Any] | None:
    """Seconds from indoor first being outside the band to the matching mode being armed."""
    times = [s[0] for s in series]
    values: list[float] = []
    for c in changes:
        if c.t < start or c.t > end or not c.scorable:
            continue
        hi = bisect.bisect_right(times, c.t)
        if hi == 0:
            continue

        def outside(v: float, c: _Change = c) -> bool:
            return v > c.ceiling if c.to_mode == "cool" else v < c.floor

        if not outside(series[hi - 1][1]):
            continue
        j = hi - 1
        while j > 0 and outside(series[j - 1][1]):
            j -= 1
        values.append((c.t - series[j][0]).total_seconds())
    if not values:
        return None
    return {"median": _r1(median(values)), "max": _r1(max(values)), "n": len(values)}


def _window(
    label: str,
    hours: int,
    *,
    now: datetime,
    changes: list[_Change],
    all_changes_empty: bool,
    rows: list[_Row],
    times: list[datetime],
    series: list[tuple[datetime, float]],
) -> dict[str, Any]:
    start = now - timedelta(hours=hours)
    intervals = _intervals(rows, start, now)
    in_window = [c for c in changes if start <= c.t <= now]
    have_chart = bool(intervals)
    count: int | None = None if all_changes_empty else len(in_window)
    covered = sum(m for _r, m, _c in intervals) / 60.0
    return {
        "samples": {
            "mode_changes": len(in_window),
            "chart_entries": sum(1 for r in rows if start <= r.t <= now),
            "coverage_hours": _r1(covered) if have_chart else None,
        },
        "misarmed": _misarmed(changes, series, start, now),
        "mode_changes": {
            "count": count,
            "per_day": _r1(count / (hours / 24.0)) if count is not None else None,
            "edge_to_armed_latency_s": _latencies(changes, series, start, now),
        },
        "runtime": _runtime(intervals) if have_chart else _empty_runtime(),
        "run_context": _run_context(intervals) if have_chart else _empty_run_context(),
        "would_have_deferred": {
            "cooling_minutes": _r1(_deferrable_minutes(intervals, rows, times, cooling=True)) if have_chart else None,
            "heating_minutes": _r1(_deferrable_minutes(intervals, rows, times, cooling=False)) if have_chart else None,
        },
        "prediction_error": _prediction_error(rows, times, start, now),
    }


def build_control_quality(
    *,
    now: datetime,
    mode_changes: list[dict[str, Any]],
    chart_entries: list[dict[str, Any]],
    indoor_history: list[Any] | None = None,
    temp_unit: str = "fahrenheit",
) -> dict[str, Any]:
    """Build the control-quality report (see module docstring for every definition)."""
    now = _aware(now)
    rows = _normalise_rows(chart_entries)
    changes = _normalise_changes(mode_changes)
    times = [r.t for r in rows]
    series = _indoor_series(rows, changes, indoor_history)
    return {
        "schema": CONTROL_QUALITY_SCHEMA,
        "generated_at": now.isoformat(),
        "temp_unit": temp_unit if isinstance(temp_unit, str) else "fahrenheit",
        "windows": {
            label: _window(
                label,
                hours,
                now=now,
                changes=changes,
                all_changes_empty=not changes,
                rows=rows,
                times=times,
                series=series,
            )
            for label, hours in _WINDOWS
        },
    }
