from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
login.login_message_category = 'info'
csrf = CSRFProtect()


def create_app(config_class=None):
    app = Flask(__name__)
    
    # Load configuration
    if config_class is None:
        # Import here to avoid circular imports
        from config import Config
        config_class = Config
    
    app.config.from_object(config_class)
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # Register blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    # Create tables if they don't exist (for initial setup)
    with app.app_context():
        db.create_all()
    
    return app


# Import models at the bottom to avoid circular imports
from app import models
