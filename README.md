# Janus Gate

Janus Gate is the backend API for [Nyx AI](https://github.com/DonalGeraghty/NyxAI). It provides account authentication, per-user encrypted OpenAI, Mistral AI, and Anthropic credentials, selectable AI models, structured meal analysis, and user-scoped nutrition storage.

## Responsibilities

- Register users and issue seven-day JWT access tokens
- Store password hashes rather than plaintext passwords
- Verify user-supplied OpenAI, Mistral AI, and Anthropic API keys
- Encrypt provider keys with Google Cloud KMS before persistence
- Persist each user's selected provider and model
- Analyze meal descriptions with structured responses from the selected model
- Create, list, update, and delete user-owned nutrition entries
- Generate structured meal recommendations from today's calorie and protein progress
- Delete credentials and nutrition data when an account is removed
- Expose health and database-status endpoints for deployment checks

## Architecture

```text
Nyx AI or another API client
  └─ Janus Gate (Flask)
       ├─ Firestore
       │    ├─ users/{email}
       │    ├─ users/{email}/nutrition_entries/{entry}
       │    ├─ users/{email}/private/openai
       │    ├─ users/{email}/private/mistral
       │    └─ users/{email}/private/anthropic
       ├─ Google Cloud KMS
       │    └─ encrypts and decrypts each provider key
       ├─ OpenAI Responses API
       ├─ Mistral AI Chat API
       └─ Anthropic Messages API
```

## Tech stack

- Python 3.11
- Flask and Flask-CORS
- Pydantic
- PyJWT
- Firebase Admin and Google Cloud Firestore
- Google Cloud KMS
- OpenAI, Mistral AI, and Anthropic Python SDKs

## Local development

### Requirements

- Python 3.11 or newer
- A Firestore-enabled Google Cloud project for persistent data
- A symmetric Cloud KMS key for AI credential storage
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
OPENAI_MODEL=gpt-5.6-sol
MISTRAL_MODEL=mistral-small-2603
ANTHROPIC_MODEL=claude-sonnet-5
AI_KMS_KEY_NAME=projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys

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

When Firestore is unavailable, local authentication, profile settings, and nutrition operations fall back to process memory. That data disappears when the server restarts. AI credential storage deliberately has no plaintext or in-memory fallback: Firestore or KMS failures cause those operations to fail closed.

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
| `GET` | `/api/user/ai-settings` | Bearer JWT | Return the selected model, provider catalog, and safe key statuses |
| `PUT` | `/api/user/ai-settings` | Bearer JWT | Select an allowlisted provider and model |
| `PUT` | `/api/user/ai-credentials/{provider}` | Bearer JWT | Authenticate, encrypt, and store that provider's API key without generating output |
| `GET` | `/api/user/ai-credentials/{provider}` | Bearer JWT | Return safe credential-status metadata |
| `DELETE` | `/api/user/ai-credentials/{provider}` | Bearer JWT | Remove that provider's credential |
| `PUT/GET/DELETE` | `/api/user/openai-key` | Bearer JWT | Compatibility alias for the OpenAI credential |
| `POST` | `/api/nutrition/analyze` | Bearer JWT | Analyze a meal without saving it |
| `POST` | `/api/nutrition/recommend` | Bearer JWT | Recommend meals for the rest of the day |
| `POST` | `/api/nutrition/entries` | Bearer JWT | Save a reviewed nutrition entry |
| `GET` | `/api/nutrition/entries` | Bearer JWT | List entries, optionally filtered by date |
| `PUT` | `/api/nutrition/entries/{entry_id}` | Bearer JWT | Replace an owned entry and recalculate totals |
| `DELETE` | `/api/nutrition/entries/{entry_id}` | Bearer JWT | Delete an owned entry |
| `GET` | `/health` | No | Return service and database status |
| `GET` | `/` | No | List the available endpoints |

The entries list accepts:

- `date=YYYY-MM-DD` to select one UTC calendar day
- `limit=1..100`, defaulting to `50`
- `start=<ISO-8601>&end=<ISO-8601>` to select an inclusive/exclusive
  timezone-aware range of up to eight days; range requests default to a limit
  of `500`
- `all=true` to return the complete nutrition history for an explicit export;
  it cannot be combined with date, range, or limit parameters

Use either `date` or `start`/`end`, not both. Range responses include
`pagination.start`, `pagination.end`, `pagination.limit`, and
`pagination.truncated`.

AI settings accept only these provider/model combinations:

- OpenAI: `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-5.6-luna`
- Mistral AI: `mistral-small-2603`, `mistral-large-2512`, or `mistral-medium-3-5`
- Claude (Anthropic): `claude-opus-5`, `claude-sonnet-5`, or `claude-haiku-4-5-20251001`

Existing users without saved AI settings default to OpenAI and `gpt-5.6-sol`. Selecting a provider does not delete any other provider's key. Analysis and recommendation requests never fall back silently: when the selected provider has no stored key, the API returns `409 provider_key_required`.

Credential setup authenticates keys with provider model-metadata endpoints and
does not spend inference tokens. A key can be stored when the provider reports
that billing or credit is required; the successful response includes a
non-fatal `provider_billing_required` warning. Actual analysis and recommendation
requests return `402 provider_billing_required` when the account has no
available credit or has reached its spending limit. Authentication, permission,
rate-limit, and provider-availability failures remain distinct.

Deployments that introduce account generations invalidate older JWTs without an
`account_id` claim. Existing users must sign in once; a successful password
login atomically assigns the legacy account an ID and issues a new token.

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

## AI credential security

- Each user supplies their own OpenAI, Mistral AI, and/or Anthropic API key.
- The selected provider authenticates a new key through model metadata before an existing credential is replaced.
- Credit availability is checked during real AI requests rather than stored as durable credential state.
- The plaintext key is encrypted with Cloud KMS and is never returned by the API.
- Firestore stores each provider separately with only ciphertext, the last four characters, version metadata, and timestamps.
- New KMS additional authenticated data includes the normalized user email and provider, binding ciphertext to both.
- Legacy OpenAI ciphertext remains decryptable with its original user-bound authenticated data.
- The plaintext key is decrypted only when Janus Gate calls the selected provider.
- Credential operations fail closed if Firestore or KMS is unavailable.
- JWTs and guarded data operations carry an immutable account ID, so a token or in-flight request from a deleted account cannot cross into a newly registered account with the same email.
- Account deletion marks the user first; credential and nutrition writes transactionally require the matching live, non-deleting parent account so concurrent requests cannot recreate orphaned data.
- Deletion cleanup is idempotent and generation-bound. Failed deletions remain marked for an authenticated retry, and stale markers resume cleanup automatically.
- Account deletion removes all encrypted provider credentials and all nutrition entries.

Protected data is scoped through the authenticated email. The API currently allows cross-origin requests to `/api/*` from any origin while restricting methods and headers. Replace the wildcard with an origin allowlist before moving to cookie-based authentication.

## Tests

Run the complete test suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover authentication, provider/model selection, three-provider key isolation, ownership isolation, nutrition CRUD, deterministic total calculation, all provider adapters, API-key lifecycle behavior, and KMS user/provider binding.

## Docker

Build and run the production image:

```bash
docker build -t janus-gate .
docker run --rm -p 8080:8080 \
  -e JWT_SECRET_KEY=replace-with-a-long-random-secret \
  -e OPENAI_MODEL=gpt-5.6-sol \
  -e MISTRAL_MODEL=mistral-small-2603 \
  -e ANTHROPIC_MODEL=claude-sonnet-5 \
  -e AI_KMS_KEY_NAME=projects/PROJECT_ID/locations/REGION/keyRings/janus-gate/cryptoKeys/user-openai-keys \
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

The workflow grants that KMS role directly on
`projects/PROJECT_ID/locations/europe-west1/keyRings/janus-gate/cryptoKeys/user-openai-keys`
before deployment. It assumes the key ring and symmetric key already exist and
does not create or rotate either one. The identity behind `GCP_SA_KEY` must be
allowed to read and update that key's IAM policy.

The deployment sets `OPENAI_MODEL`, `MISTRAL_MODEL`, `ANTHROPIC_MODEL`, and `AI_KMS_KEY_NAME`. The service still accepts the legacy `OPENAI_KMS_KEY_NAME` during migration. Shared provider API keys are not required because every user supplies their own keys.

## Project structure

```text
.
├── core/
│   ├── auth_service.py          # Password hashing and JWT handling
│   └── nutrition_service.py     # Nutrition-entry validation
├── services/
│   ├── firebase/                # Firestore persistence by data type
│   ├── credential_service.py    # Cloud KMS encryption and decryption
│   ├── ai_catalog.py            # Allowlisted providers and models
│   ├── ai_contract.py           # Shared prompts and structured response schemas
│   ├── ai_service.py            # Provider-neutral request dispatch
│   ├── anthropic_service.py      # Anthropic key verification and meal analysis
│   ├── logging_service.py       # Console logging
│   ├── mistral_service.py       # Mistral key verification and meal analysis
│   └── openai_service.py        # OpenAI key verification and meal analysis
├── tests/                       # Unit and API tests
├── app.py                       # Flask application and routes
├── Dockerfile
└── requirements.txt
```

## Related project

- [Nyx AI](https://github.com/DonalGeraghty/NyxAI) — React frontend for the Janus Gate API
