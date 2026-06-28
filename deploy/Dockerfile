# Used for container-based deployment on any platform that reads a
# Dockerfile (Fly.io, Railway, Google Cloud Run, etc.).
#
# NOTE on Fly.io specifically: as of 2026, Fly.io no longer has a free
# tier for new accounts — it was removed in 2024. New signups get a
# one-time trial (2 VM hours or 7 days) and then move to pay-as-you-go
# (a small always-on shared-CPU VM runs roughly $2-5/month). If you want
# a genuinely free deployment, use deploy/render.yaml instead — Render's
# free web service tier is still real as of this writing. This
# Dockerfile is kept so the project isn't locked to one host and remains
# useful the moment you're willing to pay a few dollars a month, or for
# any other container platform.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
