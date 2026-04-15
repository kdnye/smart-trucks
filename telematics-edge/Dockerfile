FROM balenalib/raspberrypi3-python:3.11-run

# Install hardware communication libraries for UART and I2C peripherals.
RUN apt-get update && apt-get install -yq \
    build-essential \
    gcc \
    i2c-tools \
    python3-smbus \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
