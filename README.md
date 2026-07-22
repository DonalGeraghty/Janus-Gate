# Janus Gate

A small Flask API for user authentication and AI-assisted nutrition logging. Each user supplies their own OpenAI API key. The key is encrypted with Google Cloud KMS before its ciphertext is stored in the user's private Firestore subcollection.

## Setup

Requirements: Python 3.11+, a Firestore-enabled Google Cloud project, a symmetric Cloud KMS key, and a strong `JWT_SECRET_KEY`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FLASK_ENV = "development"
$env:JWT_SECRET_KEY = "dev-secret"
$env:OPENAI_KMS_KEY_NAME = "projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys"
python app.py
```

The server listens on `PORT`, defaulting to `5000`.

For local development outside Google Cloud, use Application Default Credentials with access to Firestore and the KMS key. Authentication and nutrition entries can fall back to memory when Firestore is unavailable; API-key storage deliberately fails closed and has no plaintext or in-memory fallback.

## API

| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | `{ "email", "password" }` | Create a user and return a JWT. Passwords require at least 8 characters. |
| `POST` | `/api/auth/login` | `{ "email", "password" }` | Log in and return a JWT. |
| `GET` | `/api/auth/me` | — | Return the current user. Requires `Authorization: Bearer <JWT>`. |
| `DELETE` | `/api/auth/account` | `{ "password" }` | Delete the current user. Requires a bearer JWT and password confirmation. |
| `PUT` | `/api/user/openai-key` | `{ "api_key" }` | Verify, encrypt, and save the current user's OpenAI key. |
| `GET` | `/api/user/openai-key` | — | Return only key status, last four characters, and timestamps. |
| `DELETE` | `/api/user/openai-key` | — | Delete the current user's stored OpenAI key. |
| `POST` | `/api/nutrition/analyze` | `{ "message" }` | Use the current user's key to estimate food items, portions, calories, and protein. Does not save anything. |
| `POST` | `/api/nutrition/entries` | `{ "items", "eaten_at?", "source_message?" }` | Save a reviewed nutrition entry. |
| `GET` | `/api/nutrition/entries` | — | List entries. Supports `date=YYYY-MM-DD` (UTC) and `limit=1..100`. |
| `GET` | `/health` | — | Service and database status. |

Passwords are stored as Werkzeug hashes rather than plaintext. JWTs expire after seven days.

Saving a key performs one small OpenAI request to verify it before replacing any existing credential. Nutrition analysis uses `OPENAI_MODEL`, which defaults to `gpt-5.6`. Results are estimates and should be reviewed before they are sent to the entries endpoint. The server recalculates totals from the confirmed food items.

The plaintext key is used only for the outgoing OpenAI request. It is never returned by the API or written to Firestore or application logs. Decryption is bound to the owning user's normalized email with KMS additional authenticated data. Deleting an account also deletes its encrypted OpenAI credential and nutrition entries.

Example confirmed entry:

```json
{
  "items": [
    {
      "food": "Scrambled eggs",
      "portion": "2 large eggs",
      "calories": 180,
      "protein_g": 13
    }
  ],
  "eaten_at": "2026-07-22T12:30:00Z",
  "source_message": "I ate two scrambled eggs"
}
```

## Docker

```bash
docker build -t janus-gate .
docker run -p 8080:8080 \
  -e JWT_SECRET_KEY=your-secret \
  -e OPENAI_KMS_KEY_NAME=projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys \
  janus-gate
```
