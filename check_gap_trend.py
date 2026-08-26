# Is the terminus prediction gap still there?
#
# Usage: python3 check_gap_trend.py <route> <direction_id> <stop>
#
# Availability alone is misleading -- a stop can look "covered"
# because of predictions for trains half an hour out.  The metric that
# decides whether a fallback estimator is needed is how often the MBTA
# is silent *when a train is genuinely about to arrive*.

import sys
from collections import defaultdict

from analyze_terminus_quantiles import build_rows


def main():
  route, direction_id, target_parent = sys.argv[1], int(sys.argv[2]), sys.argv[3]
  rows, _, _, _ = build_rows(route, direction_id, target_parent)
  by_day = defaultdict(list)
  for r in rows:
    by_day[r["day"]].append(r)
  print("%s dir %d %s" % (route, direction_id, target_parent))
  print("%-12s %7s %10s %12s %12s" % (
      "day", "polls", "any pred", "silent@<=6m", "silent@2-6m"))
  for day in sorted(by_day):
    day_rows = by_day[day]
    have = sum(1 for r in day_rows if r["live"] is not None)
    close = [r for r in day_rows if r["truth"] <= 6 * 60]
    mid = [r for r in day_rows if 2 * 60 <= r["truth"] <= 6 * 60]
    def silent(sub):
      if not sub:
        return "-"
      return "%.1f%%" % (100.0 * sum(1 for r in sub if r["live"] is None) / len(sub))
    print("%-12s %7d %9.1f%% %12s %12s" % (
        day, len(day_rows), 100.0 * have / len(day_rows), silent(close), silent(mid)))


if __name__ == "__main__":
  sys.exit(main())
