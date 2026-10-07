# SoleTrack API

The backend for SoleTrack, a sneaker resale valuation platform. It collects sold-price data, projects where a pair's price is heading, and returns a HOLD or SELL recommendation net of platform fees. It also stores each user's portfolio and sales history.

Live frontend: https://soletrack-frontend.vercel.app
Frontend repo: https://github.com/oju3/soletrack-frontend

## What it does

- Pulls resale data from two sources: eBay sold listings (via Apify) and GOAT daily sales (via KicksDB).
- Stores everything in Postgres on Supabase.
- Fits a per-sneaker price trend and projects it forward.
- Compares projected GOAT proceeds, after fees, against selling today to produce a HOLD or SELL call.
- Serves a REST API with Supabase JWT authentication, so each user only reads and writes their own portfolio.

## Projection model

- **Method:** a recency-weighted linear trend fitted to each sneaker's GOAT daily sales.
- **Tuning:** each sneaker gets its own half-life, chosen from candidates between 2 and 30 days by backtesting. Per-sneaker tuning beat a single global half-life.
- **Accuracy:** at the last full data run, average MAPE was 10.4% across 43 projected sneakers. Each projection carries a confidence tier: normal (MAPE up to 15%), low confidence (15 to 25%), or suppressed (above 25%). Suppressed sneakers get no projection rather than a misleading one.
- **HOLD or SELL:** compares GOAT-to-GOAT net proceeds at 30 days against the current value. The 5 percent gain threshold is a judgment call and has not been validated against outcomes.

## Known limitations

- The uncertainty band uses the residual standard deviation as a constant at every horizon. A proper time-series simulation would widen the band with horizon length. This is an intentional MVP simplification.
- The HOLD/SELL threshold is not evidence-based yet.
- Coverage is a small catalog: Jordan deadstock only. Some sneakers have eBay data but no GOAT match, so they get a valuation but no projection or recommendation.
- GOAT and eBay sales are recorded at different grains, so the two sources do not reconcile exactly.
- Valuations and recommendations are estimates, not financial advice.

## API

All routes except `/health` require a Supabase access token.

| Route | Purpose |
|---|---|
| `GET /health` | Service and database check |
| `GET /me` | Current user |
| `GET /sneakers/search` | Search by name or style code |
| `GET /sneakers/{id}` | Sneaker details |
| `GET /sneakers/{id}/valuation` | Current value and price comparison |
| `GET /sneakers/{id}/projection` | Projected prices and confidence tier |
| `GET /sneakers/{id}/recommendation` | HOLD or SELL |
| `GET /portfolio`, `POST /portfolio` | List and add owned pairs |
| `POST /portfolio/{id}/sell` | Mark a pair as sold |
| `GET /portfolio/sales` | Sales history with realized profit and loss |
| `DELETE /portfolio/{id}` | Remove a pair |

Sales figures are frozen when a sale is recorded, so later fee changes do not rewrite history.

## Tech stack

- Python, FastAPI, Uvicorn
- PostgreSQL on Supabase, accessed with psycopg2 and raw SQL
- Supabase Auth (JWT verification)
- Apify and KicksDB for data collection
- Hosted on Render

## Run it locally

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
SUPABASE_URL=your-supabase-project-url
DATABASE_URL=your-postgres-connection-string
```

Optionally set `EXTRA_FRONTEND_ORIGINS` to a comma-separated list of extra allowed frontend origins, with no trailing slashes. Localhost on port 5173 is allowed by default.

Start the server:

```
uvicorn app.main:app --reload
```

The API runs at http://127.0.0.1:8000 and interactive docs are at `/docs`.

## Deployment

Deployed on Render with the start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Set `SUPABASE_URL`, `DATABASE_URL`, `PYTHON_VERSION`, and `EXTRA_FRONTEND_ORIGINS` as environment variables. Because it runs on a free tier, an uptime monitor pings `/health` to keep it awake.
