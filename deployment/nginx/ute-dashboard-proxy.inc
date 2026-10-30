# proxy settings for the Universal Engine dashboard (included by ute-dashboard.conf)
proxy_pass http://127.0.0.1:8790;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;   # always overwritten: the engine counts failed logins per this address
proxy_set_header X-Forwarded-For $remote_addr;   # overwritten, never appended to a client-supplied value
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Authorization "";       # no Authorization header ever reaches the engine
proxy_connect_timeout 5s;
proxy_read_timeout 30s;
proxy_redirect off;
