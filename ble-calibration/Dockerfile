FROM balenalib/raspberrypi3-python:3.11-run AS builder

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY ble-calibration/requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM balenalib/raspberrypi3-python:3.11-run

RUN apt-get update && apt-get install -yq \
    bluetooth \
    bluez \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY ble-calibration/requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && python -c "import aiohttp, aiosqlite, bleak, uvloop"

COPY ble-calibration/ .

CMD ["python", "main.py"]
