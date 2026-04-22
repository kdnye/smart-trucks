FROM balenalib/raspberrypi3-python:3.11-run

WORKDIR /usr/src/app

COPY sync-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync-service/ .

CMD ["python", "main.py"]
