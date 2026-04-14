FROM balenalib/raspberrypi3-python:3.11-run

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY gps-multiplexer/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY gps-multiplexer/ ./

CMD ["python", "main.py"]
FROM alpine:3.22

RUN apk add --no-cache socat

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

ENTRYPOINT ["/usr/local/bin/start.sh"]
