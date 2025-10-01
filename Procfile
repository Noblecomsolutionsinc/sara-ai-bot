web: gunicorn app:app --workers=1 --threads=2 --timeout=300
websocket: python streaming_server.py
worker: rq worker audio --url $REDIS_URL
