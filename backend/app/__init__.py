"""Mini-Cypris API: a small Flask service in front of the CORE research-paper API."""
import os
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from . import db
from .routes import api

VERSION = "0.1.0"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "cache.db"


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    app.config.from_mapping(
        VERSION=VERSION,
        DATABASE=os.environ.get("DATABASE_PATH", str(DEFAULT_DB)),
        FRONTEND_DIST=os.environ.get("FRONTEND_DIST"),  # set in the container
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    app.register_blueprint(api, url_prefix="/api")

    dist = app.config["FRONTEND_DIST"]
    if dist:
        # Production: Flask also serves the built Angular app (same origin, no CORS).
        @app.get("/")
        @app.get("/<path:path>")
        def spa(path="index.html"):
            if path.startswith("api/"):
                abort(404)  # unknown API routes stay JSON 404s, never the app page
            if (Path(dist) / path).is_file():
                return send_from_directory(dist, path)
            return send_from_directory(dist, "index.html")

    @app.errorhandler(404)
    def not_found(_err):
        if request.path.startswith("/api/"):
            return jsonify(error="not_found", path=request.path), 404
        return "Not found", 404

    return app
