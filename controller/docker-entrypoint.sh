#!/bin/bash

# /var/www is a bind mount, so the AriaNg landing page is rendered here at
# container start (not at build time) from its template.
sed -e "s/__RPCSECRET__/${RPCSECRET}/g" -e "s/__RPCPORT__/${RPCPORT}/g" \
    /var/www/index.html.template > /var/www/index.html

exec nginx -c /conf/nginx.conf -g "daemon off;"
