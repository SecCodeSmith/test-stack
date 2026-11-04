# iFirma MockAPI for testing 

A self-contained test harness for the PrestaShop **`ifirma`** module. It lives at
<https://github.com/SecCodeSmith/test-stack> and is consumed by the iFirma module repo as the
`test-stack` git submodule (clone the module with `--recurse-submodules`) and provides:

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
| Undocumented enumeration value | `POST /iapi/fakturaparagon.json`, `POST /iapi/fakturakraj.json` with e.g. `RodzajPodpisuOdbiorcy: "BRK"` (also `LiczOd`, `FormatDatySprzedazy`, `SposobZaplaty`, position `TypStawkiVat`) | `faktura` | Kod 200 "Niepoprawna zawartość żądania - nie można utworzyć obiektu na podstawie zawartości żądania." (observed on the live API 2026-11-02; the field is not named); nothing stored. Absent or `null` fields are not checked |
| Undocumented VAT rate | `POST /iapi/fakturaparagon.json`, `POST /iapi/fakturakraj.json` with a position `StawkaVat` outside `0, 0.05, 0.08, 0.23` (e.g. `0.07`) | `faktura` | Kod 201 `Pozycje[i].StawkaVat: niedozwolona stawka VAT`, nothing stored. **Mock's own message**: the rule is documented, the live wording is unverified. `null` and non-numeric values: `null` is allowed, a string is rejected |
| ZW without PKWiU | same endpoints with a position `TypStawkiVat: "ZW"` and empty/missing `PKWiU` | `faktura` | Kod 201 `Pozycje[i].PKWiU: pole wymagane dla stawki ZW`, nothing stored. **Mock's own message**, documented rule, live wording unverified |
| Incomplete nested contractor | same endpoints with a `Kontrahent` block missing or with an empty `Nazwa` (1–150), `KodPocztowy` (1–16) or `Miejscowosc` (1–65) | `faktura` | Kod 201 `Kontrahent.<Field>: pole wymagane` (also for over-long values), nothing stored. **Mock's own message**, documented rule, live wording unverified. Validated whenever the block is present |
| Download PDF | `GET /iapi/{type}/{id}.pdf` (`?typ=dup` = duplicate) | `faktura` | `application/pdf` bytes; JSON error envelope if not found |
| Register payment | `POST /iapi/faktury/wplaty/{type}/{id}.json` body `{"Kwota":…}` | `faktura` | `{"response":{"Kod":0,"Informacja":"…"}}` |
| List / read number | `GET /iapi/faktury.json?dataOd=…` | `faktura` | `{"response":{"Kod":0,"Wynik":[{"FakturaId":…,"PelnyNumer":"…", …}]}}` |
| List filters | `dataOd` (required, else Kod 201), `dataDo`, `kontrahent`, `nipKontrahenta`, `typ`, `status` (comma list of `oplacone,nieoplacone,oplaconeCzesciowo,przeterminowane`), `strona`, `iloscNaStronie`, `kwotaOd`, `kwotaDo` | `faktura` | Rows have the official key order (`KontrahentNazwa` … `KsefStatus`), no total count. Deleted documents are excluded |
| List filter `kontrahent` | `GET /iapi/faktury.json?dataOd=…&kontrahent=<identifier>` | `faktura` | unknown contractor identifier → Kod 201 "Nie znaleziono kontrahenta o zadanym identyfikatorze" (observed on the live API 2026-11-02, not an empty list); known identifier → filtered rows; empty value → no filter. `nipKontrahenta` stays lenient (unknown NIP → empty list) |
| Document detail | `GET /iapi/{type}/{id}.json` | `faktura` | `{"response":{"Kod":0,"Identyfikator":"…","PelnyNumer":"…","Zaplacono":…,"Uwagi":"…","DodatkoweUwagi":{…}, …}}`; unknown/deleted id → Kod 404 envelope (HTTP 200) |
| Send by e-mail | `POST /iapi/{type}/send/{id}.json` body `Tekst`, `SkrzynkaEmailOdbiorcy`, … | `faktura` | Kod 0 "Wiadomość została wysłana." for `fakturakraj`, `fakturawysylka`, `fakturaproformakraj`, … ; `fakturaparagon` → Kod 200 (not available); `Tekst` > 1000 → Kod 201 |
| Send invoice to KSeF | `POST /iapi/{type}/ksef/send/{id}.json` body `{"DataWysylki": null}` (null = send now, `YYYY-MM-DD` = scheduled); `{type}` may be `fakturakraj/korekta`; `{id}` = document id or number with `_` for `/` (e.g. `314_2026`) | `faktura` | Documented request. Response and message wording are the mock's own, unverified live: Kod 0 "Faktura została wysłana do KSeF." (status `WYSLANA`, or `ZAPLANOWANA` when a date is given; `KsefNumer` stays empty); unsupported type → Kod 200; missing key or invalid `DataWysylki` → Kod 201 `DataWysylki: nieprawidłowa wartość`; unknown id → Kod 201 "Nie znaleziono faktury o zadanym identyfikatorze"; already `WYSLANA`/`PRZYJETA` → Kod 201 "Faktura została już wysłana do KSeF" |
| API usage limit | `GET /iapi/abonent/limit.json` | `abonent` | `{"response":{"Kod":0,"LimitWykorzystany":<calls served>,"LimitPrzyznany":15000}}` |
| Contractor by id | `GET /iapi/kontrahenci/id/{identifier}.json` | `faktura` | found: `{"response":{"Kod":0,"Informacja":"","Wynik":[<contractor>]}}`; not found: Kod 208 "Nie znaleziono kontrahenta" |
| Create contractor | `POST /iapi/kontrahenci.json` | `faktura` | Kod 0 "Dodano kontrahenta" with `Wynik` = identifier; missing `Nazwa`/`KodPocztowy`/`Miejscowosc` → Kod 201 `Validation errors: …`; identifier > 15 chars or already existing → Kod 201 |
| Replace contractor | `PUT /iapi/kontrahenci/{identifier}.json` | `faktura` | Kod 0 "Zaktualizowano kontrahenta" (full replace, identifier kept); unknown → Kod 208 |
| Search contractors | `GET /iapi/kontrahenci/{phrase}.json` | `faktura` | matches on identifier, `Nazwa` or NIP (max 20) → Kod 0 + `Wynik`; none → Kod 208 "Brak wyników" |
| Document contractor lookup, step 1 | `POST /iapi/fakturaparagon.json`, `POST /iapi/fakturakraj.json` with top-level `IdentyfikatorKontrahenta` | `faktura` | stored identifier (compared after trim, any length) → that contractor is attached, its stored fields win over the `Kontrahent` block; unknown → not an error, continue with step 2 |
| Document contractor lookup, step 2 | same endpoints with top-level `NIPKontrahenta` | `faktura` | digits-only match against stored NIPs (first match attached); unknown → not an error, continue with step 3 |
| Document contractor lookup, step 3 | same endpoints with nested `Kontrahent` | `faktura` | null/empty `Identyfikator` → contractor created from the block with a generated identifier; unknown non-empty `Identyfikator` → Kod 201 "Nie znaleziono kontrahenta o zadanym identyfikatorze", nothing stored |

**Contractor rules.** As in production, a document whose `Kontrahent.Identyfikator` is
non-empty but matches no stored contractor is rejected with Kod 201 "Nie znaleziono
kontrahenta o zadanym identyfikatorze" and nothing is stored. A null or empty identifier
makes the mock create the contractor from the `Kontrahent` block with a generated
identifier (`K000001`, …), which is written back onto the document and shows up as
`IdentyfikatorKontrahenta` in list rows. The top-level lookup keys are tried first (see
the parity table): `IdentyfikatorKontrahenta`, then `NIPKontrahenta`, and only then the
nested `Kontrahent` block. The live API's handling of an `IdentyfikatorKontrahenta`
longer than the documented 15 characters is unverified; the mock accepts it. Documents seeded through `/__mock/documents`
register their contractor when its identifier is new; `POST /__mock/contractors` seeds
one directly (no HMAC) and `/__mock/reset` clears them.

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

# wipe all documents, contractors, call counters and the two switches below
curl -X POST http://localhost:8001/__mock/reset
```

Two switches reproduce iFirma behaviour the module must handle. Both are test-only (no HMAC,
disabled with `IFIRMA_MOCK_TEST_ENDPOINTS=false`) and are restored to their defaults by `/__mock/reset`:

```bash
# rename mode: POST /iapi/kontrahenci.json stores the contractor under "IFI" + digits of the sent
# Identyfikator + a 6-digit sequence (PS-C7 -> IFI7000001), so GET .../kontrahenci/id/PS-C7.json
# answers Kod 208, while the create answer still echoes the sent identifier in Wynik.
# A null/empty identifier keeps generating K%06d. Default is "normal".
curl -X POST http://localhost:8001/__mock/contractor-mode -H 'Content-Type: application/json' -d '{"create": "rename"}'
curl http://localhost:8001/__mock/contractor-mode   # {"create": "rename"}

# pdf-not-ready: the next N authenticated GET /iapi/{doc_type}/{id}.pdf calls answer
# {"response": {"Kod": 404, "Informacja": "Nie znaleziono dokumentu <id>."}} (HTTP 200), then the PDF is served again.
curl -X POST http://localhost:8001/__mock/pdf-not-ready -H 'Content-Type: application/json' -d '{"calls": 2}'
curl http://localhost:8001/__mock/pdf-not-ready     # {"remaining": 2}
```

KSeF status helpers (no HMAC) simulate the outcome of a KSeF submission, which the real
API reports asynchronously. `accept` sets `KsefStatus` to `PRZYJETA` and assigns a mock
`KsefNumer` of the form `<NIP digits or 0000000000>-<YYYYMMDD>-<12 hex>-<2 hex>` (the
format is the mock's own, unverified live); `reject` sets `ODRZUCONA` and leaves the number
empty. Both answer with the list row, and an unknown id gets HTTP 404:

```bash
curl -X POST http://localhost:8001/__mock/ksef/accept/1
curl -X POST http://localhost:8001/__mock/ksef/reject/1
```

Accepted body fields: `doc_type`, `contractor`, `gross` or `Pozycje`, `currency`,
`issue_date`, `sale_date`, `sale_date_format`, `due_date`, `uwagi`, `dodatkowe_uwagi`, `draft`, `paid`, `created_by`
(default `external`), `linked_document`, `ksef_status`, `ksef_number`.

`sale_date` (ISO `YYYY-MM-DD`) is stored as `DataSprzedazy` and `sale_date_format` as
`FormatDatySprzedazy`, both echoed by the detail endpoint. When `sale_date` is absent it
defaults to `issue_date` (or today if that is absent too); `sale_date_format` defaults to `DZN`.

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
`tests/smoke/README.md` of the iFirma module repository (the parent checkout that contains this submodule).

## Legal Disclaimer & Trademarks

iFirma is a registered trademark of IFIRMA SA.

This module is an independent third-party open-source integration and is not officially affiliated with, endorsed by, or sponsored by IFIRMA SA.

The name "iFirma" and associated references are used strictly for identification, nominative reference, and descriptive compatibility purposes (interoperability / prawo cytatu) to inform users about the third-party accounting system with which this PrestaShop module integrates.
