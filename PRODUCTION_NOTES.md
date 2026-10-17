### Production readiness notes

This commit adds several production-hardening and feature improvements to the backend:

- Database: support for DATABASE_URL (Postgres) via dj-database-url
- Static files: whitenoise-based static serving
- Security: HSTS, X-Frame-Options, secure cookies when DEBUG=0
- Throttling: DRF anon/user throttles configured
- Email verification: endpoints and utilities to send verification emails and verify tokens
- Audit logging middleware that writes AuditLog records for authenticated requests and admin actions
- Dockerfile and docker-compose.yml for a simple production setup (Postgres + web)

Important environment variables to set in production:
- SECRET_KEY (required in production)
- DATABASE_URL (postgres URL)
- DEBUG=0
- ALLOWED_HOSTS
- EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_PORT, EMAIL_USE_TLS (if email sending is desired)
- FRONTEND_URL (used to construct email verification links)

Notes and next steps
- Migrations run automatically when the container starts (the Dockerfile CMD runs `python manage.py migrate --noinput` before gunicorn), so no manual migrate step is needed after deploying the image. This is required on Render, where the instance disk is recreated on every deploy.
- Create a superuser using `python manage.py createsuperuser` for admin access.
- The email verification uses Django's signing utilities and a front-end URL `FRONTEND_URL` where a verification route should exist to consume the token and call the verify endpoint.
- Consider adding a background worker (Celery + Redis) if you plan to poll market providers continuously.
