# Logs raw predictions and vehicle positions for the whole subway and
# light rail system, to a directory of gzipped JSON-lines files.
#
# Purpose: on some routes (e.g. the Green Line branches), the MBTA
# doesn't start publishing predictions for a trip until well after it's
# left its terminus, even though a vehicle is already tracked and
# moving. This logs everything needed to later figure out, per
# route/direction/terminus, how bad that gap is and what a decent
# fallback estimate (schedule-based, headway-based, etc.) would have
# predicted -- without committing up front to which approach is best.
#
# Scope: the rapid transit system (light + heavy rail). Buses use a
# different, GPS-first prediction pipeline that doesn't show the same
# terminus blind spot, and there are ~170 of them, so they're left out
# to keep this cheap and focused.
#
# Output: <route>-<date>-<predictions|vehicles>.jsonl.gz under LOG_DIR,
# one gzip member per poll containing {"polled_at": ..., "response": ...}
# with the raw MBTA API response.

import datetime
import gzip
import json
import os
import shutil
import sys
import time

from nextbus import mbta_get

ROUTES = [
    "Green-B", "Green-C", "Green-D", "Green-E", "Mattapan",
    "Red", "Orange", "Blue",
]

POLL_INTERVAL_SECONDS = 60
MIN_FREE_BYTES = 1 * 1024 ** 3  # stop logging rather than fill the disk

LOG_DIR = os.environ.get(
    "NEXTBUS_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))

ENDPOINTS = [
    ("predictions", "/predictions", "vehicle,trip"),
    ("vehicles", "/vehicles", "trip"),
]


def log_path(route, kind, day):
  return os.path.join(LOG_DIR, "%s-%s-%s.jsonl.gz" % (route, day, kind))


def append_jsonl_gz(path, obj):
  with gzip.open(path, "at") as f:
    f.write(json.dumps(obj) + "\n")


def poll_route(route, day, now):
  for kind, endpoint, include in ENDPOINTS:
    try:
      doc = mbta_get(endpoint, {"filter[route]": route, "include": include},
                      timeout=10)
    except Exception as e:
      print("%s %s %s failed: %s" % (now.isoformat(), route, kind, e),
            file=sys.stderr)
      continue
    append_jsonl_gz(log_path(route, kind, day), {
        "polled_at": now.isoformat(),
        "response": doc,
    })


def log_route_metadata(route, day):
  # Route/stop info rarely changes, but grabbing a snapshot alongside
  # the data makes later analysis self-contained (direction names,
  # stop ordering, terminus identification) without extra API calls.
  try:
    route_doc = mbta_get("/routes/%s" % route, {})
    stops_by_direction = {}
    for direction_id in (0, 1):
      stops_by_direction[direction_id] = mbta_get("/stops", {
          "filter[route]": route,
          "filter[direction_id]": direction_id,
      })
  except Exception as e:
    print("%s metadata for %s failed: %s" % (day, route, e), file=sys.stderr)
    return
  append_jsonl_gz(log_path(route, "meta", day), {
      "route": route_doc,
      "stops_by_direction": stops_by_direction,
  })


def main():
  os.makedirs(LOG_DIR, exist_ok=True)
  print("logging %s to %s every %ss" % (
      ", ".join(ROUTES), LOG_DIR, POLL_INTERVAL_SECONDS))

  last_day = None
  while True:
    now = datetime.datetime.now(datetime.timezone.utc)
    day = now.date().isoformat()

    free = shutil.disk_usage(LOG_DIR).free
    if free < MIN_FREE_BYTES:
      print("%s low disk space (%d bytes free), skipping poll" % (
          now.isoformat(), free), file=sys.stderr)
      time.sleep(POLL_INTERVAL_SECONDS)
      continue

    if day != last_day:
      for route in ROUTES:
        log_route_metadata(route, day)
      last_day = day

    for route in ROUTES:
      poll_route(route, day, now)

    time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
  main()
