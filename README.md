# Janus Gate

Janus Gate is the backend API for [Nyx AI](https://github.com/DonalGeraghty/NyxAI). It provides account authentication, per-user encrypted OpenAI credentials, structured meal analysis, and user-scoped nutrition storage.

## Responsibilities

- Register users and issue seven-day JWT access tokens
- Store password hashes rather than plaintext passwords
- Verify user-supplied OpenAI API keys
- Encrypt OpenAI keys with Google Cloud KMS before persistence
- Analyze meal descriptions with structured OpenAI responses
- Create, list, update, and delete user-owned nutrition entries
- Delete credentials and nutrition data when an account is removed
- Expose health and database-status endpoints for deployment checks

## Architecture

```text
Nyx AI or another API client
  └─ Janus Gate (Flask)
       ├─ Firestore
       │    ├─ users/{email}
       │    ├─ users/{email}/nutrition_entries/{entry}
       │    └─ users/{email}/private/openai
       ├─ Google Cloud KMS
       │    └─ encrypts and decrypts each user's OpenAI key
       └─ OpenAI Responses API
            └─ validates keys and returns structured meal analysis
```

## Tech stack

- Python 3.11
- Flask and Flask-CORS
- Pydantic
- PyJWT
- Firebase Admin and Google Cloud Firestore
- Google Cloud KMS
- OpenAI Python SDK

## Local development

### Requirements

- Python 3.11 or newer
- A Firestore-enabled Google Cloud project for persistent data
- A symmetric Cloud KMS key for OpenAI credential storage
- Google Application Default Credentials or a service-account credential

Create a virtual environment and install the dependencies:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

Activate the environment before running commands:

```powershell
# PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS or Linux
source .venv/bin/activate
```

Create a local `.env` file:

```dotenv
FLASK_ENV=development
JWT_SECRET_KEY=replace-with-a-long-random-secret
OPENAI_MODEL=gpt-5.6
OPENAI_KMS_KEY_NAME=projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys

# Optional when Application Default Credentials are not available:
# GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json

# Optional; defaults to 5000:
# PORT=5000

# Optional; otherwise JWT_SECRET_KEY is used:
# OPENAI_SAFETY_SALT=replace-with-an-independent-random-secret
```

Start the API:

```bash
python app.py
```

The server listens on `http://localhost:5000` unless `PORT` is set.

When Firestore is unavailable, local authentication and nutrition operations fall back to process memory. That data disappears when the server restarts. OpenAI credential storage deliberately has no plaintext or in-memory fallback: Firestore or KMS failures cause those operations to fail closed.

## Authentication

Register or log in to receive a JWT, then supply it to protected endpoints:

```http
Authorization: Bearer <token>
```

Passwords must contain at least eight characters. JWTs use HS256 and expire after seven days.

## API

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | No | Create an account and return a JWT |
| `POST` | `/api/auth/login` | No | Authenticate and return a JWT |
| `GET` | `/api/auth/me` | Bearer JWT | Return the current user |
| `DELETE` | `/api/auth/account` | Bearer JWT | Delete the account after password confirmation |
| `PUT` | `/api/user/openai-key` | Bearer JWT | Verify, encrypt, and store an OpenAI API key |
| `GET` | `/api/user/openai-key` | Bearer JWT | Return safe credential-status metadata |
| `DELETE` | `/api/user/openai-key` | Bearer JWT | Remove the stored credential |
| `POST` | `/api/nutrition/analyze` | Bearer JWT | Analyze a meal without saving it |
| `POST` | `/api/nutrition/entries` | Bearer JWT | Save a reviewed nutrition entry |
| `GET` | `/api/nutrition/entries` | Bearer JWT | List entries, optionally filtered by date |
| `PUT` | `/api/nutrition/entries/{entry_id}` | Bearer JWT | Replace an owned entry and recalculate totals |
| `DELETE` | `/api/nutrition/entries/{entry_id}` | Bearer JWT | Delete an owned entry |
| `GET` | `/health` | No | Return service and database status |
| `GET` | `/` | No | List the available endpoints |

The entries list accepts:

- `date=YYYY-MM-DD` to select one UTC calendar day
- `limit=1..100`, defaulting to `50`

### Example nutrition entry

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

The API recalculates total calories and protein from the submitted items. Meal analysis is an estimate and is not saved automatically.

## OpenAI credential security

- Each user supplies their own OpenAI API key.
- A small OpenAI request verifies the key before an existing credential is replaced.
- The plaintext key is encrypted with Cloud KMS and is never returned by the API.
- Firestore stores only ciphertext, the last four characters, and timestamps.
- KMS additional authenticated data includes the normalized user email, binding ciphertext to its owner.
- The plaintext key is decrypted only when Janus Gate makes an OpenAI request.
- Credential operations fail closed if Firestore or KMS is unavailable.
- Account deletion removes the encrypted credential and all nutrition entries.

Protected data is scoped through the authenticated email. The API currently allows cross-origin requests to `/api/*` from any origin while restricting methods and headers. Replace the wildcard with an origin allowlist before moving to cookie-based authentication.

## Tests

Run the complete test suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover authentication, ownership isolation, nutrition CRUD, deterministic total calculation, API-key lifecycle behavior, and KMS user binding.

## Docker

Build and run the production image:

```bash
docker build -t janus-gate .
docker run --rm -p 8080:8080 \
  -e JWT_SECRET_KEY=replace-with-a-long-random-secret \
  -e OPENAI_MODEL=gpt-5.6 \
  -e OPENAI_KMS_KEY_NAME=projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys \
  janus-gate
```

The container runs as a non-root user, listens on port `8080`, and checks `/health`.

## Google Cloud deployment

The workflow in [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) tests the application and deploys it from source to Google Cloud Run in `europe-west1`.

Configure these GitHub Actions secrets:

| Secret | Purpose |
| --- | --- |
| `GCP_SA_KEY` | Authenticates the deployment workflow to Google Cloud |
| `JWT_SECRET_KEY` | Signs production JWTs |

The Cloud Run runtime service account needs:

- Firestore access for users, credentials, and nutrition entries
- `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the configured KMS key

The deployment sets `OPENAI_MODEL` and `OPENAI_KMS_KEY_NAME`. A shared `OPENAI_API_KEY` is not required because every user supplies their own key.

## Project structure

```text
.
├── core/
│   ├── auth_service.py          # Password hashing and JWT handling
│   └── nutrition_service.py     # Nutrition-entry validation
├── services/
│   ├── firebase/                # Firestore persistence by data type
│   ├── credential_service.py    # Cloud KMS encryption and decryption
│   ├── logging_service.py       # Console logging
│   └── openai_service.py        # Key verification and meal analysis
├── tests/                       # Unit and API tests
├── app.py                       # Flask application and routes
├── Dockerfile
└── requirements.txt
```

## Related project

- [Nyx AI](https://github.com/DonalGeraghty/NyxAI) — React frontend for the Janus Gate API
