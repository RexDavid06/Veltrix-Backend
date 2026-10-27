# Backend Dockerfile - minimal production image
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

RUN SECRET_KEY=veltrix-static-build-only-not-a-production-secret python manage.py collectstatic --noinput

# Migrations run on every start: the container filesystem (and Render's disk)
# is wiped on each deploy, so the API would otherwise answer register/login
# with "no such table: veltrix_app_user".
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn veltrix_project.wsgi:application --bind 0.0.0.0:8000 --workers 2"]
