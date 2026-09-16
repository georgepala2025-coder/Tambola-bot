# Tambola Bot V4 — Deployment Package

This package is prepared for a cloud host. It uses a Dockerfile because the backend needs a real Chromium browser for Playwright.

## Option A — Railway
1. Put this folder in a GitHub repository.
2. In Railway, create a new project and connect the GitHub repository.
3. Railway detects the root `Dockerfile` automatically.
4. Deploy.
5. Generate a public domain in the service Networking settings.
6. Open the HTTPS URL on your iPhone in Safari.
7. Safari -> Share -> Add to Home Screen -> Open as Web App -> Add.

Railway supports Dockerfiles and automatically uses a root Dockerfile when present.

## Option B — Render
1. Put this folder in a GitHub repository.
2. In Render, create a new Web Service and connect the repository.
3. Choose Docker as the runtime.
4. Render will build the root Dockerfile.
5. Deploy and open the generated HTTPS URL on iPhone.
6. Add it to the Home Screen as a web app.

## Important
The app reads only publicly rendered ticket/game information. It does not bypass login or anti-bot protection, access hidden future numbers, or manipulate the game's result.

The current selector is a transparent structural heuristic, not a prediction of a future winner.
