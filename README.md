# Janus Gate

A small Flask API for user registration, login, account lookup, and account deletion. User records are stored in Firestore. Local development falls back to an in-memory store when Firestore credentials are unavailable.

## Setup

Requirements: Python 3.11+, a Firestore-enabled Firebase/GCP project for production, and a strong `JWT_SECRET_KEY`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FLASK_ENV = "development"
$env:JWT_SECRET_KEY = "dev-secret"
python app.py
```

The server listens on `PORT`, defaulting to `5000`.

For Firestore outside Google Cloud, set `GOOGLE_APPLICATION_CREDENTIALS` to the path of a service-account JSON file. With no credentials, accounts are kept only in memory and disappear when the process restarts.

## API

| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | `{ "email", "password" }` | Create a user and return a JWT. Passwords require at least 8 characters. |
| `POST` | `/api/auth/login` | `{ "email", "password" }` | Log in and return a JWT. |
| `GET` | `/api/auth/me` | — | Return the current user. Requires `Authorization: Bearer <JWT>`. |
| `DELETE` | `/api/auth/account` | `{ "password" }` | Delete the current user. Requires a bearer JWT and password confirmation. |
| `GET` | `/health` | — | Service and database status. |

Passwords are stored as Werkzeug hashes rather than plaintext. JWTs expire after seven days.

## Docker

```bash
docker build -t janus-gate .
docker run -p 8080:8080 -e JWT_SECRET_KEY=your-secret janus-gate
```
