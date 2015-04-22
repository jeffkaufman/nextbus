# The code behind 
#
#
#
#



import re
from xml.dom import minidom
from collections import defaultdict
import urllib2
import json

def escape(s):
    s = unicode(s)
    for f,r in [["&", "&amp;"],
                ["<", "&lt;"],
                [">", "&gt;"]]:
        s = s.replace(f,r)
    return s

def slurp(url, data=None, headers={}, timeout=60):
    identifier = "Jeff Kaufman"
    botname = "bus predictions"
    headers['User-Agent'] = '%s bot by %s (www.jefftk.com)' % (botname, identifier)
    return urllib2.urlopen(urllib2.Request(url, data, headers), None, timeout).read()


def natural_sort_in_place(l):
  # from http://blog.codinghorror.com/sorting-for-humans-natural-sort-order/
  #
  # Changed key -> str(key) because original is intended for a list of
  # strings and we're using this on a list of lists of strings.  This
  # isn't technically correct, since we're no longer lexically
  # sorting, but it should work fine on the data we have.
  convert = lambda text: int(text) if text.isdigit() else text
  alphanum_key = lambda key: [ convert(c) for c in re.split('([0-9]+)', str(key)) ]
  l.sort(key=alphanum_key)

def nextbus_stop(agency, route, stop):
  title, content = nextbus_stop_helper(agency, route, stop)
  return render_page(
      title=title,
      escaped_content=content,
      include_time=True,
      include_refresh=True,
      include_arrows=True)

def to_time(seconds):
  if seconds > 60:
    return "%smin %ssec" % (seconds / 60, seconds % 60)
  return "%ssec" % seconds

def nextbus_stop_helper(agency, route, stop, path_adjust="", ages={}):
  if stop.isdigit():
      load_as="stopId=%s" % stop
  else:
      load_as="s=%s&r=%s" % (stop, route)

  try:
      xmldoc = minidom.parseString(slurp(
          "http://webservices.nextbus.com/service/publicXMLFeed?"
          "command=predictions&a=%s&%s&useShortNames=true" % (agency, load_as),
          timeout=1))
  except Exception:
      return ("Nextbus Error",
              ["Couldn't reach predictions server.  Try refreshing?"])

  prediction_sections = [] # [route_tag, escaped_html]
  stop_title = None

  no_predictions = "<div class=prediction>No predictions.</div>"

  for predictions in xmldoc.getElementsByTagName("predictions"):
    # prefer the name for this stop used by the route in question
    if stop_title is None or predictions.getAttribute("routeTag") == route:
      stop_title = predictions.getAttribute("stopTitle")

    prediction_section = []

    if predictions.getAttribute("dirTitleBecauseNoPredictions"):
      prediction_section.append("<h2 class>%s: %s</h2>" % (
          predictions.getAttribute("routeTitle"),
          predictions.getAttribute("dirTitleBecauseNoPredictions")))
      prediction_section.append(no_predictions)

    for direction in predictions.getElementsByTagName("direction"):
      prediction_section.append("<h2 class>%s: %s</h2>" % (
          predictions.getAttribute("routeTitle"),
          direction.getAttribute("title")))
      for prediction in direction.getElementsByTagName("prediction"):
        vid = prediction.getAttribute("vehicle")
        prediction_section.append("<div class=prediction>%s minute%s (%s%svehicle %s%s)</div>" % (
            escape(prediction.getAttribute("minutes")),
            "" if prediction.getAttribute("minutes") == "1" else "s",
            "layover, " if prediction.getAttribute("affectedByLayover") == "true" else "",
            "delayed, " if prediction.getAttribute("delayed") == "true" else "",
            "<a href='%s../../%s/%s/vehicle/%s'>%s</a>" % (
                path_adjust,
                predictions.getAttribute("routeTag"),
                stop,
                vid,
                vid),
            (", %s" % to_time(ages[vid])) if vid in ages else ""))

    prediction_sections.append((predictions.getAttribute("routeTitle"),
                                predictions.getAttribute("routeTag"),
                                "\n".join(prediction_section)))

  natural_sort_in_place(prediction_sections)

  escaped_content = []
  # sort the route requested before others
  for _, route_tag, prediction_section in prediction_sections:
    if route_tag == route:
      escaped_content.append(prediction_section)
  # now include any that have predictions
  for _, route_tag, prediction_section in prediction_sections:
    if route_tag != route:
      if no_predictions not in prediction_section:
        escaped_content.append(prediction_section)
  # now the ones without predictions
  for _, route_tag, prediction_section in prediction_sections:
    if route_tag != route:
      if no_predictions in prediction_section:
        escaped_content.append(prediction_section)

  return escape(stop_title), escaped_content

def nextbus_route_helper(agency, route):
  stops = {}
  xmldoc = minidom.parseString(slurp(
      "http://webservices.nextbus.com/service/publicXMLFeed?"
      "command=routeConfig&terse&a=%s&r=%s" % (agency, route),
      timeout=2))

  for stop in xmldoc.getElementsByTagName("stop"):
    if stop.getAttribute("title"):
      stops[stop.getAttribute("tag")] = [
          escape(stop.getAttribute("title")),
          float(stop.getAttribute("lat")),
          float(stop.getAttribute("lon")),
          escape(stop.getAttribute("stopId"))]

  r = [] # [[[direction_tag, direction_name], [[stop_tag, stop_name, lat, lon], ...]], ...]
  for direction in xmldoc.getElementsByTagName("direction"):
    direction_stops = []
    direction_tag= escape(direction.getAttribute("tag"))
    direction_title = escape(direction.getAttribute("title"))
    for stop in direction.getElementsByTagName("stop"):
      tag = escape(stop.getAttribute("tag"))
      stop_name, lat, lon, stopid = stops[tag]
      direction_stops.append([tag, stop_name, lat, lon, stopid])
    r.append([[direction_tag, direction_title], direction_stops])

  route_title = route
  for route_element in xmldoc.getElementsByTagName("route"):
    route_title = escape(route_element.getAttribute("title"))

  if agency == "mbta":
    # We want to sort with as:
    #   101_0_var1
    #   101_1_var1
    #   101_0_var3
    #   101_1_var3
    # This is because with the mbta low numbered variants are usually
    # what people want.

    def sortkey(entry):
      direction_tag = entry[0][0]
      match = re.findall("^(\d+)_(\d+)_var(\d+)$", direction_tag)
      if not match:
        return entry
      route, direction_bit, variant = match[0]
      return int(route), int(variant), int(direction_bit)

    r.sort(key=sortkey)
  else:
    r.sort()

  return route_title, r

def html_redirect(dest):
  return "<meta http-equiv='refresh' content='0;URL=%s'>" % dest

def bus_location_helper(agency, route, vehicleid):
  xmldoc = minidom.parseString(slurp(
      "http://webservices.nextbus.com/service/publicXMLFeed?"
      "command=vehicleLocations&a=%s&t=0" % agency,
      timeout=1))
  # just interesting vehicles:
  vehicles = {} # vid -> [route, dirtag, lat, lon, age, heading, predictable]

  # all vehicles:
  ages = {} # vid -> age
  for vehicle_node in xmldoc.getElementsByTagName("vehicle"):
    vid = escape(vehicle_node.getAttribute("id"))
    rtag = vehicle_node.getAttribute("routeTag")
    age = int(vehicle_node.getAttribute("secsSinceReport"))
    ages[vid] = age
    if route == rtag or vid == vehicleid:
        vehicles[escape(vid)] = [
            escape(rtag),
            escape(vehicle_node.getAttribute("dirTag")),
            float(vehicle_node.getAttribute("lat")),
            float(vehicle_node.getAttribute("lon")),
            age,
            int(vehicle_node.getAttribute("heading")),
            bool(vehicle_node.getAttribute("predictable"))]
  return vehicles, ages

def nextbus_stop_vehicle(agency, route, stop, vehicleid):
  # We need to draw:
  # * the route
  # * the bus in question, if found (display message if not found)
  # * maybe the other buses on the route
  # * the stop in question

  route_title, stop_info = nextbus_route_helper(agency, route)

  polylines = []
  desired_stop_loc = None

  viewport = [None, None, None, None] # minlat, minlon, maxlat, maxlon
  def seen(lat, lon):
    minlat, minlon, maxlat, maxlon = viewport
    if not minlat or lat < minlat:
      viewport[0] = lat
    if not minlon or lon < minlon:
      viewport[1] = lon
    if not maxlat or lat > maxlat:
      viewport[2] = lat
    if not maxlon or lon > maxlon:
      viewport[3] = lon

  messages = []

  for [direction_tag, direction_title], stops in stop_info:
    polyline = []
    for stop_tag, _, lat, lon, stop_id in stops:
      seen(lat, lon)
      if stop_tag == stop:
        desired_stop_loc = [lat, lon]
      polyline.append((lat, lon))
    polylines.append(polyline)

  if not desired_stop_loc:
    messages.append("Stop %s doesn't appear to be on the %s route." % (
        escape(stop), escape(route)))

  vehicles, ages = bus_location_helper(agency, route, vehicleid)
  desired_vehicle_loc = None
  other_vehicle_locs = []
  desired_vehicle_current_route = None

  for vid, (routeTag, dirTag, lat, lon, age, heading, predictable) in vehicles.items():
    seen(lat, lon)
    if vid == vehicleid:
      desired_vehicle_loc = [lat, lon, vid, heading]
      if routeTag != route:
          desired_vehicle_current_route = routeTag
    else:
      other_vehicle_locs.append([lat, lon, vid, heading])

  if not desired_vehicle_loc:
    messages.append("Vehicle %s isn't reporting a location." % (
      escape(vehicleid)))

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

  if (heading > 0) {
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
      agency, route, stop, path_adjust="../../", ages=ages)
  if stop_title != "Nextbus Error":
      escaped_content.extend(stop_content)
  else:
      escaped_content.append("<div>%s</div>" % "".join(stop_content))

  escaped_content.append("""
<div>
<br><small><i>Key: Chosen bus is red, other buses are blue, stop
is green.  Circular buses are ones that Nextbus knows the location of,
but is having trouble predicting movements for.</i></small>
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

  route_title, stop_info = nextbus_route_helper(agency, route)

  options = defaultdict(list) # [stop_tag, stop_name] -> [direction, ...]

  for [direction_tag, direction_title], stops in stop_info:
    previous_stop_tag = None
    previous_stop_name = None
    for stop_tag, stop_name, _, _ in stops:
      if relative == "next" and previous_stop_tag == stop:
        options[stop_tag, stop_name].append(direction_title)
      elif relative == "previous" and stop_tag == stop:
        options[previous_stop_tag, previous_stop_name].append(direction_title)

      previous_stop_tag = stop_tag
      previous_stop_name = stop_name

  if not options:
    return html_redirect("../")

  if len(options) == 1:
    stop_tag, stop_name = options.keys()[0]

    if stop_tag is None:
      return html_redirect("../")
    else:
      return html_redirect("../../%s" % stop_tag)

  escaped_content = []
  for (stop_tag, stop_name), directions in options.items():
    for direction in directions:
      escaped_content.append("<span class=row><a href='../../%s'>%s</a> (%s)</span>" % (stop_tag, stop_name, direction))

  return render_page(
      title="Multiple options for the %s stop" % relative,
      escaped_content=escaped_content)

def nextbus_route(agency, route):
  escaped_content = []

  route_title, stop_info = nextbus_route_helper(agency, route)

  for (direction_tag, direction_title), stops in stop_info:
    escaped_content.append("<h2>%s</h2>" % direction_title)
    for stop_tag, stop_name, _, _, stop_id in stops:
      use_id = stop_id or stop_tag
      escaped_content.append(
          '<a class=row href="%s/">%s</a>' % (use_id, stop_name))

  return render_page(
      title="%s Stops" % route_title,
      escaped_content=escaped_content)

def nextbus_agency(agency):
  routes = []
  xmldoc = minidom.parseString(slurp(
      "http://webservices.nextbus.com/service/publicXMLFeed?"
      "command=routeList&a=%s" % agency,
      timeout=2))

  for route_node in xmldoc.getElementsByTagName("route"):
    routes.append((route_node.getAttribute("title"),
                   route_node.getAttribute("tag")))
  natural_sort_in_place(routes)

  return render_page(
      title="%s routes" % escape(agency.upper()),
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
  agencies = []
  xmldoc = minidom.parseString(slurp(
      "http://webservices.nextbus.com/service/publicXMLFeed?command=agencyList",
      timeout=2))

  for agency_node in xmldoc.getElementsByTagName("agency"):
    agencies.append((agency_node.getAttribute("shortTitle") or agency_node.getAttribute("title"),
                     agency_node.getAttribute("tag")))
  natural_sort_in_place(agencies)

  return render_page(
      title="agencies",
      escaped_content=[
          '<a class=row href="%s/">%s</a>' % (escape(tag), escape(title))
          for (title, tag) in agencies],
      include_up=False)

def nextbus(path):
  if path == "/":
    return nextbus_index()

  r = re.match("^/([^/]+)/([^/]+)/([^/]+)/vehicle/([^/]+)/$", path)
  if r:
    return nextbus_stop_vehicle(*r.groups())

  r = re.match("^/([^/]+)/([^/]+)/([^/]+)/(next|previous)/$", path)
  if r:
    return nextbus_stop_relative(*r.groups())

  r = re.match("^/([^/]+)/([^/]+)/([^/]+)/$", path)
  if r:
    return nextbus_stop(*r.groups())

  r = re.match("^/([^/]+)/([^/]+)/$", path)
  if r:
    return nextbus_route(*r.groups())

  r = re.match("^/([^/]+)/$", path)
  if r:
    return nextbus_agency(*r.groups())

  return "nextbus: '%s 'not understood" % escape(path)


# actually respond to the request
# raising errors here will give a 500 and put the traceback in the body
def start(environ, start_response):
    path = environ["PATH_INFO"]
    if path.startswith("/wsgi/"):
      path = path[len("/wsgi"):]

    if path.startswith("/nextbus"):
        return nextbus.nextbus(path.replace("/nextbus", ""))

    return "not supported"

def die500(start_response, e):
    trb = "%s: %s\n\n%s" % (e.__class__.__name__, e, traceback.format_exc())
    start_response('500 Internal Server Error',
                   [('content-type', 'text/plain')])
    return trb

def application(environ, start_response):
    path = environ["PATH_INFO"]
    if path.startswith("/nextbus"):
      try:
        output = nextbus(path.replace("/nextbus", ""))
        start_response('200 OK', [('content-type', 'text/html')])
      except Exception, e:
        output = die500(start_response, e)
    else:
      output = "not understood"

    return (output.encode('utf8'), )

