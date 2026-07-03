#!/usr/bin/with-contenv bashio
# Start the Familienkalender web app on the ingress port (bind to all
# interfaces so the HA ingress proxy at 172.30.32.2 can reach it).
set -e

bashio::log.info "Starte Familienkalender..."

cd /usr/src/familienkalender
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099
