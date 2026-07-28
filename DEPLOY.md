# Deploy to Amvera

This project deploys from the `main` branch via Git.

## Flow

1. Commit changes locally
2. `git push origin main`
3. Amvera pulls the branch and restarts the service

## First-time setup

1. Create an app on Amvera and connect this GitHub repository
2. Set environment variables from `.env.example` in the Amvera panel
3. Confirm `amvera.yml` / `amvera-web.yml` match the service entrypoint

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill tokens
python main.py
```

Do not commit secrets, `.env`, or Telegram session files.
