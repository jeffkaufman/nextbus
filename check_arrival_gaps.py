# Sanity-checks the arrival detector.  If it silently misses a train,
# "time until the next arrival" jumps to the train after it, which
# inflates precisely the upper tail the p95 is trying to measure.  A
# missed arrival shows up as a gap near twice the local headway, so
# look for a bulge there directly.
#
# Usage: python3 check_arrival_gaps.py <route> <direction_id> <stop>

import statistics
import sys
from collections import Counter

from analyze_terminus_quantiles import build_rows, quantile


def main():
  route, direction_id, target_parent = sys.argv[1], int(sys.argv[2]), sys.argv[3]
  _, arrivals, _, _ = build_rows(route, direction_id, target_parent)
  gaps = sorted((arrivals[i] - arrivals[i - 1]).total_seconds()
                for i in range(1, len(arrivals)))
  day = [g for g in gaps if g <= 30 * 60]
  med = statistics.median(day)
  print("%s dir %d %s: %d arrivals, %d gaps" % (
      route, direction_id, target_parent, len(arrivals), len(gaps)))
  print("  median gap %.1fm, p90 %.1fm, p99 %.1fm" % (
      med / 60, quantile(gaps, 0.9) / 60, quantile(gaps, 0.99) / 60))
  near_double = sum(1 for g in day if 1.7 * med <= g <= 2.3 * med)
  print("  %.1f%% of sub-30min gaps sit near 2x the median (%.1f-%.1fm):"
        " candidate missed arrivals" % (
            100.0 * near_double / len(day), 1.7 * med / 60, 2.3 * med / 60))
  hist = Counter(int(g // 60) for g in day)
  print("  gap histogram (minutes -> count):")
  for m in sorted(hist):
    print("    %2dm %s %d" % (m, "#" * min(60, hist[m] // 10), hist[m]))


if __name__ == "__main__":
  sys.exit(main())
