from app import app

# WSGI entrypoint for production servers
# Example usage with waitress (Windows):
#   pip install waitress
#   waitress-serve --listen=127.0.0.1:5001 wsgi:app

if __name__ == '__main__':
    # fallback to simple run for local dev
    app.run(host='0.0.0.0', port=5001)
