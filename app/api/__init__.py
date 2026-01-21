from flask import Blueprint

# Define the Blueprint object
bp = Blueprint('api', __name__)

# Import routes AFTER defining the Blueprint to avoid circular imports
from app.api import routes
