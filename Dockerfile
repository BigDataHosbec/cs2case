FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .
CMD ["python3", "monitor.py"]
