import logging
from sara_ai.celery_app import celery_app
from sara_ai.gpt_client import generate_reply

logger = logging.getLogger("tasks")

@celery_app.task(name="process_event")
def process_event(event: dict) -> dict:
    """
    Celery task to process incoming event and generate GPT reply.
    """
    try:
        prompt = event.get("prompt", "")
        task_id = process_event.request.id
        logger.info(f"Task {task_id} started with prompt length={len(prompt)}")

        reply = generate_reply(prompt)
        return {"status": "ok", "reply": reply}
    except Exception as e:
        logger.error(f"Task failed: {e}")
        return {"status": "error", "message": str(e)}
