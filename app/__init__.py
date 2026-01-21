# ==========================================
#   CRITICAL:   ENVIRONMENT SETUP
# ==========================================
import sys
import os

# Detect if we are running as a Celery Worker
IS_CELERY = 'celery' in sys.argv[0] or (len(sys.argv) > 1 and 'celery' in sys.argv[1])

# ONLY apply Eventlet monkey patching if we are the WEB SERVER (not Celery)
# and not running database migrations
if not IS_CELERY and 'flask' not in sys.argv[0] and 'db' not in sys.argv:
    try:
        import eventlet
        # Patch everything BUT socket/ssl if you want to be safe, 
        # but for SocketIO full compatibility we usually patch all.
        # We rely on standard threading for the worker.
        eventlet.monkey_patch()
    except ImportError:
        pass

# ==========================================
#   STANDARD IMPORTS
# ==========================================
import logging
import socket
import socks
import smtplib
import ssl
from logging.handlers import RotatingFileHandler
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_socketio import SocketIO
from flask_caching import Cache
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from config import config

# ==========================================
#   SURGICAL PROXY CONFIGURATION
# ==========================================
# Apply proxy settings globally for the WEB process.
# The Worker process handles its own patching in tasks.py to avoid conflicts.
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST and not IS_CELERY:  
    # Only patch if not already patched to avoid recursion
    if socket.socket is not socks.socksocket:
        if PROXY_USER and PROXY_PASS:
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        else:
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        socks.wrap_module(smtplib)
        print(f"🔌 Web Proxy Configured: {PROXY_HOST}:{PROXY_PORT}")

# ==========================================

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
csrf = CSRFProtect()
socketio = SocketIO()
cache = Cache()
limiter = Limiter(key_func=get_remote_address)
celery = None

def get_clean_redis_url():
    """Get and clean the Redis URL for Render deployment."""
    redis_url = os.environ.get('REDIS_URL', '')
    if not redis_url:
        return None, False
    
    redis_url = redis_url.strip().rstrip('/')
    is_internal = (
        redis_url.startswith('redis://red-') and 
        '.render.com' not in redis_url and
        not redis_url.startswith('rediss://')
    )
    use_ssl = not is_internal and (
        redis_url.startswith('rediss://') or 
        '.render.com' in redis_url
    )
    
    if use_ssl and redis_url.startswith('redis://'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
    
    return redis_url, use_ssl

def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    if os.environ.get('RENDER'):
        app.config['SERVER_NAME'] = 'paris-sender-web.onrender.com'
        app.config['PREFERRED_URL_SCHEME'] = 'https'

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    cache.init_app(app)
    limiter.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # CRITICAL: Use 'threading' async mode if we are in Celery to avoid mainloop errors
    async_mode = 'threading' if IS_CELERY else 'eventlet'
    
    redis_url, _ = get_clean_redis_url()
    
    # Initialize SocketIO
    if redis_url:
        socketio.init_app(
            app,
            message_queue=redis_url,
            cors_allowed_origins="*",
            async_mode=async_mode
        )
    else:
        socketio.init_app(app, cors_allowed_origins="*", async_mode=async_mode)
    
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'uploads'), exist_ok=True)
    os.makedirs(app.config.get('EMAIL_TEMPLATES_FOLDER', 'templates'), exist_ok=True)
    
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    from app.webhooks import bp as webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    
    register_error_handlers(app)
    register_cli_commands(app)
    register_context_processors(app)
    
    return app

def make_celery(flask_app):
    from celery import Celery
    redis_url, use_ssl = get_clean_redis_url()
    
    if not redis_url:
        redis_url = 'redis://localhost:6379'
        use_ssl = False
    
    celery_app = Celery(
        flask_app.import_name,
        backend=redis_url,
        broker=redis_url
    )
    
    celery_config = {
        'broker_url': redis_url,
        'result_backend': redis_url,
        'broker_connection_retry_on_startup': True,
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'UTC',
        'enable_utc': True,
        'imports': ['app.tasks'], # Force load tasks
        'worker_concurrency': 4,  # Lower concurrency to prevent overload
        'worker_pool': 'threads'  # Force threads instead of prefork/eventlet
    }
    
    if use_ssl:
        celery_config['broker_use_ssl'] = {'ssl_cert_reqs': ssl.CERT_NONE}
        celery_config['redis_backend_use_ssl'] = {'ssl_cert_reqs': ssl.CERT_NONE}
    
    celery_app.conf.update(celery_config)
    
    class ContextTask(celery_app.Task):
        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return self.run(*args, **kwargs)
    
    celery_app.Task = ContextTask
    
    # Import tasks immediately to register them
    import app.tasks
    return celery_app

def register_error_handlers(app):
    @app.errorhandler(400)
    def bad_request_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Bad Request', 'message': str(error)}), 400
        return render_template('400.html'), 400
    
    @app.errorhandler(404)
    def not_found_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not Found', 'message': str(error)}), 404
        return render_template('404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal Server Error', 'message': 'An unexpected error occurred'}), 500
        return render_template('500.html'), 500

def register_cli_commands(app):
    @app.cli.command('init-db')
    def init_db():
        db.create_all()
        print('Database initialized.')
    
    @app.cli.command('create-admin')
    def create_admin():
        from app.models import User
        import click
        username = click.prompt('Admin username')
        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True)
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f'Admin user {username} created.')

def register_context_processors(app):
    @app.context_processor
    def inject_globals():
        from datetime import datetime
        return {
            'now': datetime.utcnow(),
            'app_name': 'Paris Sender',
            'app_version': '9.0.0',
            'features': app.config.get('FEATURES', {})
        }

# Initialize app
app = create_app()

# Initialize Celery only if we are running as a worker or strictly need it
if IS_CELERY:
    celery = make_celery(app)
