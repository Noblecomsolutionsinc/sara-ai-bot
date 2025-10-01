web: gunicorn app:app --workers=1 --threads=2 --timeout=300
stream: python streaming_server.py
worker: celery -A celery_app.celery worker --loglevel=info
