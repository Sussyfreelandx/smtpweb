#!/usr/bin/env python3
"""
manage.py - development and operational helper.

Usage examples:
  - Run development server:
      FLASK_APP=wsgi:app flask run --host=0.0.0.0 --port=5000
    or
      python manage.py runserver

  - Run celery worker (configured with app context):
      python manage.py celery-worker

  - Run a one-off shell:
      python manage.py shell
"""

import os
import sys
import subprocess
import click

from app import create_app, celery

# Create the Flask app (ensures extensions & celery configured)
app = create_app()

# Expose WSGI app for servers / tooling
application = app


@app.cli.command("runserver")
@click.option('--host', default="0.0.0.0", help="Host to listen on")
@click.option('--port', default=5000, help="Port to listen on")
@click.option('--reload/--no-reload', default=True, help="Enable/disable reloader")
def runserver(host, port, reload):
    """Run the Flask development server."""
    # Use flask run for proper env handling
    env = os.environ.copy()
    env['FLASK_APP'] = 'wsgi:app'
    
    # Construct command
    cmd = ["flask", "run", "--host", host, "--port", str(port)]
    if reload:
        cmd.append("--reload")
        
    try:
        subprocess.check_call(cmd, env=env)
    except KeyboardInterrupt:
        print("Shutting down...")


@app.cli.command("celery-worker")
@click.option("--loglevel", default="info", help="Log level for celery worker")
@click.option("--concurrency", default=2, help="Worker concurrency")
def celery_worker(loglevel, concurrency):
    """
    Start a Celery worker using the configured app.celery object.
    This spawns a new celery process that loads the app.celery instance.
    """
    # Ensure FLASK_APP is set so flask CLI commands used in release script work
    env = os.environ.copy()
    env.setdefault("FLASK_APP", "wsgi:app")

    # Use the celery CLI. This requires celery to be installed in environment.
    # We point -A to the app.celery (app package exports 'celery' instance).
    cmd = [
        "celery",
        "-A",
        "app.celery",
        "worker",
        "--loglevel",
        loglevel,
        "--concurrency",
        str(concurrency)
    ]

    print("Starting Celery worker with command:", " ".join(cmd))
    # Replace the current process with celery so signals are forwarded
    os.execvp(cmd[0], cmd)


@app.cli.command("shell")
def _shell():
    """Open a flask shell with application context loaded."""
    env = os.environ.copy()
    env.setdefault("FLASK_APP", "wsgi:app")
    # Use flask shell
    os.execvp("flask", ["flask", "shell"])


@app.cli.command("create-admin")
@click.argument("username")
@click.argument("email")
@click.argument("password")
def create_admin(username, email, password):
    """
    Create a new admin user (helper).
    Note: Requires your User model to be importable and configured.
    """
    from app import db
    from app.models import User

    with app.app_context():
        if User.query.filter_by(username=username).first():
            print("User already exists")
            return
        u = User(username=username, email=email)
        u.set_password(password)
        u.role = 'admin'
        db.session.add(u)
        db.session.commit()
        print("Created admin user:", username)


if __name__ == "__main__":
    # app.cli is the Click group for the Flask application.
    # Invoking it directly parses sys.argv and runs the appropriate command.
    app.cli()
