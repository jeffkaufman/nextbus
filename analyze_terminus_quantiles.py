# Estimates a *range* for the next train rather than a point estimate:
# the 5th and 95th percentile of how long until the next arrival.
#
# Usage: python3 analyze_terminus_quantiles.py <route> <direction_id> <stop>
#   e.g. python3 analyze_terminus_quantiles.py Green-E 0 place-balsq
#        python3 analyze_terminus_quantiles.py Red 0 place-davis
#
# The question this answers is "is it worth hustling?", not "when is
# the next train?".  p5 is the earliest a train plausibly shows up (if
# that's sooner than your walk, hustling can pay); p95 is the point by
# which one has almost certainly come and gone.
#
# Shares its data handling with analyze_terminus_gap.py.  The only new
# machinery is: quantile lookup tables instead of median ones, a
# backoff chain so a sparse bucket falls back to a coarser one rather
# than to a wild tail estimate, and calibration scoring -- a claimed
# 5th percentile is only worth anything if the truth really does land
# below it 5% of the time.

import datetime
import os
import statistics
import sys
from collections import defaultdict

from analyze_terminus_gap import (
    HEADWAY_BUCKET, MAX_PLAUSIBLE_WAIT, build_timeline, headway_estimate,
    latest_meta, live_predictions, next_arrival_after, platform_parents,
    target_platform, vehicle_features)

# Tail quantiles need far more samples than a median does.  A bucket
# with 30 observations has essentially no information about its own
# 5th percentile, so buckets below this back off to a coarser key.
MIN_TAIL_SAMPLES = 150

LOW, HIGH = 0.05, 0.95


def quantile(sorted_values, q):
  # Linear interpolation between order statistics.
  if not sorted_values:
    return None
  if len(sorted_values) == 1:
    return sorted_values[0]
  pos = q * (len(sorted_values) - 1)
  lo = int(pos)
  hi = min(lo + 1, len(sorted_values) - 1)
  return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def fit_tables(rows, key_fns):
  # One lookup table per level of the backoff chain.
  tables = []
  for key_fn in key_fns:
    table = defaultdict(list)
    for r in rows:
      table[key_fn(r)].append(r["truth"])
    tables.append({k: sorted(v) for k, v in table.items()})
  return tables


def lookup(tables, key_fns, row, quantiles):
  # Walk from the most specific key to the least, taking the first
  # bucket with enough samples to say something about its tails.
  for table, key_fn in zip(tables, key_fns):
    values = table.get(key_fn(row))
    if values and len(values) >= MIN_TAIL_SAMPLES:
      return [quantile(values, q) for q in quantiles], len(values)
  return None, 0


def leave_one_day_out(rows, key_fns, quantiles):
  days = sorted({r["day"] for r in rows})
  out = []
  for held in days:
    train = [r for r in rows if r["day"] != held]
    tables = fit_tables(train, key_fns)
    for r in rows:
      if r["day"] == held:
        qs, n = lookup(tables, key_fns, r, quantiles)
        if qs:
          out.append((qs, r["truth"], n))
  return out


def score(name, results):
  # Calibration is the whole game: if we claim a 5th percentile, the
  # truth should land below it 5% of the time.  Anything much lower
  # means we're being uselessly conservative; much higher means we're
  # telling people to relax when a train is about to leave without them.
  if not results:
    print("  %-26s (no coverage)" % name)
    return
  below = sum(1 for (lo, _, _), truth, _ in results if truth < lo)
  above = sum(1 for (_, _, hi), truth, _ in results if truth > hi)
  widths = sorted((hi - lo) for (lo, _, hi), _, _ in results)
  lows = sorted(lo for (lo, _, _), _, _ in results)
  print("  %-26s %7.1f%% %7.1f%% %8.1fm %8.1fm %8.1fm" % (
      name, 100.0 * below / len(results), 100.0 * above / len(results),
      statistics.median(widths) / 60, statistics.median(lows) / 60,
      quantile(lows, 0.1) / 60))


def fmt_wait(seconds):
  # A p95 that lands on the clip isn't a real quantile, it's "no train
  # any time soon" -- which is a perfectly good thing to tell a rider,
  # but it must not be printed as though we measured 45 minutes.
  if seconds >= MAX_PLAUSIBLE_WAIT:
    return " >%dm" % (MAX_PLAUSIBLE_WAIT // 60)
  return "%5.1fm" % (seconds / 60)


def hustle_gain(arrivals, index, t, walk, saved):
  # The decision the rider is actually making: they are `walk` minutes
  # from the platform and could get there `saved` minutes sooner by
  # hurrying.  That only ever helps if a train turns up in the window
  # between the two arrival times -- otherwise they catch the same
  # train either way, or miss it either way.
  import bisect
  lo = t + datetime.timedelta(seconds=(walk - saved) * 60)
  hi = t + datetime.timedelta(seconds=walk * 60)
  i = bisect.bisect_left(arrivals, lo)
  return i < len(arrivals) and arrivals[i] < hi


def build_rows(route, direction_id, target_parent):
  meta = latest_meta(route)
  order = [s["id"] for s in meta["stops_by_direction"][str(direction_id)]["data"]]
  target_index = order.index(target_parent)
  parents = platform_parents(order)
  target_stop = target_platform(route, direction_id, target_parent, parents)
  timeline, arrivals = build_timeline(
      route, direction_id, order, target_index, parents,
      precise_arrivals=True)
  live = live_predictions(route, direction_id, target_stop)

  rows = []
  censored = 0
  for t in sorted(timeline):
    if t not in live:
      continue
    nxt = next_arrival_after(arrivals, t)
    if nxt is None:
      continue
    truth = (nxt - t).total_seconds()
    if truth <= 0:
      continue
    is_censored = truth > MAX_PLAUSIBLE_WAIT
    if is_censored:
      # Kept, clipped, rather than dropped: throwing away every long
      # wait is exactly the bias that would make a p95 look good.
      truth = MAX_PLAUSIBLE_WAIT
      censored += 1
    headway = headway_estimate(arrivals, t)
    if headway is None:
      continue
    row = {"day": t.date().isoformat(), "t": t, "truth": truth,
           "headway": headway,
           "live": live[t], "hour": t.hour, "censored": is_censored}
    row.update(vehicle_features(timeline[t], direction_id, target_index))
    rows.append(row)
  return rows, arrivals, timeline, censored


def main():
  route, direction_id, target_parent = sys.argv[1], int(sys.argv[2]), sys.argv[3]
  rows, arrivals, timeline, censored = build_rows(
      route, direction_id, target_parent)

  # The Aug 11-16 window is the Green Line partial shutdown and Aug
  # 19-26 is the Orange one, so being able to refit on one service
  # period and check against another is the whole transferability
  # question.
  since, until = os.environ.get("SINCE"), os.environ.get("UNTIL")
  if since:
    rows = [r for r in rows if r["day"] >= since]
  if until:
    rows = [r for r in rows if r["day"] <= until]

  print("%s direction %d -> %s" % (route, direction_id, target_parent))
  print("%d arrivals, %d polls, %d usable rows (%d clipped at %d min)" % (
      len(arrivals), len(timeline), len(rows), censored,
      MAX_PLAUSIBLE_WAIT // 60))
  days = sorted({r["day"] for r in rows})
  print("days: %s .. %s (%d)" % (days[0], days[-1], len(days)))

  hb = lambda r: min(int(r["headway"] // HEADWAY_BUCKET), 8)
  lb = lambda r: ("none" if r["live"] is None
                  else min(int(r["live"] // 60), 15))

  def period(r):
    d = datetime.date.fromisoformat(r["day"])
    et = (r["hour"] - 4) % 24
    if et < 5:
      return "late"
    if d.weekday() >= 5:
      return "weekend-%d" % (et // 4)
    if et < 10:
      return "am"
    if et < 16:
      return "mid"
    if et < 19:
      return "pm"
    return "eve"

  candidates = [
      ("unconditional", [lambda r: ()]),
      ("headway", [hb, lambda r: ()]),
      ("vehicles", [lambda r: (r["in_transit"], r["incoming"], r["queued"]),
                    lambda r: (r["in_transit"], r["incoming"]),
                    lambda r: ()]),
      ("vehicles + headway",
       [lambda r: (r["in_transit"], r["incoming"], r["queued"], hb(r)),
        lambda r: (r["in_transit"], r["incoming"], r["queued"]),
        lambda r: (r["in_transit"], r["incoming"]),
        lambda r: ()]),
      ("vehicles + timeofday",
       [lambda r: (r["in_transit"], r["incoming"], r["queued"], period(r)),
        lambda r: (r["in_transit"], r["incoming"], r["queued"]),
        lambda r: (r["in_transit"], r["incoming"]),
        lambda r: ()]),
      ("live prediction", [lb, lambda r: ()]),
      ("live + vehicles",
       [lambda r: (lb(r), r["in_transit"], r["incoming"], r["queued"]),
        lambda r: (lb(r), r["in_transit"], r["incoming"]),
        lambda r: (lb(r),),
        lambda r: ()]),
  ]

  print("\ncalibration of a claimed [p5, p95] window, leave-one-day-out")
  print("  (want 5.0%% in each tail; 'p5 shown' is how early we'd tell you")
  print("   a train might turn up, and its own 10th percentile)")
  print("  %-26s %7s %7s %8s %8s %8s" % (
      "", "<p5", ">p95", "width", "p5 shown", "p10 of p5"))
  for name, key_fns in candidates:
    score(name, leave_one_day_out(rows, key_fns, (LOW, 0.5, HIGH)))

  best_keys = [lambda r: (r["in_transit"], r["incoming"], r["queued"], hb(r)),
               lambda r: (r["in_transit"], r["incoming"], r["queued"]),
               lambda r: (r["in_transit"], r["incoming"]),
               lambda r: ()]
  print("\nper-state calibration of vehicles+headway (want 5%% in each tail):")
  print("  %-40s %7s %7s %7s" % (
      "in_transit / incoming / queued", "n", "<p5", ">p95"))
  by_state = defaultdict(list)
  days = sorted({r["day"] for r in rows})
  for held in days:
    tables = fit_tables([r for r in rows if r["day"] != held], best_keys)
    for r in rows:
      if r["day"] != held:
        continue
      qs, _ = lookup(tables, best_keys, r, (LOW, 0.5, HIGH))
      if qs:
        by_state[(r["in_transit"], r["incoming"], r["queued"])].append((qs, r))
  for key in sorted(by_state):
    hits = by_state[key]
    if len(hits) < MIN_TAIL_SAMPLES:
      continue
    below = sum(1 for (lo, _, _), r in hits if r["truth"] < lo)
    above = sum(1 for (_, _, hi), r in hits if r["truth"] > hi)
    print("  in_transit=%d incoming=%d queued=%-20d %6d %6.1f%% %6.1f%%" % (
        key[0], key[1], key[2], len(hits),
        100.0 * below / len(hits), 100.0 * above / len(hits)))

  print("\nper-hour calibration of vehicles+headway (ET hour, want 5%%):")
  by_hour = defaultdict(list)
  for held in days:
    tables = fit_tables([r for r in rows if r["day"] != held], best_keys)
    for r in rows:
      if r["day"] != held:
        continue
      qs, _ = lookup(tables, best_keys, r, (LOW, 0.5, HIGH))
      if qs:
        by_hour[(r["hour"] - 4) % 24].append((qs, r))
  for key in sorted(by_hour):
    hits = by_hour[key]
    if len(hits) < MIN_TAIL_SAMPLES:
      continue
    below = sum(1 for (lo, _, _), r in hits if r["truth"] < lo)
    above = sum(1 for (_, _, hi), r in hits if r["truth"] > hi)
    print("  %02d:00 %6d  <p5 %5.1f%%  >p95 %5.1f%%" % (
        key, len(hits), 100.0 * below / len(hits), 100.0 * above / len(hits)))

  print("\nthe window itself, by vehicle state (whole period):")
  table = defaultdict(list)
  for r in rows:
    table[(r["in_transit"], r["incoming"], r["queued"])].append(r["truth"])
  print("  %-40s %7s %7s %7s %7s" % (
      "in_transit / incoming / queued", "n", "p5", "median", "p95"))
  for key in sorted(table):
    v = sorted(table[key])
    if len(v) < MIN_TAIL_SAMPLES:
      continue
    print("  in_transit=%d incoming=%d queued=%-20d %6d %s %s %s" % (
        key[0], key[1], key[2], len(v), fmt_wait(quantile(v, LOW)),
        fmt_wait(quantile(v, 0.5)), fmt_wait(quantile(v, HIGH))))

  if any(r["live"] is not None for r in rows):
    print("\nthe window given a live MBTA prediction of N minutes:")
    table = defaultdict(list)
    for r in rows:
      if r["live"] is not None:
        table[min(int(r["live"] // 60), 15)].append(r["truth"])
    print("  %-16s %7s %7s %7s %7s" % ("predicted", "n", "p5", "median", "p95"))
    for key in sorted(table):
      v = sorted(table[key])
      if len(v) < MIN_TAIL_SAMPLES:
        continue
      print("  %-14dm %6d %s %s %s" % (
          key, len(v), fmt_wait(quantile(v, LOW)), fmt_wait(quantile(v, 0.5)),
          fmt_wait(quantile(v, HIGH))))
  print("\n\"is it worth hustling?\" -- chance that arriving 2 min sooner")
  print("catches a train you would otherwise have missed, by vehicle state:")
  arr = arrivals
  print("  %-40s %7s %8s %8s %8s" % (
      "in_transit / incoming / queued", "n", "6min walk", "10min", "15min"))
  buckets = defaultdict(list)
  for r in rows:
    buckets[(r["in_transit"], r["incoming"], r["queued"])].append(r)
  for key in sorted(buckets):
    sub = buckets[key]
    if len(sub) < MIN_TAIL_SAMPLES:
      continue
    gains = []
    for walk in (6, 10, 15):
      hit = sum(1 for r in sub if hustle_gain(arr, None, r["t"], walk, 2))
      gains.append(100.0 * hit / len(sub))
    print("  in_transit=%d incoming=%d queued=%-20d %6d %7.1f%% %7.1f%% %7.1f%%" % (
        key[0], key[1], key[2], len(sub), gains[0], gains[1], gains[2]))
  return 0


if __name__ == "__main__":
  sys.exit(main())
