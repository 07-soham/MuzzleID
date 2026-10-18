FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-mvp.txt requirements-ml.txt requirements.txt ./
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY web ./web
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /app/storage/uploads /app/storage/gallery \
    && chown -R appuser:appuser /app/storage
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
