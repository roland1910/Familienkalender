#!/usr/bin/with-contenv bashio
# Start the Familienkalender web app. app/serve.py runs both listeners
# (ingress app on 8099, TLS feed listener on 8100) in ONE process, so a
# single exec under su-exec keeps s6 signal handling intact for both.
set -e

bashio::log.info "Starte Familienkalender..."

# The add-on data volume is mounted root-owned; hand it to the app user.
if [ -d /data ]; then
    chown -R app /data
fi

# Certificate paths for the TLS feed listener (add-on options; paths into
# the read-only /ssl mount, not secrets). Empty values fall back to the
# defaults in app/serve.py.
export SSL_CERTFILE="$(bashio::config 'ssl_certfile')"
export SSL_KEYFILE="$(bashio::config 'ssl_keyfile')"

cd /usr/src/familienkalender
# Access logs stay off in app/serve.py: the feed URL (/feed/<token>.ics)
# carries its auth token in the path and must never appear in
# `ha addons logs`.
exec su-exec app python3 -m app.serve
