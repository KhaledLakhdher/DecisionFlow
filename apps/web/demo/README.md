# Demo recording

Produces `demo/decisionflow-demo.mp4` at the repository root — a scripted
walkthrough driving the **real** stack. Nothing here is mocked: the uploads run
through the actual ingestion pipeline, the briefing spends real Gemini calls,
and the star schema is inferred from the uploaded values.

That is the point. A demo recorded against fixtures shows what the product is
supposed to do; this one shows what it does.

## Running it

Four things must be up first:

```bash
docker compose up -d                                    # postgres, redis, minio
cd apps/api && ./.venv/Scripts/python -m uvicorn decisionflow.main:app --port 8001
cd apps/api && ./.venv/Scripts/python -m arq decisionflow.worker.main.WorkerSettings
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8001 npm run dev
```

Then:

```bash
cd apps/web && node demo/record.mjs
ffmpeg -i demo/recording/*.webm -c:v libx264 -crf 23 -preset slow \
       -pix_fmt yuv420p -r 30 -movflags +faststart ../../demo/decisionflow-demo.mp4
```

The script deletes the demo workspace's datasets before recording, so runs are
repeatable rather than accumulating a longer and longer dataset list.

## Two things that will bite you

**Use `localhost`, not `127.0.0.1`, for the web origin.** Next's dev server
blocks cross-origin requests to `/_next/*` from a host it does not recognise,
and the API's CORS allowlist is `http://localhost:3000`. Point the browser at
`127.0.0.1:3000` and the page serves, fails to hydrate, and the login form
falls back to a native GET — which looks like a broken login rather than a
blocked asset.

**Port 8001, not 8000.** Another project on this machine holds 8000.

## Credentials

`data/credentials.json` is gitignored and holds a throwaway account for a
workspace named *Northwind Trading*. Recreate it with:

```bash
curl -X POST http://127.0.0.1:8001/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@decisionflow.app","password":"<a long passphrase>",
       "full_name":"Demo User","organization_name":"Northwind Trading"}'
```

## The data

`data/generate.py` writes the three CSVs, seeded so every run is identical. The
shape is deliberate — each module needs something to find rather than something
to decline:

| Planted | So that |
|---|---|
| 12 months, 185 orders | the forecaster has history to extrapolate |
| 28 customers, 6 lapsing early | churn clears its 20-customer floor and has real signal |
| a collapsed March | the sentinel has an anomaly to catch |
| North collapsing as West surges | the investigator hits its offsetting case |
| currency text, a duplicate, a padded value, an `N/A` | the quality report is not empty |

Separate `customers.csv` and `products.csv` are what make relationship
detection meaningful: country and category are only reachable through a join
the system has to infer for itself.

## The injected overlay

Playwright's video capture does not draw a mouse pointer, so an unassisted
recording looks like things happening by themselves. `record.mjs` injects a
synthetic cursor and a caption bar, and every click moves the pointer to its
target and pauses before pressing. The captions also carry the narration, since
the video has no audio track.
