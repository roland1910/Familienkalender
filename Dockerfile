# Home Assistant add-on image for the Familienkalender (aarch64 only).
# Explicit FROM instead of BUILD_FROM: build.yaml is deprecated since
# Supervisor 2026.04 and an explicit base image works on older versions too.
FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.22

WORKDIR /usr/src/familienkalender

COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
