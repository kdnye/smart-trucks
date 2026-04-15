FROM python:3.12-alpine

# coreutils provides stty for serial port pre-configuration.
RUN apk add --no-cache coreutils

WORKDIR /usr/src/app

COPY gps-multiplexer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gps-multiplexer/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

COPY gps-multiplexer/ .

ENTRYPOINT ["/usr/local/bin/start.sh"]
