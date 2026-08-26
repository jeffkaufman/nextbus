# Measures the terminus prediction gap at one stop, and compares
# fallback estimators for filling it.
#
# Usage: python3 analyze_terminus_gap.py <route> <direction_id> <stop>
#   e.g. python3 analyze_terminus_gap.py Green-E 0 place-balsq
#
# Reads the logs written by log_termini.py (NEXTBUS_LOG_DIR, default
# ./logs).  Needs network access to map platform stop ids to their
# parent stations.
#
# Ground truth (when a train actually reached the stop) comes from
# vehicle positions, since predictions are exactly what's missing
# during the gap.  A vehicle "arrives" the first poll at which its
# position, expressed as an index into that direction's stop list, is
# at or past the target -- so a train that slips between two 60s polls
# still counts.  Vehicles are tracked by vehicle id rather than trip
# id: the MBTA reassigns a train's trip id while it lays over at a
# terminus, which silently breaks any trip-keyed accounting.
#
# Estimators are scored leave-one-day-out, so the reported accuracy is
# out-of-sample rather than a fit to the same week.

import datetime
import glob
import gzip
import json
import os
import statistics
import sys
import urllib.request
from collections import defaultdict

LOG_DIR = os.environ.get(
    "NEXTBUS_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))

# Waits longer than this are "no more service tonight", not an
# instance of the gap, so they're left out.
MAX_PLAUSIBLE_WAIT = 45 * 60

HEADWAY_LOOKBACK = 90 * 60
HEADWAY_COUNT = 5

# Positions are bucketed for the lookup tables the estimators learn;
# finer buckets than this just overfit.
QUEUE_CAP = 3
HEADWAY_BUCKET = 120
MIN_BUCKET_SAMPLES = 25

# Vehicle positions go stale -- median age is ~25s but the tail is
# long, and 6.8% of terminus observations are over 5 minutes old
# (peaking near 28% during the 06:00 pull-out).  A train whose
# position stopped updating half an hour ago should not count towards
# the terminus queue.
MAX_POSITION_AGE = 5 * 60


def parse_time(s):
  return datetime.datetime.fromisoformat(s)


def load_records(route, kind):
  pattern = os.path.join(LOG_DIR, "%s-*-%s.jsonl.gz" % (route, kind))
  for path in sorted(glob.glob(pattern)):
    with gzip.open(path, "rt") as f:
      for line in f:
        line = line.strip()
        if line:
          yield json.loads(line)


def latest_meta(route):
  last = None
  for obj in load_records(route, "meta"):
    last = obj
  return last


def api_get(path):
  with urllib.request.urlopen("https://api-v3.mbta.com" + path) as f:
    return json.load(f)


def platform_parents(parent_ids):
  # platform stop id -> parent station id.  Vehicle and prediction
  # records are tied to platforms; the stop order we get from the
  # route metadata is in terms of parent stations.
  mapping = {}
  parent_ids = sorted(parent_ids)
  for i in range(0, len(parent_ids), 40):
    chunk = parent_ids[i:i + 40]
    doc = api_get("/stops?filter[id]=%s&include=child_stops" % ",".join(chunk))
    for stop in doc["data"]:
      mapping[stop["id"]] = stop["id"]
      for child in stop["relationships"]["child_stops"]["data"]:
        mapping[child["id"]] = stop["id"]
  return mapping


def target_platform(route, direction_id, target_parent, parents):
  # The platform serving this direction, found by intersecting the
  # station's platforms with stop ids that actually show up in this
  # direction's predictions -- more reliable than guessing from
  # platform naming.
  candidates = {sid for sid, parent in parents.items()
                if parent == target_parent}
  seen = defaultdict(int)
  for rec in load_records(route, "predictions"):
    for p in rec["response"]["data"]:
      if p["attributes"]["direction_id"] != direction_id:
        continue
      sid = p["relationships"]["stop"]["data"]["id"]
      if sid in candidates:
        seen[sid] += 1
  if not seen:
    return None
  return max(seen, key=seen.get)


def build_timeline(route, direction_id, order, target_index, parents,
                   precise_arrivals=False):
  # Returns (timeline, arrivals): per-poll vehicle state, and the
  # times at which a train crossed into the target stop.
  index = {sid: i for i, sid in enumerate(order)}
  timeline = {}
  arrivals = []
  furthest = {}  # vehicle id -> furthest index reached on this run

  for rec in load_records(route, "vehicles"):
    t = parse_time(rec["polled_at"])
    entries = []
    for v in rec["response"]["data"]:
      stop = v["relationships"].get("stop", {}).get("data")
      if not stop:
        continue
      parent = parents.get(stop["id"])
      if parent not in index:
        continue
      i = index[parent]
      status = v["attributes"]["current_status"]
      # INCOMING_AT/IN_TRANSIT_TO mean the train hasn't reached that
      # stop yet, so the last stop it actually passed is the one before.
      pos = i if status == "STOPPED_AT" else i - 1
      age = (t - parse_time(v["attributes"]["updated_at"])).total_seconds()
      entries.append({"vid": v["id"], "pos": pos, "status": status, "age": age,
                      "at": i, "direction": v["attributes"].get("direction_id")})

      if v["attributes"].get("direction_id") != direction_id:
        continue
      previous = furthest.get(v["id"])
      if previous is None or pos < previous - 2:
        furthest[v["id"]] = pos  # new run: train turned around or reappeared
        continue
      if previous < target_index <= pos:
        # The poll time overstates when the train actually got here:
        # the position we just read was reported at updated_at, which
        # is typically ~25s earlier and occasionally minutes.  Timing
        # arrivals by the poll biases every wait estimate late, which
        # for a "should I hustle?" answer is the dangerous direction.
        arrivals.append(t - datetime.timedelta(seconds=age)
                        if precise_arrivals else t)
      furthest[v["id"]] = max(previous, pos)
    timeline[t] = entries

  arrivals.sort()
  return timeline, arrivals


def next_arrival_after(arrivals, t):
  lo, hi = 0, len(arrivals)
  while lo < hi:
    mid = (lo + hi) // 2
    if arrivals[mid] <= t:
      lo = mid + 1
    else:
      hi = mid
  return arrivals[lo] if lo < len(arrivals) else None


def headway_estimate(arrivals, t):
  window = [a for a in arrivals
            if t - datetime.timedelta(seconds=HEADWAY_LOOKBACK) <= a < t]
  if len(window) < 2:
    return None
  window = window[-HEADWAY_COUNT:]
  gaps = [(window[i] - window[i - 1]).total_seconds()
          for i in range(1, len(window))]
  headway = statistics.median(gaps)
  nxt = window[-1] + datetime.timedelta(seconds=headway)
  while nxt <= t:
    nxt += datetime.timedelta(seconds=headway)
  return (nxt - t).total_seconds()


def live_predictions(route, direction_id, target_stop):
  # poll time -> soonest predicted wait, or None when the MBTA is
  # publishing nothing for this stop and direction.
  out = {}
  for rec in load_records(route, "predictions"):
    t = parse_time(rec["polled_at"])
    best = None
    for p in rec["response"]["data"]:
      attrs = p["attributes"]
      if attrs["direction_id"] != direction_id:
        continue
      if p["relationships"]["stop"]["data"]["id"] != target_stop:
        continue
      when = attrs.get("arrival_time") or attrs.get("departure_time")
      if not when:
        continue
      wait = (parse_time(when) - t).total_seconds()
      if wait >= 0 and (best is None or wait < best):
        best = wait
    out[t] = best
  return out


def vehicle_features(entries, direction_id, target_index):
  # What the /vehicles endpoint tells us about trains that could
  # plausibly show up next: ones that have left the terminus and are
  # still short of the target, and ones sitting at the terminus.
  in_transit = [e for e in entries
                if e["direction"] == direction_id
                and 0 <= e["pos"] < target_index
                and not (e["at"] == 0 and e["status"] == "STOPPED_AT")]
  queued = sum(1 for e in entries
               if e["at"] == 0 and e["status"] == "STOPPED_AT"
               and e["direction"] == direction_id
               and e["age"] <= MAX_POSITION_AGE)
  return {
      "in_transit": 1 if in_transit else 0,
      "incoming": 1 if any(e["status"] == "INCOMING_AT" for e in in_transit) else 0,
      "queued": min(queued, QUEUE_CAP),
  }


def leave_one_day_out(rows, key_fn):
  # Fit a median-per-bucket lookup table on every day but one, score
  # it on the held-out day, repeat.  Falls back to the headway
  # estimate for buckets too rare to fit.
  days = sorted({r["day"] for r in rows})
  predictions = []
  for held in days:
    table = defaultdict(list)
    for r in rows:
      if r["day"] != held:
        table[key_fn(r)].append(r["truth"])
    medians = {k: statistics.median(v)
               for k, v in table.items() if len(v) >= MIN_BUCKET_SAMPLES}
    for r in rows:
      if r["day"] == held:
        predictions.append((medians.get(key_fn(r), r["headway"]), r["truth"]))
  return predictions


def describe(name, predictions):
  errors = sorted(abs(p - truth) for p, truth in predictions)
  urgent = [(p, truth) for p, truth in predictions if p <= 3 * 60]
  false_urgency = (100.0 * sum(1 for _, truth in urgent if truth >= 6 * 60)
                   / len(urgent)) if urgent else 0.0
  soon = sorted(p for p, truth in predictions if 3 * 60 <= truth <= 5 * 60)
  shown = ("p25=%.0f med=%.0f p75=%.0f" % (
      soon[len(soon) // 4] / 60, statistics.median(soon) / 60,
      soon[3 * len(soon) // 4] / 60)) if soon else "-"
  print("%-22s %6.1fm %8.1fm %6.1fm %11.1f%%  %s" % (
      name, statistics.mean(errors) / 60, statistics.median(errors) / 60,
      errors[int(0.9 * len(errors))] / 60, false_urgency, shown))


def main():
  if len(sys.argv) != 4:
    print(__doc__ or "usage: analyze_terminus_gap.py <route> <direction> <stop>",
          file=sys.stderr)
    return 1
  route, direction_id, target_parent = sys.argv[1], int(sys.argv[2]), sys.argv[3]

  meta = latest_meta(route)
  if not meta:
    print("no metadata logged for %s" % route, file=sys.stderr)
    return 1
  order = [s["id"] for s in meta["stops_by_direction"][str(direction_id)]["data"]]
  if target_parent not in order:
    print("%s is not on %s direction %d; stops are:\n  %s" % (
        target_parent, route, direction_id, ", ".join(order)), file=sys.stderr)
    return 1
  target_index = order.index(target_parent)

  parents = platform_parents(order)
  target_stop = target_platform(route, direction_id, target_parent, parents)
  if not target_stop:
    print("no predictions ever logged for %s in direction %d" % (
        target_parent, direction_id), file=sys.stderr)
    return 1

  timeline, arrivals = build_timeline(
      route, direction_id, order, target_index, parents)
  live = live_predictions(route, direction_id, target_stop)

  print("%s direction %d -> %s (platform %s), %d stops after the terminus (%s)"
        % (route, direction_id, target_parent, target_stop, target_index,
           order[0]))
  print("%d arrivals observed, %d polls logged" % (len(arrivals), len(timeline)))

  rows = []
  for t in sorted(timeline):
    if t not in live:
      continue
    nxt = next_arrival_after(arrivals, t)
    if nxt is None:
      continue
    truth = (nxt - t).total_seconds()
    if not 0 < truth <= MAX_PLAUSIBLE_WAIT:
      continue
    headway = headway_estimate(arrivals, t)
    if headway is None:
      continue
    row = {"day": t.date().isoformat(), "truth": truth, "headway": headway,
           "live": live[t]}
    row.update(vehicle_features(timeline[t], direction_id, target_index))
    rows.append(row)

  have_live = sum(1 for r in rows if r["live"] is not None)
  print("\nthe MBTA is publishing a prediction here in %.1f%% of polls" % (
      100.0 * have_live / len(rows)))
  for limit in (3, 6):
    sub = [r for r in rows if r["truth"] <= limit * 60]
    silent = sum(1 for r in sub if r["live"] is None)
    print("  when a train is really %d min or less away: silent %.1f%% of the time"
          % (limit, 100.0 * silent / len(sub)))

  print("\nwhat a train currently in view is worth knowing about:")
  table = defaultdict(list)
  for r in rows:
    table[(r["in_transit"], r["incoming"], r["queued"])].append(r["truth"])
  print("  %-42s %6s %8s %7s %7s" % (
      "left terminus / about to arrive / at terminus", "n", "median", "p25", "p75"))
  for key in sorted(table):
    waits = sorted(table[key])
    if len(waits) < MIN_BUCKET_SAMPLES:
      continue
    print("  in_transit=%d incoming=%d queued=%-16d %6d %7.1fm %6.1fm %6.1fm" % (
        key[0], key[1], key[2], len(waits), statistics.median(waits) / 60,
        waits[len(waits) // 4] / 60, waits[3 * len(waits) // 4] / 60))

  print("\nestimator accuracy (leave-one-day-out):")
  print("%-22s %7s %9s %7s %12s  %s" % (
      "", "MAE", "medianAE", "p90AE", "false urgency", "shown when truly 3-5min"))
  describe("live MBTA (when any)",
           [(r["live"], r["truth"]) for r in rows if r["live"] is not None])
  describe("headway only", [(r["headway"], r["truth"]) for r in rows])
  describe("vehicles only", leave_one_day_out(
      rows, lambda r: (r["in_transit"], r["incoming"], r["queued"])))
  describe("vehicles + headway", leave_one_day_out(
      rows, lambda r: (r["in_transit"], r["incoming"], r["queued"],
                       min(int(r["headway"] // HEADWAY_BUCKET), 8))))
  return 0


if __name__ == "__main__":
  sys.exit(main())
