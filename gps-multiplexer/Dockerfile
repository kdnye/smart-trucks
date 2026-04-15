FROM alpine:3.22

# coreutils provides stty for serial port pre-configuration.
# socat creates the virtual PTY multiplexer.
RUN apk add --no-cache coreutils socat

COPY gps-multiplexer/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

ENTRYPOINT ["/usr/local/bin/start.sh"]
