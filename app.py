from app import app, celery

if __name__ == "__main__":
    # When running locally without gunicorn
    import eventlet
    # Patch only if not already patched by __init__.py logic
    if not eventlet.patcher.is_monkey_patched(socket):
        eventlet.monkey_patch()
    
    # Use socketio.run instead of app.run for websocket support
    from app import socketio
    socketio.run(app, debug=True, port=5000)
