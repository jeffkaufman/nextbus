# nextbus viewer

The code behind jefftk.com/nextbus/

Installation:

1. Set up your web server to point to a wsgi server.  Something like
   (for nginx):

        location /nextbus {
            include uwsgi_params;
            uwsgi_pass 127.0.0.1:7091;
            add_header Cache-Control "private;max-age=0";
        }

2. Set up your wsgi server.  I like uwsgi.  Install it, then create an
   initi script.  On Ubuntu 14.04 I set up
   /etc/init/uwsgi-nextbus.conf with:

        description "nextbus uWSGI server"

        start on runlevel [2345]
        stop on runlevel [!2345]
        respawn
        exec /usr/local/bin/uwsgi --socket :7091 --wsgi-file /home/jefftk/nextbus/nextbus.py

3. Tell your web server to make some redirects the app needs:

        rewrite ^/nextbus$ /nextbus/ permanent;
        rewrite ^/nextbus/(.*[^/])$ /nextbus/$1/ permanent;

4. Done!
