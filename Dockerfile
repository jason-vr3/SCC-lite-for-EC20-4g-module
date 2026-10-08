# SCC-lite Docker image
# Build: docker build -t scc-lite .
# Run (example - adjust devices for your modem):
#   docker run -d --name scc-lite --restart unless-stopped \
#     -p 7577:7577 \
#     -v /opt/scc-lite-for-EC20-4g-module/config.yaml:/opt/scc-lite-for-EC20-4g-module/config.yaml:ro \
#     -v scc-data:/opt/scc-lite-for-EC20-4g-module/data \
#     --device /dev/ttyUSB3 \
#     --device /dev/cdc-wdm0 \
#     scc-lite
#
# NOTE: expose ONLY the AT port SCC-lite should use (/dev/ttyUSB3).
# Do NOT expose the voice port (e.g. /dev/ttyUSB2 if Asterisk uses it).

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libqmi-utils \
        iproute2 \
        iputils-ping \
        && rm -rf /var/lib/apt/lists

WORKDIR /opt/scc-lite-for-EC20-4g-module
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY modem.py notifications.py data_control.py scc-lite.py scc-web.py ./
COPY config.yaml ./config.yaml.example

VOLUME ["/opt/scc-lite-for-EC20-4g-module/data"]
EXPOSE 7577

# scc-web is the foreground process; run scc-lite daemon alongside via
# a process supervisor, or run two containers. Simple default: web only,
# start daemon with --entrypoint python3 scc-lite.py in a second container
# sharing the data volume.
CMD ["python3", "scc-web.py", "--config", "/opt/scc-lite-for-EC20-4g-module/config.yaml"]
