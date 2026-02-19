#!/usr/bin/env python3
"""
manage.py - development and operational helper.
"""

import os
import sys
import subprocess
import click

from app import create_app, db

app = create_app()

@app.shell_context_processor
def make_shell_context():
    """Ensure models are available in shell context."""
    from app.models import User, Campaign, Recipient, SMTPServer, Webhook, APIKey
    return {
        'app': app, 
        'db': db, 
        'User': User, 
        'Campaign': Campaign, 
        'Recipient': Recipient, 
        'SMTPServer': SMTPServer,
        'Webhook': Webhook,
        'APIKey': APIKey
    }

@app.cli.command("runserver")
@click.option('--host', default="0.0.0.0")
@click.option('--port', default=5000)
@click.option('--reload/--no-reload', default=True)
def runserver(host, port, reload):
    """Run the Flask development server."""
    env = os.environ.copy()
    env['FLASK_APP'] = 'wsgi:app'
    cmd = ["flask", "run", "--host", host, "--port", str(port)]
    if reload: cmd.append("--reload")
    subprocess.call(cmd, env=env)

@app.cli.command("celery-worker")
@click.option("--loglevel", default="info")
@click.option("--concurrency", default=4)
@click.option("--pool", default="eventlet")
def celery_worker(loglevel, concurrency, pool):
    """Start Celery worker. Now points to the correct Celery app instance."""
    # The -A flag points to `celery_worker.py` and the `celery` instance within it.
    cmd = [
        "celery", "-A", "celery_worker:celery", "worker",
        f"--loglevel={loglevel}", 
        f"--concurrency={concurrency}",
        f"--pool={pool}"
    ]
    # Using os.execvp replaces the current process with the Celery worker
    os.execvp(cmd[0], cmd)


@app.cli.command("create-admin")
@click.argument("username")
@click.argument("email")
@click.argument("password")
def create_admin(username, email, password):
    """Create a new admin user."""
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
        print(f"Created admin user: {username}")

if __name__ == "__main__":
    app.cli()
