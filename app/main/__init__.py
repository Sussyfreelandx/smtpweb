from flask import Blueprint

bp = Blueprint('main', __name__)

# Import routes to register them with the blueprint
from app.main import routes
