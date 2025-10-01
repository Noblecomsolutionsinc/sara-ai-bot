# cleanup_task.py
from tasks import cleanup_mp3s
# call this via Celery Beat or an external scheduler
if __name__ == "__main__":
    print("Cleaning MP3s...")
    print(cleanup_mp3s.delay().get(timeout=60))
