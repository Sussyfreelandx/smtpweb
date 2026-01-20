import os
import logging
import socket
import socks  # pip install PySocks
import smtplib
import ssl    # Required for Redis SSL checks
from logging.handlers import RotatingFileHandler
from flask import Flask
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
#   CRITICAL:  GLOBAL PROXY CONFIGURATION
# ==========================================
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST: 
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched_getaddrinfo
    
    if PROXY_USER and PROXY_PASS: 
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        print(f"🔌 Web/App Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: Yes)")
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        print(f"🔌 Web/App Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: No)")
    
    socks.wrap_module(smtplib)

# ==========================================

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
csrf = CSRFProtect()
socketio = SocketIO()
cache = Cache()
limiter = Limiter(key_func=get_remote_address)


def create_app(config_name=None):
    """Application factory pattern."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # --- CRITICAL FIX FOR RENDER DEPLOYMENT ---
    if os.environ.get('RENDER'):
        app.config['SERVER_NAME'] = 'paris-sender-web.onrender.com'
        app.config['PREFERRED_URL_SCHEME'] = 'https'
    # ------------------------------------------

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    cache.init_app(app)
    limiter.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Initialize SocketIO
    socketio.init_app(
        app,
        message_queue=os.environ.get('REDIS_URL'), 
        cors_allowed_origins="*",
        async_mode='eventlet'
    )
    
    # Create upload directories
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'uploads'), exist_ok=True)
    os.makedirs(app.config.get('EMAIL_TEMPLATES_FOLDER', 'templates'), exist_ok=True)
    
    # Register blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    from app.webhooks import bp as webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    
    register_error_handlers(app)
    
    if not app.debug and not app.testing:
        setup_logging(app)
    
    if app.config.get('SENTRY_DSN'):
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration
            sentry_sdk.init(
                dsn=app.config['SENTRY_DSN'],
                integrations=[FlaskIntegration()],
                traces_sample_rate=0.1
            )
        except ImportError:  
            pass
    
    register_cli_commands(app)
    register_context_processors(app)
    
    return app


def register_error_handlers(app):
    from flask import render_template, jsonify, request
    
    @app.errorhandler(400)
    def bad_request_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Bad Request', 'message': str(error)}), 400
        return render_template('errors/400.html'), 400
    
    @app.errorhandler(403)
    def forbidden_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Forbidden', 'message': str(error)}), 403
        return render_template('errors/403.html'), 403
    
    @app.errorhandler(404)
    def not_found_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not Found', 'message': str(error)}), 404
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal Server Error', 'message': 'An unexpected error occurred'}), 500
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(429)
    def ratelimit_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Too Many Requests', 'message': 'Rate limit exceeded'}), 429
        return render_template('errors/429.html'), 429


def setup_logging(app):
    if not os.path.exists('logs'):
        os.mkdir('logs')
    
    file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240000, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Paris Sender startup')


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
    
    @app.cli.command('cleanup-old-data')
    def cleanup_old_data():
        from app.models import Recipient
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=90)
        old_recipients = Recipient.query.filter(Recipient.sent_at < cutoff).count()
        print(f'Found {old_recipients} recipients older than 90 days.')


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


# =========================================================
#   FIXED CELERY CONFIGURATION (SSL + PROTOCOL FIX)
# =========================================================
def make_celery(app):
    """Create Celery instance with SSL support for Render."""
    from celery import Celery
    
    # 1. Get the Redis URL
    redis_url = os.environ.get('REDIS_URL', app.config.get('CELERY_BROKER_URL'))
    
    # 2. CRITICAL FIX: If using SSL options, URL MUST be 'rediss://'
    # Render gives 'redis://', so we swap it here.
    if redis_url and redis_url.startswith('redis://'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)

    celery_app = Celery(
        app.import_name,
        backend=redis_url,
        broker=redis_url
    )
    
    # 3. Update config using ONLY lowercase keys
    celery_app.conf.update(
        broker_url=redis_url,
        result_backend=redis_url,
        
        # SSL Configuration for Render
        broker_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
        redis_backend_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
        
        # Connection Stability
        broker_transport_options={
            'visibility_timeout': 3600,
            'socket_timeout': 30,
            'socket_connect_timeout': 30,
            'socket_keepalive': True,
        },
        broker_connection_retry_on_startup=True,
        
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True
    )
    
    class ContextTask(celery_app.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery_app.Task = ContextTask
    return celery_app


_app = create_app()
celery = make_celery(_app)
