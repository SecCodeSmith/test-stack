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
| List filters | `dataOd` (required, else Kod 201), `dataDo`, `kontrahent`, `nipKontrahenta`, `typ`, `status` (comma list of `oplacone,nieoplacone,oplaconeCzesciowo,przeterminowane`), `strona`, `iloscNaStronie`, `kwotaOd`, `kwotaDo` | `faktura` | Rows have the official key order (`KontrahentNazwa` … `KsefStatus`), no total count. Deleted documents are excluded |
| Document detail | `GET /iapi/{type}/{id}.json` | `faktura` | `{"response":{"Kod":0,"Identyfikator":"…","PelnyNumer":"…","Zaplacono":…,"Uwagi":"…","DodatkoweUwagi":{…}, …}}`; unknown/deleted id → Kod 404 envelope (HTTP 200) |
| Send by e-mail | `POST /iapi/{type}/send/{id}.json` body `Tekst`, `SkrzynkaEmailOdbiorcy`, … | `faktura` | Kod 0 "Wiadomość została wysłana." for `fakturakraj`, `fakturawysylka`, `fakturaproformakraj`, … ; `fakturaparagon` → Kod 200 (not available); `Tekst` > 1000 → Kod 201 |
| API usage limit | `GET /iapi/abonent/limit.json` | `abonent` | `{"response":{"Kod":0,"LimitWykorzystany":<calls served>,"LimitPrzyznany":15000}}` |

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

## Simulating iFirma's own integrator

iFirma's own integrations (shop plugins, marketplaces) create documents the module
did not issue. Seed such "external" documents through the test-only endpoints (no HMAC):

```bash
# create an unpaid external receipt (returns the created list row incl. FakturaId)
curl -X POST http://localhost:8001/__mock/documents -H 'Content-Type: application/json' -d '{
  "doc_type": "fakturaparagon",
  "contractor": {"Nazwa": "Jan Kowalski", "Identyfikator": "JAN1", "NIP": "1234567890", "Email": "jan@example.com"},
  "gross": 123.45, "currency": "PLN", "paid": 0,
  "uwagi": "Zamówienie 42", "created_by": "external"
}'

# delete it again (disappears from lists and detail)
curl -X DELETE http://localhost:8001/__mock/documents/1

# wipe all documents and call counters
curl -X POST http://localhost:8001/__mock/reset
```

Accepted body fields: `doc_type`, `contractor`, `gross` or `Pozycje`, `currency`,
`issue_date`, `due_date`, `uwagi`, `dodatkowe_uwagi`, `draft`, `paid`, `created_by`
(default `external`), `linked_document`, `ksef_status`, `ksef_number`.

## Mock settings

| Variable | Default | Effect |
|----------|---------|--------|
| `IFIRMA_MOCK_RATE_LIMIT` | `0` (off) | When `N > 0`, the (N+1)th authenticated `/iapi/*` call within a calendar minute is answered with Kod 100 "Przekroczono limit zapytań" |
| `IFIRMA_MOCK_TEST_ENDPOINTS` | `true` | Enables `/__mock/*`; when `false` they return HTTP 404 |
| `IFIRMA_MOCK_STRICT_LIST_WINDOW` | `false` | When `true`, `faktury.json` without `dataDo` uses the real API's `dataOd + 30 days` window (off by default so a plain `dataOd=2020-01-01` call still lists new documents) |
| `IFIRMA_MOCK_KEY_ABONENT` | empty | Key for `abonent/limit.json`; empty means that endpoint always answers 401 |

## Notes

- `prestashop/prestashop:latest` is used as requested. Pin a tag (e.g.
  `prestashop/prestashop:9`) in `docker-compose.yml` for reproducible runs.
- `make destroy` wipes the DB and PrestaShop volumes for a clean reinstall.
- Because the module folder (repo parent) is bind-mounted, its own `test-stack/`
  appears under `modules/ifirma/test-stack` inside the container — harmless; PrestaShop
  only loads `ifirma.php`.

## Module smoke run

With the stack up and PrestaShop installed, run the module's end-to-end smoke script
inside the PrestaShop container (duplicate guard, data request, delivery, status sync,
cron, customer visibility), from the repository root:

```bash
docker compose -f test-stack/docker-compose.yml exec -u www-data prestashop php modules/ifirma/tests/smoke/run.php
```

Run it as `www-data`: a root-run script leaves root-owned cache directories under `var/cache` and Apache then fails with `unable to create directory`. Repair: `docker compose exec prestashop chown -R www-data:www-data /var/www/html/var`.

It exits non-zero on any failed assertion. It creates real orders in the dev database;
use `make destroy` to reset. Details, environment overrides and limitations are in
[`tests/smoke/README.md`](../tests/smoke/README.md).

## Legal Disclaimer & Trademarks

iFirma is a registered trademark of IFIRMA SA.

This module is an independent third-party open-source integration and is not officially affiliated with, endorsed by, or sponsored by IFIRMA SA.

The name "iFirma" and associated references are used strictly for identification, nominative reference, and descriptive compatibility purposes (interoperability / prawo cytatu) to inform users about the third-party accounting system with which this PrestaShop module integrates.
