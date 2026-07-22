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

## BYOK deployment handoff

This section records the current Google Cloud state and remaining setup work so the deployment can be resumed in a later session.

### Architecture and key handling

- Each user supplies their own OpenAI API key through `PUT /api/user/openai-key`.
- The backend makes a small OpenAI request to verify the key before replacing a stored credential.
- The plaintext key is encrypted by Google Cloud KMS. Only ciphertext, the last four characters, and timestamps are written to Firestore.
- Credentials are stored at `users/{normalized_email}/private/openai`.
- KMS additional authenticated data includes the normalized email, so one user's ciphertext cannot be decrypted as another user.
- Plaintext is decrypted only in the backend for an OpenAI request. It is never returned by the API or intentionally written to Firestore or logs.
- There is no plaintext or in-memory credential fallback. If Firestore or KMS is unavailable, key operations fail closed.
- Deleting an account also deletes its encrypted OpenAI credential and nutrition entries.
- Cloud Run does not need a shared `OPENAI_API_KEY`; the deployment workflow no longer reads one.

### Project configuration

| Setting | Value |
| --- | --- |
| Google Cloud project | `donal-geraghty-home` |
| Region | `europe-west1` |
| Cloud Run service | `janus-gate` |
| Runtime service account | `janus-gate@donal-geraghty-home.iam.gserviceaccount.com` |
| KMS key ring | `janus-gate` |
| KMS key | `user-openai-keys` |
| Full KMS resource | `projects/donal-geraghty-home/locations/europe-west1/keyRings/janus-gate/cryptoKeys/user-openai-keys` |
| OpenAI model | `gpt-5.6` |

Production CORS is restricted to the deployed NyxAI Cloud Run origin, `https://minerva-965419436472.europe-west1.run.app`. Local development also permits Vite on `http://localhost:5173` and `http://127.0.0.1:5173`. Additional production origins can be supplied as a comma-separated `CORS_ALLOWED_ORIGINS` value.

### Confirmed Google Cloud state

On 2026-07-22, the default Firestore database was confirmed to exist:

- Database: `projects/donal-geraghty-home/databases/(default)`
- Type: `FIRESTORE_NATIVE`
- Edition: `STANDARD`
- Location: `europe-west1`
- Realtime updates: enabled
- Created: `2025-08-30T19:01:36.093529Z`

Do not create another Firestore database. The next setup task is Cloud KMS.

### Remaining Google Cloud steps

Run these commands in Google Cloud Shell. They are written so the work can be resumed from a fresh login.

1. Select the project:

   ```bash
   gcloud config set project donal-geraghty-home
   ```

2. Enable the required APIs:

   ```bash
   gcloud services enable \
     cloudkms.googleapis.com \
     firestore.googleapis.com \
     run.googleapis.com \
     cloudbuild.googleapis.com
   ```

3. Create the KMS key ring. This command is only needed once:

   ```bash
   gcloud kms keyrings create janus-gate \
     --location=europe-west1
   ```

   If it already exists, verify it instead:

   ```bash
   gcloud kms keyrings describe janus-gate \
     --location=europe-west1
   ```

4. Create the symmetric encryption key. This is also only needed once:

   ```bash
   gcloud kms keys create user-openai-keys \
     --keyring=janus-gate \
     --location=europe-west1 \
     --purpose=encryption \
     --protection-level=software \
     --rotation-period=90d \
     --next-rotation-time=2026-10-20T00:00:00Z
   ```

5. Confirm the key exists:

   ```bash
   gcloud kms keys describe user-openai-keys \
     --keyring=janus-gate \
     --location=europe-west1
   ```

6. Confirm the Cloud Run runtime service account exists:

   ```bash
   gcloud iam service-accounts describe \
     janus-gate@donal-geraghty-home.iam.gserviceaccount.com
   ```

   Create it only if the describe command reports that it does not exist:

   ```bash
   gcloud iam service-accounts create janus-gate \
     --display-name="Janus Gate API"
   ```

7. Grant that service account encryption and decryption access to this key only:

   ```bash
   gcloud kms keys add-iam-policy-binding user-openai-keys \
     --keyring=janus-gate \
     --location=europe-west1 \
     --member="serviceAccount:janus-gate@donal-geraghty-home.iam.gserviceaccount.com" \
     --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
   ```

8. Commit and push the repository changes to `main` or `master`. GitHub Actions will deploy Cloud Run with:

   ```text
   OPENAI_KMS_KEY_NAME=projects/donal-geraghty-home/locations/europe-west1/keyRings/janus-gate/cryptoKeys/user-openai-keys
   OPENAI_MODEL=gpt-5.6
   ```

9. After a successful BYOK deployment, remove the obsolete `OPENAI_API_KEY` from GitHub **Settings > Secrets and variables > Actions**, if it exists, and revoke the old shared key in the OpenAI dashboard. Keep `JWT_SECRET_KEY` and `GCP_SA_KEY`.

### Resume checklist

- [x] Default Firestore database exists in `europe-west1`.
- [x] BYOK endpoints and encrypted persistence are implemented.
- [x] Cloud Run workflow uses `OPENAI_KMS_KEY_NAME` instead of a shared OpenAI key.
- [x] Automated test suite passes (20 tests as of 2026-07-22).
- [ ] Cloud KMS API enabled.
- [ ] `janus-gate` key ring created.
- [ ] `user-openai-keys` encryption key created.
- [ ] Runtime service account granted `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key.
- [ ] Changes committed and deployed.
- [ ] BYOK credential and nutrition endpoints tested against Cloud Run.
- [ ] Obsolete shared OpenAI key removed and revoked.

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
