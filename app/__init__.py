from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf. csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
csrf = CSRFProtect()


def create_app(config_class=None):
    app = Flask(__name__)
    
    if config_class is None:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from config import Config
        config_class = Config
    
    app. config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    return app
