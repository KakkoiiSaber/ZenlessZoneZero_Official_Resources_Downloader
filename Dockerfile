FROM python:3.12-slim
ARG RUNTIME_SIGNATURE=""
LABEL io.github.kakkoiisaber.zzz.runtime-signature=$RUNTIME_SIGNATURE
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-download.txt ./
RUN pip install --no-cache-dir -r requirements-download.txt
USER 1000:1000
ENTRYPOINT ["python", "-m", "src.download"]
