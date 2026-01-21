import sys
import os
import socket

# ==========================================
#   CRITICAL:   EVENTLET MONKEY PATCHING
#   MUST RUN BEFORE ANY OTHER IMPORTS
# ==========================================

# 1. Detect if we are running in a context that needs Eventlet
# We only patch if we are NOT in a Celery worker (Celery handles its own concurrency)
IS_CELERY = 'celery' in sys.argv[0] or (len(sys.argv) > 1 and 'celery' in sys.argv[1])

# 2. Apply Eventlet Patching EARLY
if not IS_CELERY and not os.environ.get('SKIP_EVENTLET_PATCH'):
    try:
        import eventlet
        eventlet.monkey_patch()
        print("✅ Eventlet monkey_patch() applied successfully.")
    except ImportError:
        pass

# ==========================================
#   STANDARD IMPORTS
# ==========================================
import logging
import socks
import smtplib
import ssl
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
#   SURGICAL PROXY CONFIGURATION (FIXED)
# ==========================================
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST:
    print(f"🔌 Configuring Proxy: {PROXY_HOST}:{PROXY_PORT}")
    
    # --- FIX 1: RETRIEVE ORIGINAL SOCKET MODULE ---
    # We must grab the original 'socket' module hidden by Eventlet
    try:
        from eventlet.patcher import original
        real_socket = original('socket')
    except (ImportError, AttributeError):
        import socket as real_socket

    # --- FIX 2: FORCE PYSOCKS TO USE REAL SOCKET ---
    # This prevents the RecursionError.
    # By default, socks.socksocket inherits from socket.socket. 
    # If socket.socket is patched (GreenSocket), the recursion happens.
    # We force socks.socket to be the REAL OS socket class.
    socks.socket = real_socket.socket
    
    # --- FIX 3: RESTORE ATTRIBUTES ONTO SOCKS MODULE ---
    # Since we swapped the underlying socket class, we must ensure
    # PySocks can still find the constants it expects on 'socks.socket'
    for attr in ['AF_INET', 'AF_INET6', 'SOCK_STREAM', 'SOCK_DGRAM', 'SOL_TCP', 'TCP_NODELAY']:
        if hasattr(real_socket, attr):
            try:
                setattr(socks.socket, attr, getattr(real_socket, attr))
            except AttributeError:
                pass

    # Restore Exceptions (Critical for try/except blocks in PySocks)
    if hasattr(real_socket, 'error'):
        socks.socket.error = real_socket.error
    if hasattr(real_socket, 'timeout'):
        socks.socket.timeout = real_socket.timeout

    # --- FIX 4: IPv4 FORCE HACK (DNS Resolution) ---
    # We patch the *original* getaddrinfo that PySocks uses internally to prevent IPv6 issues
    _orig_getaddrinfo = real_socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # Force IPv4 (AF_INET) if family is unspecified (0) or explicitly IPv6
        if family == 0 or family == getattr(real_socket, 'AF_INET6', 999):
            try:
                return _orig_getaddrinfo(host, port, real_socket.AF_INET, type, proto, flags)
            except Exception:
                pass
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    # Apply the IPv4 patch to the real socket module
    real_socket.getaddrinfo = patched_getaddrinfo

    # --- FIX 5: CONFIGURE PROXY ---
    if PROXY_USER and PROXY_PASS:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)

    # --- FIX 6: WRAP SMTPLIB ---
    # This instructs smtplib to use the modified socks.socksocket (which is now based on the real socket)
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

# ==========================================
#   CELERY INSTANCE (LAZY INITIALIZATION)
# ==========================================
celery = None  # Will be initialized only when needed


def get_celery():
    """Get or create the Celery instance lazily."""
    global celery
    if celery is None:  
        celery = make_celery(create_app())
    return celery


# ==========================================
#   REDIS URL HELPER
# ==========================================
def get_clean_redis_url():
    """
    Get and clean the Redis URL for Render deployment.
    """
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
    """Application factory pattern."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    if os.environ.get('RENDER'):
        app.config['SERVER_NAME'] = 'paris-sender-web.onrender.com'
        app.config['PREFERRED_URL_SCHEME'] = 'https'

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    cache.init_app(app)
    
    # Configure Limiter via app.config (Fixes TypeError in newer Flask-Limiter)
    limiter_storage = "memory://"
    if os.environ.get('REDIS_URL'):
        limiter_storage = os.environ.get('REDIS_URL')
    
    app.config['RATELIMIT_STORAGE_URI'] = limiter_storage
    limiter.init_app(app)
    
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Force eventlet async mode if not in Celery
    async_mode = 'eventlet' if not IS_CELERY else 'threading'
    
    redis_url, _ = get_clean_redis_url()
    
    socketio.init_app(
        app,
        message_queue=redis_url,
        cors_allowed_origins="*",
        async_mode=async_mode
    )
    
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'uploads'), exist_ok=True)
    os.makedirs(app.config.get('EMAIL_TEMPLATES_FOLDER', 'templates'), exist_ok=True)
    
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
            return jsonify({'error':  'Bad Request', 'message': str(error)}), 400
        return render_template('errors/400.html'), 400
    
    @app.errorhandler(403)
    def forbidden_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Forbidden', 'message':  str(error)}), 403
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
            return jsonify({'error':  'Too Many Requests', 'message':  'Rate limit exceeded'}), 429
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


def make_celery(flask_app):
    from celery import Celery
    redis_url, use_ssl = get_clean_redis_url()
    
    if not redis_url:
        print("WARNING: No REDIS_URL configured for Celery!")
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
        'broker_transport_options': {
            'visibility_timeout': 3600,
            'socket_timeout': 30,
            'socket_connect_timeout': 30,
            'socket_keepalive': True,
            'health_check_interval': 10,
        },
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'UTC',
        'enable_utc': True
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
    return celery_app

# =========================================================
#   APP INSTANTIATION
# =========================================================
app = create_app()

if IS_CELERY:
    celery = make_celery(app)
