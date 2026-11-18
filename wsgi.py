"""WSGI entry point.

A production server loads ``wsgi:app``. Importing this module also runs the
same one-time startup as ``python app.py``.
"""
from app import app, initialize_application

initialize_application()
