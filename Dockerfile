# Imagen base: Debian 13 (Trixie) + Python 3.13 slim.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system app && \
    useradd --system --gid app --home /app --shell /usr/sbin/nologin app

COPY main.py .
COPY x_gnarly.py .

RUN mkdir -p /data && chown -R app:app /data

VOLUME ["/data"]

USER app

CMD ["python", "-u", "main.py"]
