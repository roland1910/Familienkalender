#!/usr/bin/with-contenv bashio
# Start the Familienkalender web app on the ingress port (bind to all
# interfaces so the HA ingress proxy at 172.30.32.2 can reach it).
set -e

bashio::log.info "Starte Familienkalender..."

# The add-on data volume is mounted root-owned; hand it to the app user.
if [ -d /data ]; then
    chown -R app /data
fi

cd /usr/src/familienkalender
exec su-exec app python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099
