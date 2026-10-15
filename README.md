# Ensure with Insure

Two-wheeler insurance platform: buy a policy online, file claims with a document checklist, track them live and
receive payouts on an **Ensure Card**. Includes a staff console for reviewing and settling claims.

> **Demonstration project.** Premiums are illustrative, partner insurers are fictional, payments run in a
> sandbox (no real money, no bank or card credentials collected) and all data is synthetic.

The customer journey follows the original HTML concept pages in `refrence pages for idea/`
(landing → bike details → compare plans → payment summary → pay → dashboard → claim → tracking → Ensure Card).

## What is in it

| Area | Routes | Notes |
|---|---|---|
| Public site | `/`, `/how-it-works`, `/about`, `/contact` | Contact form is stored and shown to staff |
| Buy a policy | `/quote` → `/quote/[id]` → `/checkout/[id]` → `/pay/[id]` | Quote needs no account; checkout needs one. Pricing is computed on the server |
| Customer account | `/account`, `/account/policies`, `/account/claims`, `/account/claims/new`, `/account/claims/[id]`, `/account/card` | Live claim tracking, document upload, card ledger |
| Staff console | `/console/*` | Overview, policies, claims workflow (review / approve / reject / settle), customers, analytics, enquiries |

Design: white and blue only, Next.js + Tailwind + Motion; semantic green/amber/red appear only in status badges and alerts.

## Architecture

```
frontend/  Next.js (App Router) · React · TypeScript · Tailwind 3 · Motion · Recharts · qrcode.react
backend/   FastAPI · Pydantic v2 · SQLAlchemy 2 · SQLite (Postgres-ready via DATABASE_URL)
```

- Auth: email + password (scrypt-hashed), signed bearer tokens (12 h), login throttling. Roles: `customer`,
  `admin`, `agent`, `claims_officer`, `viewer`. Customers can only reach their own records (404 otherwise);
  staff endpoints require a staff token. Viewer sees masked contact details.
- Claim workflow (server-enforced): submitted → under_review → info_requested | approved | rejected; approved → settled.
  Reasons are required for rejection / info requests; approval can be partial; **settling credits the customer's Ensure Card**.
  Nothing is ever approved automatically; the document checklist only reports what is present.
- Documents: PDF/PNG/JPG ≤ 5 MB, validated by content (magic bytes), stored under random names, downloadable only by the owner and staff.
- Payments: sandbox only. Methods: UPI QR (simulated), net banking (bank pick, no credentials), Ensure Card (real ledger debit).
- Pricing (`backend/app/pricing.py`): IDV by age depreciation, own-damage rate, third-party tariff, no-claim bonus, owner-driver PA cover, 18 % GST.
  **All figures are demo assumptions**, not an actuarial tariff.

## Run locally (PowerShell)

```powershell
# Backend -> http://localhost:8000  (docs: /docs)
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# Frontend -> http://localhost:3000   (new terminal)
cd frontend
npm install
copy .env.example .env.local
npm run dev
```

If the database schema changes between versions, delete `backend/ensure.db` (there are no migrations yet) and restart.

### Demo accounts

All share the password `demo@12345!` (override with `DEMO_PASSWORD`; hide the login helper with `NEXT_PUBLIC_SHOW_DEMO_LOGINS=false`).

| Account | Email |
|---|---|
| Customer (active policy, ₹12,500 on card, a claim needing a document) | `kamesh@gmail.com` |
| Administrator | `admin@ensure.demo` |
| Agent | `agent@ensure.demo` |
| Claims officer | `claims@ensure.demo` |
| Viewer (read-only, masked PII) | `viewer@ensure.demo` |

### Suggested demo script
1. Landing → enter `TN22D4596` → pick *Suzuki Swish 125*, 2017 → compare plans → *Buy this plan* → create account → pay with UPI QR.
2. Account dashboard → *File a claim* → upload documents against the checklist → watch the tracker.
3. In another window sign in as `claims@ensure.demo` → Claims → review → approve (try a partial amount) → mark settled.
4. Back as the customer: the claim shows *Funds available* and the Ensure Card balance has increased.

## Tests and checks

```powershell
cd backend;  .\.venv\Scripts\python.exe -m pytest -q          # 17 tests: auth, quotes, purchase, payments, claims, uploads, card
cd frontend; npm run lint; npm run typecheck; npm run build
```

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | frontend | Base URL of the API (public, no secrets) |
| `NEXT_PUBLIC_SHOW_DEMO_LOGINS` | frontend | `false` hides the demo-account helper on `/login` |
| `DATABASE_URL` | backend | Default `sqlite:///./ensure.db`; Postgres: `postgresql+psycopg://…` (+ `psycopg[binary]`) |
| `SECRET_KEY` | backend | Token signing key. **Set a long random value in production.** If empty a key is generated into `backend/.secret_key` |
| `CORS_ORIGINS` | backend | Comma-separated allowed frontend origins |
| `SEED_DEMO_DATA` | backend | Seed synthetic data (and demo accounts) when the DB is empty |
| `DEMO_PASSWORD` | backend | Password for seeded demo accounts |
| `UPLOAD_DIR`, `MAX_UPLOAD_MB` | backend | Where claim documents are stored, and the size limit |

## Deploy (shareable link)

1. **Backend on Render**: New → Blueprint → select this repo (`render.yaml`). Set `CORS_ORIGINS` to the frontend URL and `SECRET_KEY` to a random value.
   Free instances sleep and have an ephemeral disk: the demo database and uploaded documents reset on restart (data re-seeds automatically).
2. **Frontend on Vercel**: import the repo, set *Root Directory* to `frontend`, and `NEXT_PUBLIC_API_URL` to the Render URL.
3. Verify `<render-url>/api/health`, then open the Vercel URL and sign in with a demo account.

Nothing has been deployed from this machine.

## Known limitations

- Sandbox payments and fictional insurers: not a real insurance or payments product. Do not use real personal data.
- Tokens are kept in browser `localStorage` (simple for a cross-origin demo). A production build should use HttpOnly cookies.
- Login throttling is per process (in memory). No email verification, password reset, or Google / DigiLocker sign-in.
- Claim documents are stored on local disk; production needs object storage and malware scanning.
- No Alembic migrations; no notifications by email/SMS; Car, Health and Travel products are shown as "Coming soon".
