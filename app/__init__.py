from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from config import Config

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Init extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    celery.conf.update(app.config)

    # Register Main Blueprint (Dashboard, Settings, Campaigns)
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # Register Tracking Blueprint (Open/Click Tracking)
    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    return app
