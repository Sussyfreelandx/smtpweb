# This is the entry point for the Gunicorn web server and Flask commands.
import os
from app import create_app

# The create_app function is the application factory.
# We create the app instance here so it can be imported by Gunicorn and Flask.
app = create_app(os.getenv('FLASK_CONFIG') or 'default')

# This part is not strictly necessary for deployment but is good practice
# for running the app locally with `flask run`.
if __name__ == '__main__':
    app.run()
