# nextbus viewer

The code behind `jefftk.com/nextbus/`.

Originally a thin wrapper around the MBTA's old NextBus-based predictions
feed. The MBTA retired that feed in favor of their own
[V3 API](https://api-v3.mbta.com/), so this now talks to that instead.
Since the V3 API only covers the MBTA, this is MBTA-only now (it used to
support any transit agency running NextBus).

Installation:

1. (Recommended) Register for a free MBTA API key at
   https://api-v3.mbta.com/register. Without one, the API allows only 20
   requests/minute, which is easy to hit; a key raises that a lot. Set it
   in the `MBTA_API_KEY` environment variable wherever you run the app.

2. Set up your web server to point to a wsgi server.  Something like
   (for nginx):

        location /nextbus {
            include uwsgi_params;
            uwsgi_pass 127.0.0.1:7091;
            add_header Cache-Control "private;max-age=0";
        }

3. Set up your wsgi server.  I like uwsgi.  Install it (with its python3
   plugin), then create an init script.  On Ubuntu 14.04 I set up
   /etc/init/uwsgi-nextbus.conf with:

        description "nextbus uWSGI server"

        start on runlevel [2345]
        stop on runlevel [!2345]
        respawn
        env MBTA_API_KEY=your-key-here
        exec /usr/local/bin/uwsgi --plugin python3 --socket :7091 --wsgi-file /home/jefftk/nextbus/nextbus.py

4. Tell your web server to make some redirects the app needs:

        rewrite ^/nextbus$ /nextbus/ permanent;
        rewrite ^/nextbus/(.*[^/])$ /nextbus/$1/ permanent;

5. Done!

For local testing, `python3 nextbus.py` runs a dev server on
http://127.0.0.1:8000/nextbus/ (set `PORT` to change the port).


Copying:

* Distributed under the GPL.  For details see the LICENSE file.
