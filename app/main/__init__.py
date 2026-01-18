from flask import Blueprint

bp = Blueprint('main', __name__, template_folder='templates')

# Import routes at the end to avoid circular dependencies
from app.main import routes
