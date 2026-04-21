FROM balenalib/raspberrypi3-python:3.11-run

RUN apt-get update && apt-get install -yq \
    bluetooth \
    bluez \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY ble-calibration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ble-calibration/ .

CMD ["python", "main.py"]
