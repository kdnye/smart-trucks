FROM balenalib/raspberrypi3-python:3.11-run

RUN apt-get update && apt-get install -yq \
    i2c-tools \
    python3-smbus \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY imu-calibration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY imu-calibration/ .

CMD ["python", "main.py"]
