# VELTRIX Django Backend

This backend is now built on Django instead of Flask. The React front-end lives in `frontend/`, with the original static design reference kept in `frontend/reference/`. The server-side application is this `backend/` directory.

## Why Django
- Built-in authentication and authorization
- Staff/admin model and admin site
- Stronger production defaults than a minimal Flask app
- Easier security hardening for production deployments
- Better fit for real auth + admin flows without writing a custom security layer from scratch

## Local setup

Run the following from this `backend/` directory:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

## Environment variables

All configuration and secrets are read from the environment. For local
development, copy the template and fill in your own values:

```bash
cp .env.example .env
```

`.env` is git-ignored; never commit it. See `.env.example` for the full,
documented list (`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL`,
`POSTGRES_*`, `CORS_ALLOWED_ORIGINS`, `EMAIL_*`, `FRONTEND_URL`,
`MARKET_USE_LIVE`, `LOG_LEVEL`, ...). In production, provide these through your
host's environment / secrets manager instead of shipping a `.env` file.

## Funding rails

- NOWPayments and direct USDT TRC-20 funding code exists but is disabled by
  default and fail-closed.
- Direct TRON uses one shared public receiving address. The application never
  handles private keys and does not sweep funds.
- Manual bank deposits are recorded and reviewed by an administrator; account
  details are deliberately provided outside the app.
- See `docs/direct-usdt-tron.md` for configuration, monitoring, reconciliation,
  testnet rehearsal, and controlled mainnet activation.

## What is still simulated
- Demo-account top-ups are separate from real funding and should stay disabled
  in production.
- No real broker/exchange execution
- Prices are still illustrative until a real data feed is configured

## Next recommended step

1. Ship the React SPA from `frontend/` (Vite build) and point `VITE_API_URL` / `CORS_ALLOWED_ORIGINS` at this API.
2. Add a real market data service using a free source and normalise symbol mappings.
3. Replace demo-only portfolio logic with real account + order validation rules.
4. Add rate limiting, audit logging, and email verification for production hardening.
