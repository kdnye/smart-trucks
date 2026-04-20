FROM balenalib/raspberrypi3-python:3.11-run AS builder

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY power-monitor/requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM balenalib/raspberrypi3-python:3.11-run

WORKDIR /usr/src/app

COPY power-monitor/requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && python -c "import aiohttp, aiosqlite, ina219, smbus2, uvloop"

COPY shared/ /usr/src/app/shared/
COPY power-monitor/ .

CMD ["python", "main.py"]
