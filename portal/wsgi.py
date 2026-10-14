# -*- coding: utf-8 -*-
"""Production entrypoint for the portal.

This exists so that the department's app.py does not have to be edited. app.py
is vendored byte for byte from the handout; everything we added lives beside
it. Importing `app` runs app.py exactly as shipped -- including the line at the
bottom of it that registers the OAuth blueprint -- and then we register our own
machine-facing API on the same Flask application.

Run it with:
    gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 wsgi:app
"""

from app import app          # noqa: F401  (the department's portal, unmodified)
from api import api
from evals import evals

app.register_blueprint(api)
app.register_blueprint(evals)

# `gunicorn wsgi:app` resolves this name.
application = app
