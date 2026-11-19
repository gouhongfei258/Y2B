"""Flask application factory."""
import os

from flask import Flask

from y2b.utils import locate_bundled_dir

_BLUEPRINTS = (
    "auth",
    "dashboard",
    "tasks",
    "settings",
    "upload_templates",
    "monitor",
    "cookies_api",
)

_ready = False
app = None


def frontend_dir(kind: str) -> str:
    """Locate frontend/templates, frontend/static, or frontend/fonts in dev, Docker, and frozen builds."""
    return locate_bundled_dir("frontend", kind)


def _load_source(filename: str) -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    exec(compile(source, path, "exec"), globals())


def create_app():
    """Create the Flask app, load helpers, and register blueprints once."""
    global app, _ready
    if _ready:
        return app
    app = Flask(
        __name__,
        template_folder=frontend_dir("templates"),
        static_folder=frontend_dir("static"),
        static_url_path="/static",
    )
    _load_source("services.py")
    for name in _BLUEPRINTS:
        _load_source(os.path.join("blueprints", name + ".py"))
    _ready = True
    return app


create_app()
