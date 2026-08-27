# The code behind jefftk.com/nextbus/
#
# A thin wrapper around the MBTA's V3 API (https://api-v3.mbta.com/).

import datetime
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import zoneinfo
from collections import defaultdict

# uwsgi loads this by file path with the working directory set to /, so
# the directory holding it isn't on sys.path and a plain sibling import
# fails at startup -- taking down the whole app, not just the feature.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from terminus_tables import TABLES as TERMINUS_TABLES

MBTA_API_BASE = "https://api-v3.mbta.com"
MBTA_API_KEY = os.environ.get("MBTA_API_KEY")

ERROR_TITLE = "MBTA Error"

# Very small TTL cache for the slow-changing lookups (routes, stop
# lists).  The MBTA API allows only 20 requests/minute without an API
# key, and a single page view (e.g. clicking "next") can otherwise cost
# several requests, so this keeps repeat browsing of the same route
# cheap.  Live data (predictions, vehicles) is never cached.
_cache = {}  # (endpoint, params tuple) -> (expires_at, value)
CACHE_TTL_SECONDS = 30


def escape(s):
  s = str(s)
  for f, r in [["&", "&amp;"],
               ["<", "&lt;"],
               [">", "&gt;"]]:
    s = s.replace(f, r)
  return s


def natural_sort_in_place(l):
  # from http://blog.codinghorror.com/sorting-for-humans-natural-sort-order/
  #
  # Changed key -> str(key) because original is intended for a list of
  # strings and we're using this on a list of lists of strings.  This
  # isn't technically correct, since we're no longer lexically
  # sorting, but it should work fine on the data we have.
  convert = lambda text: int(text) if text.isdigit() else text
  alphanum_key = lambda key: [convert(c) for c in re.split('([0-9]+)', str(key))]
  l.sort(key=alphanum_key)


def to_time(seconds):
  if seconds > 60:
    return "%smin %ssec" % (seconds // 60, seconds % 60)
  return "%ssec" % seconds


def mbta_get(endpoint, params, timeout=5, cache=False, ttl=None):
  cache_key = (endpoint, tuple(sorted(params.items())))
  if cache:
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.time():
      return cached[1]

  query = urllib.parse.urlencode(params)
  url = "%s%s?%s" % (MBTA_API_BASE, endpoint, query)
  headers = {
      "User-Agent": "nextbus viewer bot by Jeff Kaufman (www.jefftk.com)",
      "Accept": "application/vnd.api+json",
  }
  if MBTA_API_KEY:
    headers["x-api-key"] = MBTA_API_KEY

  request = urllib.request.Request(url, headers=headers)
  with urllib.request.urlopen(request, timeout=timeout) as response:
    value = json.loads(response.read().decode("utf8"))

  if cache:
    _cache[cache_key] = (time.time() + (ttl or CACHE_TTL_SECONDS), value)
  return value


def jsonapi_include_index(document):
  index = {}
  for resource in document.get("included", []):
    index[(resource["type"], resource["id"])] = resource
  return index


def route_display_title(route_attrs, route_id):
  return route_attrs.get("short_name") or route_attrs.get("long_name") or route_id


# --- Estimates where the MBTA publishes no predictions ---------------
#
# The MBTA doesn't publish predictions for dispatcher-ADDED trips, so
# at the first stop after some Green Line termini a train can be four
# minutes out with nothing shown at all.  Since 2026-08-20 scheduled
# trips are predicted normally, which leaves this biting mainly on
# weekends and during disruptions, when most trips are ADDED.
#
# Where that happens we fall back to what the vehicle feed shows --
# whether a train has already left the terminus, and how many are
# sitting at it -- and report a *range* rather than a single time.
# Someone walking to the stop wants to know whether it's worth
# hurrying, and a spuriously precise "4 minutes" answers a question we
# can't actually answer.  The tables are fitted and calibrated by
# fit_terminus_tables.py; see TERMINUS_ANALYSIS.md.

EASTERN = zoneinfo.ZoneInfo("America/New_York")

# Vehicle positions go stale -- the median is fresh but the tail is
# long -- so a train whose position last updated half an hour ago
# shouldn't count towards the terminus queue.
MAX_POSITION_AGE_SECONDS = 5 * 60
QUEUE_CAP = 3

# Stop ordering effectively never changes, so cache it for much longer
# than the default: the MBTA allows only 20 requests/minute without an
# API key and this is an extra call on top of the predictions fetch.
STOP_ORDER_TTL_SECONDS = 60 * 60


def service_period(now):
  # Must stay identical to period() in fit_terminus_tables.py.
  local = now.astimezone(EASTERN)
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


def stop_order(route, direction_id):
  # (parent station ids in travel order, platform stop id -> parent id,
  # parent stop id -> name).  Vehicles report the platform they're at,
  # but the ordering is in terms of parent stations.
  doc = mbta_get("/stops", {
      "filter[route]": route,
      "filter[direction_id]": direction_id,
      "include": "child_stops",
  }, cache=True, ttl=STOP_ORDER_TTL_SECONDS)
  order = [stop["id"] for stop in doc["data"]]
  parents = {}
  names = {}
  for stop in doc["data"]:
    parents[stop["id"]] = stop["id"]
    names[stop["id"]] = stop["attributes"].get("name") or stop["id"]
    for child in stop["relationships"].get("child_stops", {}).get("data", []):
      parents[child["id"]] = stop["id"]
  return order, parents, names


def terminus_vehicle_state(route, direction_id, target_index, order, parents,
                           now):
  # What the vehicle feed says about trains that could plausibly turn
  # up next: whether one has left the terminus and not yet reached us,
  # whether it's about to arrive, and how many are parked at the
  # terminus waiting to go.
  doc = mbta_get("/vehicles", {"filter[route]": route}, timeout=3)
  index = {stop_id: i for i, stop_id in enumerate(order)}
  in_transit = incoming = False
  queued = 0
  for vehicle in doc["data"]:
    attrs = vehicle["attributes"]
    if attrs.get("direction_id") != direction_id:
      continue
    stop_data = vehicle["relationships"].get("stop", {}).get("data")
    if not stop_data:
      continue
    parent = parents.get(stop_data["id"])
    if parent not in index:
      continue
    at = index[parent]
    status = attrs.get("current_status")
    if at == 0 and status == "STOPPED_AT":
      updated_at = attrs.get("updated_at")
      if not updated_at:
        continue
      age = (now - datetime.datetime.fromisoformat(updated_at)).total_seconds()
      if age <= MAX_POSITION_AGE_SECONDS:
        queued += 1
      continue
    # INCOMING_AT/IN_TRANSIT_TO mean it hasn't reached that stop yet,
    # so the last stop it actually passed is the one before.
    position = at if status == "STOPPED_AT" else at - 1
    if 0 <= position < target_index:
      in_transit = True
      if status == "INCOMING_AT":
        incoming = True
  return int(in_transit), int(incoming), min(queued, QUEUE_CAP)


def terminus_estimate(route, direction_id, stop, now):
  # (low, high, why) -- seconds until the next train at the 5th and
  # 95th percentile, high None meaning "further out than we measured",
  # and a plain-language reason.  None if we can't say anything.
  entry = TERMINUS_TABLES.get((route, direction_id, stop))
  if not entry:
    return None
  order, parents, names = stop_order(route, direction_id)
  if stop not in order:
    return None
  target_index = order.index(stop)
  if target_index == 0:
    return None
  in_transit, incoming, queued = terminus_vehicle_state(
      route, direction_id, target_index, order, parents, now)
  table = entry["table"]
  # Most specific bucket first, backing off to coarser ones rather
  # than reading a tail off a handful of samples.
  found = None
  for key in ((in_transit, incoming, queued, service_period(now)),
              (in_transit, incoming, queued),
              (in_transit, incoming)):
    if key in table:
      found = table[key]
      break
  if not found:
    return None

  terminus = escape(names.get(entry["terminus"], "the terminus"))
  if incoming:
    why = "a train is arriving"
  elif in_transit:
    why = "a train has left %s" % terminus
  elif queued == 1:
    why = "a train is waiting at %s" % terminus
  elif queued:
    why = "%d trains are waiting at %s" % (queued, terminus)
  else:
    why = "no train has left %s yet" % terminus
  return found[0], found[1], why


def waiting_at_terminus(route, direction_id, stop, vehicle, cache):
  # The name of the terminus a predicted train is still sitting at, or
  # None if it has left (or if this stop isn't just past a terminus).
  #
  # Since 2026-08-20 the MBTA predicts trains that haven't departed
  # yet, which is a big improvement on the old silence, but a train
  # being held at the terminus keeps a pinned "1 minute" for as long
  # as it sits there -- observed once for 33 minutes straight at Ball
  # Square.  "1 minute" means something quite different depending on
  # whether the train has actually started moving, so say which.
  key = (route, direction_id, stop)
  if key not in cache:
    cache[key] = None
    entry = TERMINUS_TABLES.get(key)
    if entry:
      try:
        _, parents, names = stop_order(route, direction_id)
      except Exception:
        parents = None  # never break the page over a nicety
      if parents:
        cache[key] = (entry["terminus"], parents,
                      names.get(entry["terminus"], "the terminus"))
  found = cache[key]
  if not found:
    return None
  terminus, parents, name = found

  if vehicle["attributes"].get("current_status") != "STOPPED_AT":
    return None
  stop_data = vehicle["relationships"].get("stop", {}).get("data")
  if not stop_data or parents.get(stop_data["id"]) != terminus:
    return None
  return name


def format_terminus_estimate(estimate):
  low, high, why = estimate
  # Round outwards: understating how soon a train might come is the
  # error that makes someone miss one.
  low_minutes = int(low // 60)
  if high is None:
    headline = "%d+ minutes, possibly much longer" % low_minutes
  elif low_minutes == 0:
    headline = "within %d minutes" % int(-(-high // 60))
  else:
    headline = "%d&ndash;%d minutes" % (low_minutes, int(-(-high // 60)))
  return ("<div class=prediction>No MBTA predictions. "
          "Next train probably <b>%s</b>.</div>"
          "<div class=estimate>Estimated because %s &mdash; not an MBTA "
          "prediction. The wait falls outside this range about "
          "1 time in 10.</div>" % (headline, why))


def nextbus_stop(agency, route, stop):
  title, content = nextbus_stop_helper(route, stop)
  return render_page(
      title=title,
      escaped_content=content,
      include_time=True,
      include_refresh=True,
      include_arrows=True)


def nextbus_stop_helper(route, stop, path_adjust=""):
  try:
    doc = mbta_get("/predictions", {
        "filter[stop]": stop,
        "include": "vehicle,route,stop",
    })
  except Exception:
    return (ERROR_TITLE,
            ["Couldn't reach predictions server.  Try refreshing?"])

  idx = jsonapi_include_index(doc)

  # Predictions for a parent station (e.g. a subway station) are
  # actually tied to its child platform stops, so their ids won't
  # match `stop`; any of them has the name we want, though.
  stop_title = stop
  for (rtype, rid), resource in idx.items():
    if rtype == "stop":
      stop_title = resource["attributes"]["name"]
      break

  now = datetime.datetime.now(datetime.timezone.utc)

  # (route_id, direction_id) -> [(seconds, prediction_html), ...]
  # The API doesn't reliably sort predictions that only have a
  # departure_time (e.g. the start of a trip), so we sort ourselves
  # below.
  buckets = defaultdict(list)

  # Per-request memo for waiting_at_terminus, whose first lookup for a
  # given direction costs an API call.
  terminus_cache = {}

  for prediction in doc["data"]:
    attrs = prediction["attributes"]
    rel = prediction["relationships"]

    route_data = rel.get("route", {}).get("data")
    if not route_data:
      continue
    route_id = route_data["id"]
    direction_id = attrs.get("direction_id")

    time_str = attrs.get("arrival_time") or attrs.get("departure_time")
    if time_str is None:
      continue
    when = datetime.datetime.fromisoformat(time_str)
    seconds = max(0, int((when - now).total_seconds()))

    if seconds < 30:
      time_display = "Due"
    else:
      minutes = round(seconds / 60.0)
      time_display = "%d minute%s" % (minutes, "" if minutes == 1 else "s")

    extras = []
    if attrs.get("status"):
      extras.append(escape(attrs["status"]))

    vehicle_data = rel.get("vehicle", {}).get("data")
    if vehicle_data:
      vehicle = idx.get(("vehicle", vehicle_data["id"]))
      if vehicle:
        held_at = waiting_at_terminus(
            route_id, direction_id, stop, vehicle, terminus_cache)
        if held_at:
          extras.append("still at %s" % escape(held_at))

        vid = vehicle["id"]
        label = vehicle["attributes"].get("label") or vid
        age_str = ""
        updated_at = vehicle["attributes"].get("updated_at")
        if updated_at:
          updated = datetime.datetime.fromisoformat(updated_at)
          age = int((now - updated).total_seconds())
          if age >= 0:
            age_str = ", %s" % to_time(age)
        extras.append("vehicle <a href='%s../../%s/%s/vehicle/%s'>%s</a>%s" % (
            path_adjust, route_id, stop, vid, escape(label), age_str))

    if extras:
      prediction_html = "<div class=prediction>%s (%s)</div>" % (
          time_display, ", ".join(extras))
    else:
      prediction_html = "<div class=prediction>%s</div>" % time_display

    buckets[route_id, direction_id].append((seconds, prediction_html))

  route_titles = {}  # route_id -> title
  direction_names = {}  # route_id -> [name0, name1]
  for (rtype, rid), resource in idx.items():
    if rtype == "route":
      route_titles[rid] = route_display_title(resource["attributes"], rid)
      direction_names[rid] = resource["attributes"].get("direction_names") or []

  if route not in route_titles:
    # The requested route has no predictions for this stop at all, but
    # we still want to show it (empty). We don't have its title from
    # the predictions response since it's not referenced anywhere in
    # it, so fetch it directly.
    try:
      route_doc = mbta_get("/routes/%s" % route, {}, cache=True)
      route_attrs = route_doc["data"]["attributes"]
      route_titles[route] = route_display_title(route_attrs, route)
      direction_names[route] = route_attrs.get("direction_names") or []
    except Exception:
      route_titles[route] = route

  # A direction the MBTA says nothing about renders no section at all,
  # which is worst exactly where it's least helpful: one stop past a
  # Green Line terminus, where a train can be minutes away with
  # nothing published.  At the few stops where that silence is both
  # common and predictable, estimate from vehicle positions instead.
  estimates = {}  # (route_id, direction_id) -> html
  for direction_id in (0, 1):
    if (route, direction_id) in buckets:
      continue
    if (route, direction_id, stop) not in TERMINUS_TABLES:
      continue
    try:
      estimate = terminus_estimate(route, direction_id, stop, now)
    except Exception:
      continue  # a fallback estimate must never break the page
    if estimate:
      buckets[route, direction_id] = []
      estimates[route, direction_id] = format_terminus_estimate(estimate)

  if not any(route_id == route for route_id, _ in buckets):
    buckets[route, None] = []

  sections = []  # (sort_title, route_id, html)
  for (route_id, direction_id), predictions in buckets.items():
    title = route_titles.get(route_id, route_id)
    names = direction_names.get(route_id, [])
    if direction_id is not None and direction_id < len(names) and names[direction_id]:
      header = "<h2>%s: %s</h2>" % (escape(title), escape(names[direction_id]))
    else:
      header = "<h2>%s</h2>" % escape(title)
    if predictions:
      predictions.sort(key=lambda p: p[0])
      body = "\n".join(html for _, html in predictions)
    elif (route_id, direction_id) in estimates:
      body = estimates[route_id, direction_id]
    else:
      body = "<div class=prediction>No predictions.</div>"
    sections.append((title, route_id, header + "\n" + body))

  natural_sort_in_place(sections)

  escaped_content = []
  # sort the route requested before others
  for _, route_id, section_html in sections:
    if route_id == route:
      escaped_content.append(section_html)
  for _, route_id, section_html in sections:
    if route_id != route:
      escaped_content.append(section_html)

  return escape(stop_title), escaped_content


def nextbus_route_helper(route):
  route_doc = mbta_get("/routes/%s" % route, {}, cache=True)
  route_attrs = route_doc["data"]["attributes"]
  route_title = route_display_title(route_attrs, route)
  direction_names = route_attrs.get("direction_names") or []
  direction_destinations = route_attrs.get("direction_destinations") or []

  r = []  # [[[direction_id, direction_title], [[stop_id, stop_name, lat, lon, stop_id], ...]], ...]
  for direction_id in range(len(direction_names)):
    stops_doc = mbta_get("/stops", {
        "filter[route]": route,
        "filter[direction_id]": direction_id,
    }, cache=True)

    direction_stops = []
    for stop in stops_doc["data"]:
      attrs = stop["attributes"]
      if not attrs.get("name"):
        continue
      direction_stops.append([
          stop["id"], escape(attrs["name"]),
          attrs.get("latitude"), attrs.get("longitude"), stop["id"]])

    if not direction_stops:
      continue

    name = direction_names[direction_id] or ""
    destination = (direction_destinations[direction_id]
                   if direction_id < len(direction_destinations) else None)
    direction_title = "%s to %s" % (name, destination) if destination else name

    r.append([[str(direction_id), escape(direction_title)], direction_stops])

  return route_title, r


def html_redirect(dest):
  return "<meta http-equiv='refresh' content='0;URL=%s'>" % dest


def vehicles_for_route(route):
  doc = mbta_get("/vehicles", {"filter[route]": route})
  vehicles = {}  # id -> [route_id, direction_id, lat, lon, age, heading]
  now = datetime.datetime.now(datetime.timezone.utc)
  for vehicle in doc["data"]:
    attrs = vehicle["attributes"]
    updated_at = attrs.get("updated_at")
    age = 0
    if updated_at:
      age = max(0, int((now - datetime.datetime.fromisoformat(updated_at)).total_seconds()))
    bearing = attrs.get("bearing")
    route_data = vehicle["relationships"].get("route", {}).get("data")
    vehicles[vehicle["id"]] = [
        route_data["id"] if route_data else None,
        attrs.get("direction_id"),
        attrs.get("latitude"),
        attrs.get("longitude"),
        age,
        bearing if bearing is not None else -1,
    ]
  return vehicles


def nextbus_stop_vehicle(agency, route, stop, vehicleid):
  # We need to draw:
  # * the route
  # * the bus in question, if found (display message if not found)
  # * the other buses currently serving the route
  # * the stop in question

  route_title, stop_info = nextbus_route_helper(route)

  polylines = []
  desired_stop_loc = None

  viewport = [None, None, None, None]  # minlat, minlon, maxlat, maxlon
  def seen(lat, lon):
    minlat, minlon, maxlat, maxlon = viewport
    if lat is None or lon is None:
      return
    if not minlat or lat < minlat:
      viewport[0] = lat
    if not minlon or lon < minlon:
      viewport[1] = lon
    if not maxlat or lat > maxlat:
      viewport[2] = lat
    if not maxlon or lon > maxlon:
      viewport[3] = lon

  messages = []

  for [direction_id, direction_title], stops in stop_info:
    polyline = []
    for stop_id, _, lat, lon, _ in stops:
      seen(lat, lon)
      if stop_id == stop:
        desired_stop_loc = [lat, lon]
      polyline.append((lat, lon))
    polylines.append(polyline)

  if not desired_stop_loc:
    messages.append("Stop %s doesn't appear to be on the %s route." % (
        escape(stop), escape(route)))

  other_vehicles = vehicles_for_route(route)
  other_vehicle_locs = []
  for vid, (v_route, v_direction, lat, lon, age, heading) in other_vehicles.items():
    if vid == vehicleid:
      continue
    seen(lat, lon)
    other_vehicle_locs.append([lat, lon, vid, heading])

  desired_vehicle_loc = None
  desired_vehicle_current_route = None
  try:
    vehicle_doc = mbta_get("/vehicles/%s" % vehicleid, {})
    vattrs = vehicle_doc["data"]["attributes"]
    lat, lon = vattrs.get("latitude"), vattrs.get("longitude")
    seen(lat, lon)
    bearing = vattrs.get("bearing")
    desired_vehicle_loc = [lat, lon, vehicleid, bearing if bearing is not None else -1]
    route_data = vehicle_doc["data"]["relationships"].get("route", {}).get("data")
    if route_data and route_data["id"] != route:
      desired_vehicle_current_route = route_data["id"]
  except Exception:
    messages.append("Vehicle %s isn't reporting a location." % escape(vehicleid))

  escaped_content = []
  for message in messages:
    escaped_content.append("<div>%s</div>" % message)

  escaped_content.extend([
      "<script>",
      "desired_stop_loc=%s;" % json.dumps(desired_stop_loc),
      "desired_vehicle_loc=%s;" % json.dumps(desired_vehicle_loc),
      "other_vehicle_locs=%s;" % json.dumps(other_vehicle_locs),
      "desired_vehicle_current_route=%s;" % json.dumps(
          desired_vehicle_current_route),
      "polylines=%s;" % json.dumps(polylines),
      "viewport=%s;" % json.dumps(viewport),
      "</script>"])
  escaped_content.append("""
<center><svg id=svg
     viewBox="0 0 1 1"
     xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink">
</svg></center>

<script>
svg = document.getElementById("svg");

window.onload = function() {
  for (var i = 0 ; i < polylines.length ; i++) {
    draw_polyline(polylines[i]);
  }
  for (var i = 0; i < other_vehicle_locs.length; i++) {
    draw_bus(other_vehicle_locs[i], "lightblue", null);
  }
  var extra = null;
  if (desired_vehicle_current_route != null) {
    extra = "(" + desired_vehicle_current_route + ")";
  }
  draw_bus(desired_vehicle_loc, "red", extra);
  draw_circle(desired_stop_loc, "lightgreen");
};

function distance(lat1, lon1, lat2, lon2) {
  // Approximate a flat Earth at the average latitude (of lat1, lat2).
  var dlat = lat1 - lat2;
  var dlon = (lon1 - lon2) * Math.cos(0.5*(lat1 + lat2) * Math.PI/180.);
  return Math.sqrt(dlat*dlat + dlon*dlon);
}

function draw_bus(bus, color, extra) {
  if (bus === null) {
    return;
  }
  lat = bus[0];
  lon = bus[1];
  vid = bus[2];
  heading = bus[3];

  var screen_coord = to_screen([lat, lon]);

  if (heading >= 0) {
    draw_screen_triangle(screen_coord[0], screen_coord[1], heading, color);
  } else {
    draw_screen_circle(screen_coord[0], screen_coord[1], color, 0.03);
  }
  draw_screen_text(screen_coord[0], screen_coord[1], vid, 0);
  if (extra != null) {
    draw_screen_text(screen_coord[0], screen_coord[1], extra, -1);
  }
}

function draw_circle(pos, color) {
  if (pos === null) {
    return;
  }
  var screen_coord = to_screen(pos);
  draw_screen_circle(screen_coord[0], screen_coord[1], color, 0.01);
}

function draw_screen_triangle(x, y, heading, color) {
  triangle = document.createElementNS(svg.namespaceURI, "polygon");
  triangle.setAttribute("transform", "rotate(" + heading + ",  " + x + ", " + y + ")");
  triangle.setAttribute("points",
                        x + "," + (y-0.045) + " " +
                        (x-0.025) + "," + (y+0.025) + " " +
                        (x+0.025) + "," + (y+0.025));
  triangle.setAttribute("fill", color);
  triangle.setAttribute("stroke", "black");
  triangle.setAttribute("stroke-width", .0005);
  svg.appendChild(triangle);
}
  function draw_screen_text(x, y, s, adj) {
  text = document.createElementNS(svg.namespaceURI, "text");
  text.setAttribute("x", x);
  text.setAttribute("y", y);
  text.setAttribute("font-size", .02);
  text.setAttribute("text-anchor", "middle");
  adj = adj - 0.4;
  text.setAttribute("baseline-shift", adj + "em");
  text.innerHTML = s;
  svg.appendChild(text);
}
  function draw_screen_circle(x, y, color, size) {
  circle = document.createElementNS(svg.namespaceURI, "circle");
  circle.setAttribute("cx", x);
  circle.setAttribute("cy", y);
  circle.setAttribute("r", size);
  circle.setAttribute("stroke", "black");
  circle.setAttribute("stroke-width", .0005);
  circle.setAttribute("fill", color);
  svg.appendChild(circle);
}
function draw_polyline(points) {
  line = document.createElementNS(svg.namespaceURI, "polyline");
  points_str = ""
  for (var i = 0; i < points.length; i++) {
    var screen_coord = to_screen(points[i]);
    if (i > 0) {
      points_str += ", ";
    }
    points_str += screen_coord[0] + " " + screen_coord[1];
  }

  line.setAttribute("points", points_str);
  line.setAttribute("stroke", "black");
  line.setAttribute("stroke-width", "0.001");
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("fill", "none");
  svg.appendChild(line);
}

center_lat = (viewport[0] + viewport[2])/2;
center_lon = (viewport[1] + viewport[3])/2;

lat_lon_ratio = (distance(center_lat + 0.01, center_lon, center_lat - 0.01, center_lon) /
                 distance(center_lat, center_lon + 0.01, center_lat, center_lon - 0.01));

function fix_ratio(pos) {
  lat = pos[0];
  lon = pos[1];

  r_x = lon * lat_lon_ratio;
  r_y = -lat;

  return [r_x , r_y];
}

r_a = fix_ratio([viewport[0], viewport[1]]);
r_b = fix_ratio([viewport[2], viewport[3]]);

r_x_min = Math.min(r_a[0], r_b[0]) - 0.0015;
r_y_min = Math.min(r_a[1], r_b[1]) - 0.0015;
r_x_max = Math.max(r_a[0], r_b[0]) + 0.0015;
r_y_max = Math.max(r_a[1], r_b[1]) + 0.0015;

delta_rx = r_x_max - r_x_min;
delta_ry = r_y_max - r_y_min;

r_scale = Math.max(delta_rx, delta_ry);

function to_screen(pos) {
  ratio_fixed = fix_ratio(pos);
  r_x = ratio_fixed[0];
  r_y = ratio_fixed[1];

  s_x = (r_x - r_x_min)/r_scale;
  s_y = (r_y - r_y_min)/r_scale;

  return [s_x, s_y];
}

vp_a = to_screen([viewport[0], viewport[1]]);
vp_b = to_screen([viewport[2], viewport[3]]);

max_screen_x = Math.max(vp_a[0], vp_b[0]);
max_screen_y = Math.max(vp_a[1], vp_b[1]);

desired_height = svg.offsetWidth / max_screen_x * max_screen_y;
margin_adjustment = -(svg.offsetHeight - desired_height);
svg.style.marginBottom = margin_adjustment + "px";
</script>
""")

  stop_title, stop_content = nextbus_stop_helper(
      route, stop, path_adjust="../../")
  if stop_title != ERROR_TITLE:
    escaped_content.extend(stop_content)
  else:
    escaped_content.append("<div>%s</div>" % "".join(stop_content))

  escaped_content.append("""
<div>
<br><small><i>Key: Chosen bus is red, other buses are blue, stop
is green.  Circular buses are ones whose heading isn't currently known.</i></small>
</div>
  """)

  return render_page(
      title="%s Map (Vehicle %s)" % (escape(route), escape(vehicleid)),
      uploc="../../",
      include_refresh=True,
      escaped_content=escaped_content)


def nextbus_stop_relative(agency, route, stop, relative):
  if relative not in ["next", "previous"]:
    return "Not understood."

  route_title, stop_info = nextbus_route_helper(route)

  options = defaultdict(list)  # [stop_id, stop_name] -> [direction, ...]

  for [direction_id, direction_title], stops in stop_info:
    previous_stop_id = None
    previous_stop_name = None
    for stop_id, stop_name, _, _, _ in stops:
      if relative == "next" and previous_stop_id == stop:
        options[stop_id, stop_name].append(direction_title)
      elif relative == "previous" and stop_id == stop:
        options[previous_stop_id, previous_stop_name].append(direction_title)

      previous_stop_id = stop_id
      previous_stop_name = stop_name

  if not options:
    return html_redirect("../")

  if len(options) == 1:
    (stop_id, stop_name), = options.keys()

    if stop_id is None:
      return html_redirect("../")
    else:
      return html_redirect("../../%s/" % stop_id)

  escaped_content = []
  for (stop_id, stop_name), directions in options.items():
    for direction in directions:
      escaped_content.append("<span class=row><a href='../../%s/'>%s</a> (%s)</span>" % (stop_id, stop_name, direction))

  return render_page(
      title="Multiple options for the %s stop" % relative,
      escaped_content=escaped_content)


def nextbus_route(agency, route):
  escaped_content = []

  route_title, stop_info = nextbus_route_helper(route)

  for (direction_id, direction_title), stops in stop_info:
    escaped_content.append("<h2>%s</h2>" % direction_title)
    for stop_id, stop_name, lat, lon, _ in stops:
      escaped_content.append(
          '<a class=row href="%s/">%s</a>' % (stop_id, stop_name))

  return render_page(
      title="%s Stops" % route_title,
      escaped_content=escaped_content)


def nextbus_agency(agency):
  doc = mbta_get("/routes", {}, cache=True)
  routes = []
  for route in doc["data"]:
    attrs = route["attributes"]
    if not attrs.get("listed_route", True):
      continue
    routes.append((route_display_title(attrs, route["id"]), route["id"]))
  natural_sort_in_place(routes)

  return render_page(
      title="MBTA Routes",
      escaped_content=[
          '<a class=row href="%s/">%s</a>' % (escape(tag), escape(title))
          for (title, tag) in routes])


def render_page(title,
                escaped_content,
                uploc="../",
                include_up=True,
                include_time=False,
                include_arrows=False,
                include_refresh=False):
    time = "\n".join([
        "&nbsp;&nbsp;",
        "<script>",
        "var currentdate = new Date();",
        "var hours = currentdate.getHours();",
        "hours = (hours < 13) ? hours : hours - 12;",
        "var minutes = currentdate.getMinutes();",
        "minutes = (minutes < 10) ? ('0' + minutes) : minutes;",
        "document.write(hours + ':' + minutes);",
        "</script>"])

    header_row = "\n".join([
      "<table border=0 style='width:100%'><tr>",
      '<td align=center valign=center class="gray"'
      '    style="padding: 13px 0px">',
      '<h1>%s</h1>' % escape(title),
      time if include_time else "",

      '<td align=center valign=center class=gray width="60px"'
      '    style="font-size: 150%">'
      '<a class=button href=#'
      '   onclick="window.location.reload(true); return false;"'
      '>&#8635;</a>' if include_refresh else "",

      "</table>"])

    up_row = "\n".join([
      "<table border=0 style='width:100%; font-size: 150%'><tr>",

      '<td align=center valign=center class=gray width=12%>'
      '<a class=button href="previous/">&larr;' if include_arrows else "",

      '<td align=center valign=center class=gray>'
      '<a class=button href="%s">&uarr;</a>' % uploc,

      '<td align=center valign=center class=gray width=38%>'
      '<a class=button href=#'
      '   onclick="window.location.reload(true); return false;"'
      '>&#8635;</a>' if include_refresh else "",

      '<td align=center valign=center class=gray width=12%>'
      '<a class=button href="next/">&rarr;</a>' if include_arrows else "",

      "</table>"])

    return "\n".join([
        "<html>",
        "<head>",
        "<style>",
        "body {margin: 0}",
        ".gray {background-color: #DDD}",
        ".sans {font-family: sans-serif}",
        ".container {",
        "  margin: 10px;",
        "  font-size: 16px;",
        "}",
        "h1 {",
        "  font-size: 16px;",
        "  font-family: sans-serif;",
        "  display: inline-block;",
        "  margin: 0;",
        "  padding: 0;",
        "}",
        "h2 {",
        "  min-height: 20px;",
        "  margin: 0;",
        "  margin-bottom: 10px;",
        "  margin-top: 10px;",
        "  font-size: 16px;",
        "  font-family: sans-serif;",
        "}",
        ".button {",
        "  color: black;",
        "  text-decoration: none;",
        "  width: 100%;",
        "  display: inline-block;",
        "  padding-top: 2px;",
        "  padding-bottom: 7px;",
        "}",
        "",
        ".row {",
        "  display: block;",
        "  width: 100%;",
        "  min-height: 20px;",
        "  padding: 0;",
        "  padding-top: 10px;",
        "  padding-bottom: 10px;",
        "  margin: 0",
        "}",
        ".prediction {",
        "  width: 100%;",
        "  margin-bottom: 3px;",
        "}",
        ".estimate {",
        "  width: 100%;",
        "  margin-bottom: 3px;",
        "  font-size: 13px;",
        "  color: #555;",
        "}",
        "#svg {",
        "  width: 90vmin;",
        "  margin: 0;",
        "  padding: 0;",
        "}",
        ".header { padding: 10px }",
        "</style>",
        "<meta name=viewport content='width=device-width, initial-scale=1px'>",
        "<title>%s</title>" % escape(title),
        "</head>",
        "<body>",
        header_row,
        "<div class=container>",
        "\n".join(escaped_content),
        "</div>",
        up_row if include_up else "",
        "<center><a class='row button%s' href='http://www.jefftk.com'>jefftk.com</a></center>" % (" gray" if not include_up else ""),
        "</body>",
        "</html>"])


def nextbus_index():
  # There's only one agency (the MBTA) now, so skip straight there.
  return html_redirect("mbta/")


def nextbus(path):
  if path == "/":
    return nextbus_index()

  r = re.match(r"^/([^/]+)/([^/]+)/([^/]+)/vehicle/([^/]+)/$", path)
  if r:
    return nextbus_stop_vehicle(*r.groups())

  r = re.match(r"^/([^/]+)/([^/]+)/([^/]+)/(next|previous)/$", path)
  if r:
    return nextbus_stop_relative(*r.groups())

  r = re.match(r"^/([^/]+)/([^/]+)/([^/]+)/$", path)
  if r:
    return nextbus_stop(*r.groups())

  r = re.match(r"^/([^/]+)/([^/]+)/$", path)
  if r:
    return nextbus_route(*r.groups())

  r = re.match(r"^/([^/]+)/$", path)
  if r:
    return nextbus_agency(*r.groups())

  return "nextbus: '%s' not understood" % escape(path)


def die500(e):
    trb = "%s: %s\n\n%s" % (e.__class__.__name__, e, traceback.format_exc())
    return trb


# actually respond to the request
# raising errors here will give a 500 and put the traceback in the body
def application(environ, start_response):
    path = environ["PATH_INFO"]
    if path.startswith("/nextbus"):
      try:
        output = nextbus(path[len("/nextbus"):])
        start_response('200 OK', [('content-type', 'text/html')])
      except Exception as e:
        output = die500(e)
        start_response('500 Internal Server Error', [('content-type', 'text/plain')])
    else:
      output = "not understood"
      start_response('404 Not Found', [('content-type', 'text/plain')])

    return (output.encode('utf8'), )


if __name__ == "__main__":
  from wsgiref.simple_server import make_server
  port = int(os.environ.get("PORT", 8000))
  print("Serving on http://127.0.0.1:%d/nextbus/" % port)
  if not MBTA_API_KEY:
    print("Warning: MBTA_API_KEY not set; the MBTA API allows only "
          "20 requests/minute without one. Register at "
          "https://api-v3.mbta.com/register")
  make_server("", port, application).serve_forever()
