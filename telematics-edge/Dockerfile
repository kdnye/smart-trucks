FROM balenalib/raspberrypi3-python:3.11-run

# Install hardware communication libraries for UART and I2C peripherals.
# coreutils provides stty for serial port pre-configuration.
RUN apt-get update && apt-get install -yq \
    build-essential \
    gcc \
    i2c-tools \
    python3-smbus \
    coreutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY telematics-edge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /usr/src/app/shared/
COPY telematics-edge/ .

CMD ["/bin/sh", "start.sh"]
