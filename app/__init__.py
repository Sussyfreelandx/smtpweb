import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)
    
    basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-this-in-production'
    
    database_url = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'app.db')
    if database_url and database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['CELERY_BROKER_URL'] = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    app.config['CELERY_RESULT_BACKEND'] = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY')
    app.config['LOCAL_AI_URL'] = os.environ.get('LOCAL_AI_URL')

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    return app
