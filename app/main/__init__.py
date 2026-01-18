from flask import Blueprint

# Create the blueprint object for the 'main' part of your app.
bp = Blueprint('main', __name__)

# DO NOT IMPORT ROUTES HERE. This is the crucial change.
# We will import the routes at the bottom of this file.

from app.main import routes
