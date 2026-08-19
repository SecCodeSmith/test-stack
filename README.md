# iFirma MockAPI for testing 

A self-contained test harness for the PrestaShop **`ifirma`** module. It is its own
git repository (push it to GitHub as a separate repo) and provides:

- **`ifirma-mock/`** — a **FastAPI** service that mocks the iFirma API. It implements
  the exact endpoints the module calls **and validates the module's HMAC-SHA1
  signature**, so signing bugs surface immediately.
- **`docker-compose.yml`** — boots **PrestaShop (latest)** + **MariaDB** + the mock,
  with the module bind-mounted live into PrestaShop.

```
test-stack/
├── docker-compose.yml
├── .env.example
├── Makefile
└── ifirma-mock/
    ├── Dockerfile
    ├── requirements.txt / requirements-dev.txt
    ├── app/            # FastAPI app (main, auth, pdf, store, config)
    └── tests/          # pytest suite (also documents the auth contract)
```

## Endpoint parity with the official iFirma API

Verified against the official docs at <https://api.ifirma.pl/>. The mock mirrors
the real endpoints **and response shapes** so the module is tested against realistic
behaviour:

| Purpose | Method & path | Auth key | Response (mock == real) |
|---------|---------------|----------|--------------------------|
| Create receipt | `POST /iapi/fakturaparagon.json` | `faktura` | `{"response":{"Kod":0,"Informacja":"…","Identyfikator":"<string>"}}` — **identifier is a string, no number returned** |
| Create invoice | `POST /iapi/fakturakraj.json` | `faktura` | same envelope as above |
| Download PDF | `GET /iapi/{type}/{id}.pdf` (`?typ=dup` = duplicate) | `faktura` | `application/pdf` bytes; JSON error envelope if not found |
| Register payment | `POST /iapi/faktury/wplaty/{type}/{id}.json` body `{"Kwota":…}` | `faktura` | `{"response":{"Kod":0,"Informacja":"…"}}` |
| List / read number | `GET /iapi/faktury.json?dataOd=…` | `faktura` | `{"response":{"Kod":0,"Wynik":[{"FakturaId":…,"PelnyNumer":"…", …}]}}` |

Auth on every call: `Authentication: IAPIS user=<user>, hmac-sha1=<hash>` where
`hash = HMAC-SHA1(url + user + keyName + body, hex2bin(key))` and the URL is signed
**without** its query string.

Two things the audit corrected so the mock is not misleading:

1. **Create returns only the identifier (as a string), never the printable number.**
   The number is assigned server-side and read back from `faktury.json` (under
   `Wynik`, matched on `FakturaId`). The module now does exactly this after issuing a
   document, so the number shown in PrestaShop is obtained the same way as in production.
2. **The list payload uses `Wynik`** (not `Faktury`) with the real field names
   (`FakturaId`, `PelnyNumer`, `Brutto`, `Rodzaj`, `KontrahentNazwa`, …).

## Quick start

```bash
cp .env.example .env          # optional; defaults work out of the box
docker compose up -d --build  # or: make up
```

- PrestaShop back office → http://localhost:8080/admin-dev  (`admin@example.com` / `Passw0rd!`)
- PrestaShop front office → http://localhost:8080
- iFirma mock → http://localhost:8001  (docs at http://localhost:8001/docs)

First PrestaShop boot takes a few minutes while it auto-installs. Follow it with
`make ps-logs`.

## Point the module at the mock

In the PrestaShop back office → **Modules** → install **iFirma – Receipts & Invoices**,
then open its configuration and set:

| Field | Value |
|-------|-------|
| **API base URL** | `http://ifirma-mock:8001/iapi/` |
| **iFirma login** | `test@example.com` |
| **API key: faktura** | `1111111111111111111111111111111111111111111111111111111111111111` |

These must match the `IFIRMA_MOCK_*` values in `docker-compose.yml` (from `.env`).
`ifirma-mock` is the service's hostname on the internal Docker network, which is why
the base URL uses it rather than `localhost`.

> The module signs `baseURL + endpoint` (query excluded). The mock rebuilds the same
> string as `IFIRMA_MOCK_BASE_URL + path`, so `IFIRMA_MOCK_BASE_URL`
> (`http://ifirma-mock:8001`) must equal the module base URL **minus** the `/iapi/` path.

## Test the flow

1. Create a test order in PrestaShop.
2. Open it in the back office → **iFirma** panel → **Create Receipt**.
   The request is signed by the module and verified by the mock; you get a document
   number back.
3. **Download PDF** → the mock returns a generated PDF.
4. **Update status in iFirma** → registers a payment in the mock.
5. Log in as the customer → **My account** / order detail → download the receipt.

Watch the exchange live with `make mock-logs`.

## Run the mock's unit tests (no Docker needed)

```bash
cd ifirma-mock
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
```

The `sign()` helper in the tests reproduces the module's signing algorithm exactly,
so the suite is both a functional test and an executable spec of the auth contract.

## Auth toggles

- `IFIRMA_MOCK_STRICT_AUTH=true` (default) — bad/missing signatures are rejected with
  HTTP 401, like the real API. Set to `false` to accept anything (logs a warning) when
  you just want to smoke-test wiring.

## Notes

- `prestashop/prestashop:latest` is used as requested. Pin a tag (e.g.
  `prestashop/prestashop:9`) in `docker-compose.yml` for reproducible runs.
- `make destroy` wipes the DB and PrestaShop volumes for a clean reinstall.
- Because the module folder (repo parent) is bind-mounted, its own `test-stack/`
  appears under `modules/ifirma/test-stack` inside the container — harmless; PrestaShop
  only loads `ifirma.php`.

## Legal Disclaimer & Trademarks

iFirma is a registered trademark of IFIRMA SA.

This module is an independent third-party open-source integration and is not officially affiliated with, endorsed by, or sponsored by IFIRMA SA.

The name "iFirma" and associated references are used strictly for identification, nominative reference, and descriptive compatibility purposes (interoperability / prawo cytatu) to inform users about the third-party accounting system with which this PrestaShop module integrates.
