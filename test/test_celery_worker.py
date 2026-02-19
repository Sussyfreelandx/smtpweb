import importlib
import os


def test_celery_worker_runs_tasks_in_flask_app_context(monkeypatch):
    # Ensure celery_worker creates the app in testing mode
    monkeypatch.setenv("APP_SETTINGS", "testing")
    monkeypatch.delenv("RENDER", raising=False)

    import celery_worker

    # Other tests may import celery_worker earlier; reload ensures the module
    # picks up the (monkeypatched) APP_SETTINGS environment variable.
    importlib.reload(celery_worker)

    @celery_worker.celery.task
    def _probe_task():
        from flask import current_app

        return bool(current_app.config.get("TESTING"))

    assert _probe_task.name in celery_worker.celery.tasks
    assert _probe_task() is True
