# proxy settings for the Universal Engine dashboard (included by ute-dashboard.conf)
proxy_pass http://127.0.0.1:8790;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Authorization "";       # the Basic-Auth credential is never forwarded to the engine
proxy_connect_timeout 5s;
proxy_read_timeout 30s;
proxy_redirect off;
