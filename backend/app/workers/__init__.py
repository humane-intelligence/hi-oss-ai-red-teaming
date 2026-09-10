"""Celery worker package — see app/workers/celery_app.py for the entry point.

Tasks are discovered automatically via app.autodiscover_tasks; each bounded
context defines its own tasks.py in app/core/<domain>/ and is picked up
without manual registration.  See .claude/skills/tasks/SKILL.md for conventions.
"""
