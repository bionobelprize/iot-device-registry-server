import os
import secrets
import string
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from flask_pymongo import PyMongo

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev_secret_key")
app.config["MONGO_URI"] = os.environ.get(
    "MONGO_URI", "mongodb://localhost:27017/device_registry"
)

DEFAULT_MQTT_BROKER = os.environ.get("DEFAULT_MQTT_BROKER", "47.104.248.242")
DEFAULT_MQTT_PORT = int(os.environ.get("DEFAULT_MQTT_PORT", "1883"))

mongo = PyMongo(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_indexes():
    """Create unique indexes on chip_id and device_id."""
    mongo.db.devices.create_index("chip_id", unique=True)
    mongo.db.devices.create_index("device_id", unique=True)


def _generate_device_id():
    """Return a unique dev_XXXXXX identifier."""
    alphabet = string.ascii_lowercase + string.digits
    while True:
        candidate = "dev_" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not mongo.db.devices.find_one({"device_id": candidate}):
            return candidate


def _generate_password(length=32):
    return secrets.token_hex(length // 2)


def _serialize_device(doc):
    """Convert a MongoDB document to a JSON-safe dict."""
    if doc is None:
        return None
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    for field in ("registered_at", "last_seen", "assigned_at"):
        if doc.get(field):
            doc[field] = doc[field].isoformat()
    return doc


# ---------------------------------------------------------------------------
# API — Device Registration
# ---------------------------------------------------------------------------


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    chip_id = data.get("chip_id", "").strip()
    product_type = data.get("product_type", "").strip()

    if not chip_id:
        return jsonify({"error": "chip_id is required"}), 400

    existing = mongo.db.devices.find_one({"chip_id": chip_id})

    if existing:
        status = existing.get("status", "pending")
        if status == "active":
            mongo.db.devices.update_one(
                {"_id": existing["_id"]},
                {"$set": {"last_seen": datetime.now(timezone.utc)}},
            )
            sensor_ids = existing.get("sensor_ids", {})
            device_id = existing["device_id"]
            return jsonify(
                {
                    "device_id": device_id,
                    "mqtt_broker": existing.get("mqtt_broker", DEFAULT_MQTT_BROKER),
                    "mqtt_port": existing.get("mqtt_port", DEFAULT_MQTT_PORT),
                    "mqtt_username": device_id,
                    "mqtt_password": existing.get("mqtt_password", ""),
                    "telemetry_topic": f"agriculture/device/{device_id}/telemetry",
                    "region_id": existing.get("region_id", ""),
                    "sensor_ids": {
                        "co2_id": sensor_ids.get("co2_id", ""),
                        "light_id": sensor_ids.get("light_id", ""),
                        "air_temp_id": sensor_ids.get("air_temp_id", ""),
                        "air_humidity_id": sensor_ids.get("air_humidity_id", ""),
                    },
                }
            )
        if status == "disabled":
            return jsonify({"error": "device_disabled"}), 403
        # pending
        return jsonify({"status": "pending_approval"})

    # New device
    device_id = _generate_device_id()
    now = datetime.now(timezone.utc)
    doc = {
        "chip_id": chip_id,
        "device_id": device_id,
        "status": "pending",
        "mqtt_password": _generate_password(),
        "mqtt_broker": DEFAULT_MQTT_BROKER,
        "mqtt_port": DEFAULT_MQTT_PORT,
        "region_id": "",
        "sensor_ids": {
            "co2_id": "",
            "light_id": "",
            "air_temp_id": "",
            "air_humidity_id": "",
        },
        "registered_at": now,
        "last_seen": now,
        "assigned_by": "",
        "assigned_at": None,
        "product_type": product_type,
    }
    mongo.db.devices.insert_one(doc)
    app.logger.info("New device registered: chip_id=%s device_id=%s", chip_id, device_id)
    return jsonify({"status": "pending_approval", "device_id": device_id}), 201


# ---------------------------------------------------------------------------
# API — Device Config
# ---------------------------------------------------------------------------


@app.route("/api/devices/<device_id>/config", methods=["GET"])
def api_device_config(device_id):
    device = mongo.db.devices.find_one({"device_id": device_id})
    if not device:
        return jsonify({"error": "device not found"}), 404
    if device.get("status") != "active":
        return jsonify({"error": "device not active"}), 403

    sensor_ids = device.get("sensor_ids", {})
    return jsonify(
        {
            "device_id": device_id,
            "mqtt_broker": device.get("mqtt_broker", DEFAULT_MQTT_BROKER),
            "mqtt_port": device.get("mqtt_port", DEFAULT_MQTT_PORT),
            "mqtt_username": device_id,
            "mqtt_password": device.get("mqtt_password", ""),
            "telemetry_topic": f"agriculture/device/{device_id}/telemetry",
            "region_id": device.get("region_id", ""),
            "sensor_ids": {
                "co2_id": sensor_ids.get("co2_id", ""),
                "light_id": sensor_ids.get("light_id", ""),
                "air_temp_id": sensor_ids.get("air_temp_id", ""),
                "air_humidity_id": sensor_ids.get("air_humidity_id", ""),
            },
        }
    )


# ---------------------------------------------------------------------------
# Web — Dashboard
# ---------------------------------------------------------------------------


@app.route("/")
def dashboard():
    pipeline = [
        {
            "$group": {
                "_id": "$status",
                "count": {"$sum": 1},
            }
        }
    ]
    stats = {s["_id"]: s["count"] for s in mongo.db.devices.aggregate(pipeline)}
    total = sum(stats.values())
    return render_template(
        "index.html",
        total=total,
        pending=stats.get("pending", 0),
        active=stats.get("active", 0),
        disabled=stats.get("disabled", 0),
    )


# ---------------------------------------------------------------------------
# Web — Device List
# ---------------------------------------------------------------------------


@app.route("/devices")
def device_list():
    devices = list(mongo.db.devices.find().sort("registered_at", -1))
    return render_template("devices.html", devices=devices)


# ---------------------------------------------------------------------------
# Web — Device Detail
# ---------------------------------------------------------------------------


@app.route("/devices/<device_id>")
def device_detail(device_id):
    device = mongo.db.devices.find_one({"device_id": device_id})
    if not device:
        abort(404)
    return render_template("device_detail.html", device=device)


# ---------------------------------------------------------------------------
# Web — Assign / Edit Device
# ---------------------------------------------------------------------------


@app.route("/devices/<device_id>/assign", methods=["GET", "POST"])
def device_assign(device_id):
    device = mongo.db.devices.find_one({"device_id": device_id})
    if not device:
        abort(404)

    if request.method == "POST":
        reset_password = request.form.get("reset_password") == "on"
        sensor_ids = {
            "co2_id": request.form.get("co2_id", "").strip(),
            "light_id": request.form.get("light_id", "").strip(),
            "air_temp_id": request.form.get("air_temp_id", "").strip(),
            "air_humidity_id": request.form.get("air_humidity_id", "").strip(),
        }
        update_fields = {
            "region_id": request.form.get("region_id", "").strip(),
            "mqtt_broker": request.form.get("mqtt_broker", DEFAULT_MQTT_BROKER).strip(),
            "mqtt_port": int(request.form.get("mqtt_port", DEFAULT_MQTT_PORT)),
            "sensor_ids": sensor_ids,
            "assigned_by": request.form.get("assigned_by", "").strip(),
            "assigned_at": datetime.now(timezone.utc),
            "status": "active",
        }
        if reset_password:
            update_fields["mqtt_password"] = _generate_password()

        mongo.db.devices.update_one(
            {"device_id": device_id}, {"$set": update_fields}
        )
        app.logger.info("Device assigned/updated: device_id=%s", device_id)
        return redirect(url_for("device_detail", device_id=device_id))

    return render_template(
        "assign.html",
        device=device,
        default_broker=DEFAULT_MQTT_BROKER,
        default_port=DEFAULT_MQTT_PORT,
    )


# ---------------------------------------------------------------------------
# Web — Disable Device
# ---------------------------------------------------------------------------


@app.route("/devices/<device_id>/disable", methods=["POST"])
def device_disable(device_id):
    result = mongo.db.devices.update_one(
        {"device_id": device_id}, {"$set": {"status": "disabled"}}
    )
    if result.matched_count == 0:
        abort(404)
    app.logger.info("Device disabled: device_id=%s", device_id)
    return redirect(url_for("device_list"))


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------


@app.errorhandler(400)
def bad_request(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "bad request"}), 400
    return render_template("error.html", code=400, message="Bad Request"), 400


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("error.html", code=404, message="Page Not Found"), 404


@app.errorhandler(500)
def internal_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal server error"}), 500
    return render_template("error.html", code=500, message="Internal Server Error"), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


with app.app_context():
    try:
        _ensure_indexes()
    except Exception as exc:
        app.logger.warning("Could not create indexes at startup: %s", exc)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
