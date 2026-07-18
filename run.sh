#!/usr/bin/with-contenv bashio
# Start the Familienkalender web app. app/serve.py runs both listeners
# (ingress app on 8099, TLS feed listener on 8100) in ONE process.
#
# This script stays root and supervises that process. Reason: the Let's
# Encrypt key in /ssl is root-only (0600), while the app runs as the
# unprivileged `app` user — uvicorn could never load it directly. So
# BEFORE dropping privileges, stage_certs copies the certificate pair
# into a tmpfs directory readable only by `app` (never written to disk,
# gone after a container restart). app/serve.py watches the mtimes of
# the ORIGINALS (stat needs no read permission) and exits with code 86
# (CERT_RELOAD_EXIT_CODE in app/serve.py — a contract between the two
# files) when they change; the loop below then stages fresh copies and
# restarts the process. Every other exit code is passed through to s6.
set -e

bashio::log.info "Starte Familienkalender..."

# The add-on data volume is mounted root-owned; hand it to the app user.
if [ -d /data ]; then
    chown -R app /data
fi

# Source certificate paths (add-on options; paths into the read-only
# /ssl mount, not secrets). Empty options fall back to the HA defaults.
SSL_SOURCE_CERTFILE="$(bashio::config 'ssl_certfile')"
SSL_SOURCE_KEYFILE="$(bashio::config 'ssl_keyfile')"
: "${SSL_SOURCE_CERTFILE:=/ssl/fullchain.pem}"
: "${SSL_SOURCE_KEYFILE:=/ssl/privkey.pem}"
export SSL_SOURCE_CERTFILE SSL_SOURCE_KEYFILE

# Enable the hourly background photo-index rescan (app/main.py gates it
# behind this flag so test/dev servers without /media never scan).
export SLIDESHOW_SCAN=1

# The staged, app-readable copies uvicorn actually loads.
STAGE_DIR=/run/familienkalender-ssl
export SSL_CERTFILE="${STAGE_DIR}/fullchain.pem"
export SSL_KEYFILE="${STAGE_DIR}/privkey.pem"

stage_certs() {
    # Runs as root before su-exec: only root can read the originals.
    rm -rf "${STAGE_DIR}"
    mkdir -m 700 "${STAGE_DIR}"
    if cp "${SSL_SOURCE_CERTFILE}" "${SSL_CERTFILE}" 2>/dev/null \
            && cp "${SSL_SOURCE_KEYFILE}" "${SSL_KEYFILE}" 2>/dev/null; then
        chmod 400 "${SSL_CERTFILE}" "${SSL_KEYFILE}"
    else
        # Missing certificates only keep the feed listener off (error in
        # the app log); the calendar app must start regardless.
        rm -f "${SSL_CERTFILE}" "${SSL_KEYFILE}"
        bashio::log.warning \
            "Zertifikate nicht vorhanden (${SSL_SOURCE_CERTFILE} / ${SSL_SOURCE_KEYFILE}) — der Feed-Listener bleibt aus."
    fi
    chown -R app "${STAGE_DIR}"
}

# Forward s6 lifecycle signals to the python child so uvicorn shuts down
# gracefully; the loop then exits with the child's status.
CHILD_PID=0
SHUTDOWN=0
forward_term() {
    SHUTDOWN=1
    if [ "${CHILD_PID}" -ne 0 ]; then
        kill -TERM "${CHILD_PID}" 2>/dev/null || true
    fi
}
trap forward_term TERM INT

cd /usr/src/familienkalender
# Access logs stay off in app/serve.py: the feed URL (/feed/<token>.ics)
# carries its auth token in the path and must never appear in
# `ha addons logs`.
while true; do
    stage_certs
    su-exec app python3 -m app.serve &
    CHILD_PID=$!
    EXIT_CODE=0
    wait "${CHILD_PID}" || EXIT_CODE=$?
    # A trap interrupts `wait` (status >128) while the child is still
    # shutting down; wait again until it is gone so EXIT_CODE is the
    # child's real status.
    while kill -0 "${CHILD_PID}" 2>/dev/null; do
        EXIT_CODE=0
        wait "${CHILD_PID}" || EXIT_CODE=$?
    done
    if [ "${SHUTDOWN}" -eq 1 ] || [ "${EXIT_CODE}" -ne 86 ]; then
        exit "${EXIT_CODE}"
    fi
    # Exit code 86: renewed certificates detected — restage and restart.
    # The sleep guards against a hot loop if restarts fail repeatedly.
    bashio::log.info "Zertifikatswechsel erkannt — Neustart mit frisch kopierten Zertifikaten."
    sleep 1
done
