# Second Brain HydraDB MVP

Slack-based second brain MVP using:

- Slack API ingestion
- HydraDB knowledge storage
- HydraDB recall
- OpenRouter cloud LLM
- FastAPI query backend

## Run

Install:

pip install -r requirements.txt

Ingest Slack:

python -m ingestion.ingest_slack

Run API:

python -m uvicorn main:app --reload --port 8000
