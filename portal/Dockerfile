FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && crontab /app/deploy/crontab

EXPOSE 8000
# wsgi:app, not app:app -- the department's app.py is untouched, and wsgi.py is
# what mounts our API and the eval dashboard beside it.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "30", "wsgi:app"]
