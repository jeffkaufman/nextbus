# Does the remaining Ball Square prediction gap track ADDED trips?
#
# Usage: python3 check_added_vs_silence.py <route> <direction_id> <stop> [SINCE]
#
# Daily aggregates suggest the MBTA started predicting these trips
# around Aug 20 but still publishes nothing for dispatcher-ADDED ones.
# That's a per-train claim, so test it per train: for each poll where
# a train really is 2-6 minutes away, was that specific train on an
# ADDED trip, and was the MBTA silent?

import datetime
import sys
from collections import defaultdict

from analyze_terminus_gap import (
    latest_meta, live_predictions, load_records, parse_time, platform_parents,
    target_platform)


def main():
  route, direction_id, target_parent = sys.argv[1], int(sys.argv[2]), sys.argv[3]
  since = sys.argv[4] if len(sys.argv) > 4 else "0000"
  until = sys.argv[5] if len(sys.argv) > 5 else "9999"

  meta = latest_meta(route)
  order = [s["id"] for s in meta["stops_by_direction"][str(direction_id)]["data"]]
  target_index = order.index(target_parent)
  parents = platform_parents(order)
  target_stop = target_platform(route, direction_id, target_parent, parents)
  index = {sid: i for i, sid in enumerate(order)}

  # Arrivals tagged with which vehicle made them, plus each vehicle's
  # trip id as of every poll.
  arrivals = []           # (time, vid)
  trip_at = defaultdict(dict)  # poll time -> vid -> trip id
  furthest = {}
  for rec in load_records(route, "vehicles"):
    t = parse_time(rec["polled_at"])
    if not since <= t.date().isoformat() <= until:
      continue
    for v in rec["response"]["data"]:
      stop = v["relationships"].get("stop", {}).get("data")
      if not stop:
        continue
      parent = parents.get(stop["id"])
      if parent not in index:
        continue
      trip = v["relationships"].get("trip", {}).get("data")
      trip_at[t][v["id"]] = trip["id"] if trip else None
      if v["attributes"].get("direction_id") != direction_id:
        continue
      i = index[parent]
      pos = i if v["attributes"]["current_status"] == "STOPPED_AT" else i - 1
      previous = furthest.get(v["id"])
      if previous is None or pos < previous - 2:
        furthest[v["id"]] = pos
        continue
      if previous < target_index <= pos:
        age = (t - parse_time(v["attributes"]["updated_at"])).total_seconds()
        arrivals.append((t - datetime.timedelta(seconds=age), v["id"]))
      furthest[v["id"]] = max(previous, pos)
  arrivals.sort()

  live = live_predictions(route, direction_id, target_stop)
  times = [a for a, _ in arrivals]
  import bisect
  counts = defaultdict(lambda: [0, 0])  # added? -> [silent, total]
  for t in sorted(trip_at):
    if t not in live:
      continue
    i = bisect.bisect_right(times, t)
    if i >= len(times):
      continue
    wait = (times[i] - t).total_seconds()
    if not 2 * 60 <= wait <= 6 * 60:
      continue
    vid = arrivals[i][1]
    trip = trip_at[t].get(vid)
    # The trip id we want is the one the arriving train is running
    # now; if it isn't in this poll, fall back to the poll where it
    # arrived.
    if trip is None:
      trip = trip_at.get(times[i], {}).get(vid)
    added = bool(trip and trip.startswith("ADDED"))
    entry = counts[added]
    entry[1] += 1
    if live[t] is None:
      entry[0] += 1

  print("%s dir %d %s, polls where a train is truly 2-6 min away%s" % (
      route, direction_id, target_parent,
      (", %s..%s" % (since, until)) if since != "0000" else ""))
  for added in (False, True):
    silent, total = counts[added]
    if total:
      print("  arriving train on %-13s n=%-6d MBTA silent %5.1f%%" % (
          "an ADDED trip" if added else "a scheduled trip", total,
          100.0 * silent / total))


if __name__ == "__main__":
  sys.exit(main())
