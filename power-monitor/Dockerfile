FROM balenalib/raspberrypi3-python:3.11-run

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY power-monitor/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY power-monitor ./power-monitor

WORKDIR /usr/src/app/power-monitor
CMD ["python", "main.py"]
