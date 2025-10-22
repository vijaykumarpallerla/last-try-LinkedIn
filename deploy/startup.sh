#!/bin/bash
# Template nginx.conf from nginx.conf.template replacing ${PORT}
set -e
if [ -f /etc/nginx/nginx.conf.template ]; then
  echo "Templating nginx.conf from template"
  envsubst < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf
fi
# Start nginx in the foreground
exec /usr/sbin/nginx -g 'daemon off;'
