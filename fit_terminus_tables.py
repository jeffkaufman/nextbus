# Fits the quantile lookup tables that nextbus.py ships, and emits
# them as terminus_tables.py.
#
# Usage: python3 fit_terminus_tables.py [> terminus_tables.py]
#
# For every route and direction it takes the first stop after the
# terminus -- the place where the MBTA's terminus blind spot bites --
# and fits [p5, p95] of "how long until the next train" conditioned on
# what the vehicle feed shows plus a coarse time-of-day period.
#
# Only stops that actually pass leave-one-day-out calibration get
# emitted; a table whose claimed 5th percentile isn't really a 5th
# percentile is worse than showing nothing.  Diagnostics for every
# candidate go to stderr.

import datetime
import sys
import zoneinfo
from collections import defaultdict

from analyze_terminus_gap import (
    MAX_PLAUSIBLE_WAIT, build_timeline, headway_estimate, latest_meta,
    live_predictions, next_arrival_after, platform_parents, target_platform,
    vehicle_features)
from analyze_terminus_quantiles import (
    LOW, HIGH, MIN_TAIL_SAMPLES, quantile)

ROUTES = ["Green-B", "Green-C", "Green-D", "Green-E", "Mattapan",
          "Red", "Orange", "Blue"]

# The Green Line partial shutdown ran through 2026-08-16, and during it
# the C ran almost not at all.  Fitting across it teaches the tables
# that an empty terminus means "no train for ages", which is true of a
# shutdown and false of ordinary service.  Aug 17 onward is normal
# service and still covers two weekends, which is the regime the
# fallback actually runs in.
FIT_SINCE = "2026-08-17"

# A stop only gets a table if the fallback would actually be used
# there, i.e. the MBTA is frequently silent when a train is close.
MIN_SILENT_FRACTION = 0.25

# Calibration tolerances, leave-one-day-out.  The low tail is the one
# riders act on ("could I still make it?"), so it's held tighter.
MAX_LOW_TAIL = 0.09
MAX_HIGH_TAIL = 0.15


EASTERN = zoneinfo.ZoneInfo("America/New_York")


def period(when_utc):
  # Coarse service-frequency bucket.  Stateless -- the live site can't
  # keep headway history between requests -- and it recovers nearly
  # all of what a live-headway term would give (9.1m vs 8.6m median
  # window width at Ball Square).
  #
  # Bucket by Eastern *local* time, not by the UTC date: 01:00 UTC on
  # a Saturday is Friday evening in Boston, and calling that weekend
  # service puts Friday-evening trains in a weekend bucket.  nextbus.py
  # reproduces this function exactly.
  local = when_utc.astimezone(EASTERN)
  if local.hour < 5:
    return "late"
  if local.weekday() >= 5:
    return "weekend-%d" % (local.hour // 4)
  if local.hour < 10:
    return "am"
  if local.hour < 16:
    return "mid"
  if local.hour < 19:
    return "pm"
  return "eve"


def row_keys(row):
  # Most specific first; the runtime walks the same chain.
  it, inc, q = row["in_transit"], row["incoming"], row["queued"]
  return [(it, inc, q, row["period"]), (it, inc, q), (it, inc)]


def fit(rows):
  tables = defaultdict(list)
  for r in rows:
    for key in row_keys(r):
      tables[key].append(r["truth"])
  out = {}
  for key, waits in tables.items():
    if len(waits) < MIN_TAIL_SAMPLES:
      continue
    waits.sort()
    lo, hi = quantile(waits, LOW), quantile(waits, HIGH)
    # A p95 that lands on the clip isn't a measured quantile, it's
    # "nothing plausibly soon"; None tells the runtime to say so.
    out[key] = (int(round(lo)),
                None if hi >= MAX_PLAUSIBLE_WAIT else int(round(hi)))
  return out


def lookup(table, row):
  for key in row_keys(row):
    if key in table:
      return table[key]
  return None


def calibrate(rows, silent_only=True):
  if silent_only:
    rows = [r for r in rows if r["live"] is None]
  days = sorted({r["day"] for r in rows})
  below = above = total = 0
  for held in days:
    table = fit([r for r in rows if r["day"] != held])
    for r in rows:
      if r["day"] != held:
        continue
      got = lookup(table, r)
      if not got:
        continue
      lo, hi = got
      total += 1
      if r["truth"] < lo:
        below += 1
      if hi is not None and r["truth"] > hi:
        above += 1
  if not total:
    return None
  return below / total, above / total, total


def build(route, direction_id):
  meta = latest_meta(route)
  if not meta:
    return None
  order = [s["id"] for s in meta["stops_by_direction"][str(direction_id)]["data"]]
  if len(order) < 2:
    return None
  target_parent = order[1]
  target_index = 1
  parents = platform_parents(order)
  target_stop = target_platform(route, direction_id, target_parent, parents)
  if not target_stop:
    return None
  timeline, arrivals = build_timeline(
      route, direction_id, order, target_index, parents, precise_arrivals=True)
  live = live_predictions(route, direction_id, target_stop)

  rows = []
  for t in sorted(timeline):
    if t not in live:
      continue
    nxt = next_arrival_after(arrivals, t)
    if nxt is None:
      continue
    truth = min((nxt - t).total_seconds(), MAX_PLAUSIBLE_WAIT)
    if truth <= 0:
      continue
    day = t.date().isoformat()
    if day < FIT_SINCE:
      continue
    row = {"day": day, "truth": truth, "live": live[t],
           "period": period(t)}
    row.update(vehicle_features(timeline[t], direction_id, target_index))
    rows.append(row)
  if not rows:
    return None

  close = [r for r in rows if r["truth"] <= 6 * 60]
  silent = (sum(1 for r in close if r["live"] is None) / len(close)
            if close else 0.0)
  return {"route": route, "direction": direction_id, "stop": target_parent,
          "terminus": order[0], "rows": rows, "silent": silent,
          "arrivals": len(arrivals)}


def main():
  emit = {}
  for route in ROUTES:
    for direction_id in (0, 1):
      try:
        info = build(route, direction_id)
      except Exception as e:
        print("%s dir %d: failed (%s)" % (route, direction_id, e),
              file=sys.stderr)
        continue
      if not info:
        print("%s dir %d: no usable data" % (route, direction_id),
              file=sys.stderr)
        continue
      name = "%s dir %d %s" % (route, direction_id, info["stop"])
      if info["silent"] < MIN_SILENT_FRACTION:
        print("%-34s skip: silent only %.1f%% when a train is <=6min away"
              % (name, 100 * info["silent"]), file=sys.stderr)
        continue
      cal = calibrate(info["rows"])
      if not cal:
        print("%-34s skip: no bucket had enough samples" % name,
              file=sys.stderr)
        continue
      low, high, n = cal
      ok = low <= MAX_LOW_TAIL and high <= MAX_HIGH_TAIL
      print("%-34s silent %.0f%% n=%-6d <p5 %.1f%% >p95 %.1f%%  %s" % (
          name, 100 * info["silent"], n, 100 * low, 100 * high,
          "SHIP" if ok else "skip: badly calibrated"), file=sys.stderr)
      if ok:
        silent_rows = [r for r in info["rows"] if r["live"] is None]
        emit[(route, direction_id, info["stop"])] = {
            "terminus": info["terminus"], "table": fit(silent_rows)}

  print("# Generated by fit_terminus_tables.py -- do not edit by hand.")
  print("#")
  print("# [p5, p95] seconds until the next train at stops where the MBTA")
  print("# publishes no predictions, keyed by")
  print("# (route, direction_id, stop) -> (in_transit, incoming, queued,")
  print("# period) with backoff to shorter keys.  A p95 of None means the")
  print("# 95th percentile ran past the 45-minute measurement horizon,")
  print("# i.e. \"nothing plausibly soon\".")
  print()
  print("TABLES = {")
  for key in sorted(emit):
    entry = emit[key]
    print("    %r: {" % (key,))
    print("        \"terminus\": %r," % entry["terminus"])
    print("        \"table\": {")
    for bucket in sorted(entry["table"], key=lambda k: (len(k), [str(p) for p in k])):
      print("            %r: %r," % (bucket, entry["table"][bucket]))
    print("        },")
    print("    },")
  print("}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
