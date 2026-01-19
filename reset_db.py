from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    print("Resetting Alembic version history...")
    try:
        # This drops the table that remembers the old migration ID
        db.session.execute(text("DROP TABLE IF EXISTS alembic_version;"))
        db.session.commit()
        print("Success! Migration history cleared.")
    except Exception as e:
        print(f"Error: {e}")
