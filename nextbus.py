# The code behind jefftk.com/nextbus/
#
# A thin wrapper around the MBTA's V3 API (https://api-v3.mbta.com/).

import datetime
import json
import os
import re
import time
import traceback
import urllib.parse
import urllib.request
from collections import defaultdict

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


def mbta_get(endpoint, params, timeout=5, cache=False):
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
    _cache[cache_key] = (time.time() + CACHE_TTL_SECONDS, value)
  return value


def jsonapi_include_index(document):
  index = {}
  for resource in document.get("included", []):
    index[(resource["type"], resource["id"])] = resource
  return index


def route_display_title(route_attrs, route_id):
  return route_attrs.get("short_name") or route_attrs.get("long_name") or route_id


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
      route_titles[route] = route_display_title(route_doc["data"]["attributes"], route)
    except Exception:
      route_titles[route] = route
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
