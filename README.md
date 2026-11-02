# AIWAF — AI-assisted invoice processing for accounting firms

AIWAF turns Spanish invoices into reviewable accounting data and Intermega-formatted CSV files. It is designed for accounting firms working with hospitality businesses (HORECA), where invoices vary widely in layout, supplier, tax treatment, and document quality.

Rather than relying on an LLM to make an accounting decision end to end, the system combines **cloud OCR, targeted Gemini calls, deterministic validation, and human review**. The result is a workflow for preparing accounting records—not a replacement for a qualified accountant or an autonomous posting system.

## What it does

- Accepts PDF and image invoices, organized into purchase/expense, sales/income, and investment-asset books.
- Splits multi-page PDFs using either a default one-invoice-per-page rule or an optional Gemini-based grouping for multi-page invoices.
- Extracts invoice fields and tax lines, checks Spanish tax identities and amounts, and proposes accounting concepts and accounts.
- Routes incomplete, uncertain, blocked, and failed cases for attention; users can inspect documents, correct data, approve or reject invoices, and export approved data.
- Generates semicolon-delimited CSV files for Intermega, grouped by accounting-firm client and invoice type. Invoices without tax lines do not produce export rows.
- Keeps intermediate JSON artifacts, processing states, and audit logs on the local filesystem, with duplicate detection based on fiscal identifiers.

## How the AI pipeline works

```text
PDF / image upload
       │
       ▼
PDF splitting ── page-by-page by default; optional Gemini grouping + PyMuPDF
       │
       ▼
Text extraction ── Google Cloud Vision
       │
       ▼
Structured invoice fields and tax lines ── Gemini on Vertex AI
       │
       ▼
Identity and header  ─┐
Fiscal validation    ─┼─ parallel resolution + deterministic checks
Account classification ┘
       │
       ▼
Client resolution → assembled result → human review → Intermega CSV
```

The processing stages are implemented in [`sistema-de-asientos-automatizado/src/`](sistema-de-asientos-automatizado/src/):

1. **Document splitting:** by default, each PDF page is treated as a separate invoice. Set `SPLIT_ONE_INVOICE_PER_PAGE=false` in the backend configuration to have Gemini propose page groups instead, including multi-page invoices. The implementation checks that page references are in range, non-overlapping, and cover the document before creating split PDFs. Review split results; neither strategy guarantees correct grouping.
2. **Extraction:** Cloud Vision performs document OCR; Gemini converts the text into structured invoice data. Larger PDFs are chunked to respect the synchronous Vision page limit, and cloud requests have retry handling.
3. **Field resolution:** identity, fiscal, and semantic modules run in parallel. Identity resolution can ask Gemini to choose among existing candidates, followed by deterministic validation. Fiscal rules check tax lines and totals; semantic classification uses supplier reference data or Gemini with a constrained chart of accounts.
4. **Assembly and review:** client matching and an assembler combine the outputs into field-level results with sources, confidence values, decisions, and review reasons. The pipeline records statuses such as `auto`, `warn`, `pendiente`, `block`, and `error`; these are processing decisions, not a guarantee of correctness.
5. **Export:** the interface lets the operator review and approve the data before producing Intermega CSVs. The system does not post entries directly to Intermega.

This design confines model calls to specific tasks and checks their outputs against application rules. Some confidence values are deterministic or model-reported estimates, not calibrated probabilities; professional review remains necessary.

## Architecture

| Component | Responsibility |
| --- | --- |
| **Python / FastAPI** | API, batch orchestration, OCR and LLM integration, validation, state and audit artifacts |
| **Google Cloud Vision + Gemini on Vertex AI** | OCR, PDF splitting, structured extraction, targeted disambiguation, and account classification |
| **Next.js / React / TypeScript** | Upload, processing progress, invoice and client review, and CSV export |
| **Docker Compose** | Local deployment of frontend and backend; optional Watchtower service for on-demand image updates |
| **Filesystem + YAML** | Local invoice files, intermediate results, audit logs, and accounting reference data |

The browser connects to the Next.js interface, which proxies `/api/*` to FastAPI over the Docker network. The default Compose configuration binds the UI to `127.0.0.1:3003` and does not publish the API port on the host.

## Run locally with Docker

### Prerequisites

- Docker and Docker Compose.
- A Google Cloud project with Cloud Vision and Vertex AI enabled, access to the configured Gemini model, and a service-account JSON with appropriate permissions. Cloud processing may incur charges.
- A local service-account file stored **outside** the repository. The backend container must be able to read it.

### Configuration

From the repository root, create the two environment files:

```sh
cp .env.example .env
cp sistema-de-asientos-automatizado/.env.docker.example sistema-de-asientos-automatizado/.env.docker
```

In PowerShell, use `Copy-Item` instead of `cp` if preferred. Then:

1. Set `CREDENTIALS_PATH` in the root `.env` to the absolute path of the service-account JSON; on Windows, use a Docker-accessible path such as `C:/Users/.../service-account.json`.
2. Add `WATCHTOWER_HTTP_API_TOKEN=<a-long-random-token>` to the root `.env`. Compose requires it during configuration even if only the application services are started.
3. Set `GOOGLE_CLOUD_PROJECT_ID` in `sistema-de-asientos-automatizado/.env.docker`. Review the Gemini model and region settings for your project.

Keep credentials and populated environment files private. The tracked examples contain placeholders only.

### Build and start

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build pipeline-api interfaz
```

Open **http://127.0.0.1:3003**. This command builds the application images locally and starts only the API and interface. Stop the foreground process with `Ctrl+C`; use the same Compose files with `down` to remove the containers. The base Compose file also defines Watchtower, which requires access to the Docker socket and should only be enabled in a trusted environment.

## Tests

CI uses Python 3.12, Node.js 20, and pnpm 9. To run the backend tests without real cloud credentials, install the same dependencies used in CI:

```sh
cd sistema-de-asientos-automatizado
python -m pip install -r requirements.txt -r requirements-dev.txt "fastapi[standard]" uvicorn
python -m pytest tests/ -q
```

In a separate terminal, run the frontend unit/component tests and the mocked Playwright suite:

```sh
cd interfaz-asientos-automatizados
pnpm install --frozen-lockfile
pnpm test
pnpm exec playwright install chromium
pnpm exec playwright test -c playwright.ci.config.ts
```

GitHub Actions runs these deterministic checks on pull requests and pushes to `main`. A separate scheduled/manual multi-invoice suite calls real Google services. **Do not run `pnpm test:e2e:multi` against valuable data:** its fixtures clean operational `libros/` directories, and the suite incurs cloud costs.

## Repository guide

```text
.
├── sistema-de-asientos-automatizado/
│   ├── src/                    # FastAPI, pipeline, processing phases, audit/state writers
│   ├── data/maestros/          # Accounting and fiscal reference catalogs
│   └── tests/                  # Backend, pipeline, API, and golden-fixture tests
├── interfaz-asientos-automatizados/
│   ├── app/                    # Next.js application
│   ├── components/             # Upload, review, progress, and export UI
│   ├── lib/                    # API client, domain types, and CSV generation
│   └── e2e/                    # Playwright scenarios
├── docker-compose*.yml         # Deployment and local-build override
├── installer/                  # Windows installer and launcher tooling
├── .github/workflows/          # CI, lint, release, and real-service E2E
└── docs/                       # Additional project documentation
```

## Boundaries and limitations

- **Data privacy:** operational files are stored locally, but documents and/or extracted text are sent to Google Cloud for OCR and Gemini processing. Assess service configuration, permissions, retention, and applicable privacy obligations before using real invoices.
- **Accuracy:** scans, ambiguous invoices, model outputs, and tax edge cases can produce errors. No accuracy, cost, or throughput benchmark is claimed here. Verify outputs before importing them into accounting software.
- **Security:** the included deployment is intended for local, single-operator use; it is not a multi-user authenticated service. Do not expose the UI, API, or Docker socket to a public network.
- **Scope:** tax and account rules target Spanish invoices and the HORECA domain. Intermega CSV generation is an export, not a direct integration or automatic posting.

## License

MIT — see [LICENSE](LICENSE).
