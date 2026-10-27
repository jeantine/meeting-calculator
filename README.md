# Meeting Calculator

A Flask web app that finds the best city to hold a meeting for a group of
attendees, comparing **air and rail** options across Europe (and global air) by
**cost** and **carbon**.

**Live app:** https://web-production-7a866.up.railway.app/

## Running locally

```bash
python3 app.py            # dev server on http://localhost:5001
pytest                    # run the test suite
```

- `SERPAPI_KEY` is read from `.env`; without it, live prices fall back to estimates.

## Deployment

Deployed on **Railway**, which auto-deploys from the `main` branch — pushing to
`main` updates production. The app runs under gunicorn (see `Procfile`).

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the system, request-flow,
scoring-pipeline, and route-map diagrams.
