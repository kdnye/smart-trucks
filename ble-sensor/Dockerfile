# Use Balena's optimized Python base image
FROM balenalib/raspberrypi3-python:3.11-run

# Enable UDEV for hardware/Bluetooth access
ENV UDEV=1

# Set the working directory
WORKDIR /usr/src/app

# Copy dependency list and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Execute the script
CMD ["python", "main.py"]
