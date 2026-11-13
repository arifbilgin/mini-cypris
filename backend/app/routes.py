import time

from flask import Blueprint, current_app, request

from .db import get_db

api = Blueprint("api", __name__)


@api.get("/health")
def health():
    tables = [
        row["name"]
        for row in get_db().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {"status": "ok", "version": current_app.config["VERSION"], "db": "ok", "tables": tables}


@api.get("/hello")
def hello():
    name = (request.args.get("name") or "").strip()[:50] or "world"
    return {"message": f"Hello, {name}! This is Flask.", "name": name, "served_at": int(time.time())}
