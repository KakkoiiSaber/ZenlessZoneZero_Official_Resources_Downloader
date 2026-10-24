FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-nas.txt .
RUN pip install --no-cache-dir -r requirements-nas.txt
COPY src/downloader ./src/downloader
COPY src/metadata ./src/metadata
COPY src/nas ./src/nas
COPY tools/build_gallery.py tools/deduplicate_images.py tools/gallery_tags.py tools/gallery_tag_rules.py ./tools/
USER 10001:10001
ENTRYPOINT ["python", "-m", "src.nas.sync"]
