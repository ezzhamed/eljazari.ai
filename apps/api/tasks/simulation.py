"""eljazari.ai — Celery simulation tasks."""

from celery_app import app


@app.task(name="tasks.simulation.run", bind=True, max_retries=3)
def run_simulation(self, project_id: str, design_id: str) -> dict:
    """Run a drone simulation asynchronously."""
    try:
        # Import here to avoid circular imports at module load time
        import asyncio
        from services.gazebo_service import GazeboService

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                GazeboService().run(project_id=project_id, design_id=design_id)
            )
            return {"status": "pass", "result": result}
        finally:
            loop.close()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30)
