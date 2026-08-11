# nextbus viewer

The code behind `jefftk.com/nextbus/`.

A thin wrapper around the MBTA's [V3 API](https://api-v3.mbta.com/),
covering routes, stops, predictions, and vehicle locations for the MBTA.

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
   plugin), then create a systemd unit.  On Ubuntu 22.04 I set up
   /etc/systemd/system/uwsgi-nextbus.service with:

        [Unit]
        Description=uWSGI nextbus

        [Service]
        Environment=MBTA_API_KEY=your-key-here
        ExecStart=/usr/bin/uwsgi_python3 --socket :7091 --wsgi-file /home/jefftk/nextbus/nextbus.py
        Restart=always
        KillSignal=SIGQUIT
        Type=notify
        NotifyAccess=all

        [Install]
        WantedBy=multi-user.target

   Then `systemctl daemon-reload && systemctl enable --now uwsgi-nextbus`.
   (`uwsgi_python3` is a distro-provided binary that auto-loads the
   python3 plugin; if yours doesn't have it, use `uwsgi --plugin python3`
   instead.)

4. Tell your web server to make some redirects the app needs:

        rewrite ^/nextbus$ /nextbus/ permanent;
        rewrite ^/nextbus/(.*[^/])$ /nextbus/$1/ permanent;

5. Done!

For local testing, `python3 nextbus.py` runs a dev server on
http://127.0.0.1:8000/nextbus/ (set `PORT` to change the port).


Copying:

* Distributed under the GPL.  For details see the LICENSE file.
