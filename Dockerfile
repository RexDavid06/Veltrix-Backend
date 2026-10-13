# Backend Dockerfile - minimal production image
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

RUN python manage.py collectstatic --noinput || true

CMD ["gunicorn", "veltrix_project.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
