FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-download.txt ./
RUN pip install --no-cache-dir -r requirements-download.txt
COPY src/download.py ./src/download.py
USER 1000:1000
ENTRYPOINT ["python", "-m", "src.download"]
