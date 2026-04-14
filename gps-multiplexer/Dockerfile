FROM balenalib/raspberrypi3-python:3.11-run

WORKDIR /usr/src/app

COPY gps-multiplexer/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY gps-multiplexer/ ./

CMD ["python", "main.py"]
