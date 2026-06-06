import os
import re
import secrets
import string
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_pymongo import PyMongo

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev_secret_key")
app.config["MONGO_URI"] = os.environ.get(
    "MONGO_URI", "mongodb://localhost:27017/device_registry"
)

DEFAULT_MQTT_BROKER = os.environ.get("DEFAULT_MQTT_BROKER", "47.104.248.242")
DEFAULT_MQTT_PORT = int(os.environ.get("DEFAULT_MQTT_PORT", "1883"))

DEVICE_ID_SUFFIX_LENGTH = 6   # characters after "dev_"
DEFAULT_PASSWORD_LENGTH = 32   # bytes of hex entropy for MQTT passwords

mongo = PyMongo(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_indexes():
    """Create unique indexes on chip_id and device_id."""
    mongo.db.devices.create_index("chip_id", unique=True)
    mongo.db.devices.create_index("device_id", unique=True)
    mongo.db.device_subsensor_groups.create_index("device_class", unique=True)
    mongo.db.sensor_brands.create_index("brand", unique=True)
    mongo.db.sensor_profiles.create_index("sensor_key", unique=True)


def _generate_device_id():
    """Return a unique dev_XXXXXX identifier."""
    alphabet = string.ascii_lowercase + string.digits
    while True:
        candidate = "dev_" + "".join(secrets.choice(alphabet) for _ in range(DEVICE_ID_SUFFIX_LENGTH))
        if not mongo.db.devices.find_one({"device_id": candidate}):
            return candidate


def _generate_password():
    return secrets.token_hex(DEFAULT_PASSWORD_LENGTH // 2)


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


def _normalize_device_class(raw_value):
    device_class = (raw_value or "").strip()
    if not device_class:
        raise ValueError("device_class is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", device_class):
        raise ValueError("device_class only allows letters, numbers, _, -, .")
    return device_class


def _normalize_sensor_brand(raw_value):
    brand = (raw_value or "").strip()
    if not brand:
        raise ValueError("brand is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", brand):
        raise ValueError("brand only allows letters, numbers, _, -, .")
    return brand


def _normalize_sensor_name(raw_value):
    sensor_name = (raw_value or "").strip()
    if not sensor_name:
        raise ValueError("sensor_name is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", sensor_name):
        raise ValueError("sensor_name only allows letters, numbers, _, -, .")
    return sensor_name


def _parse_attributes(raw_value):
    normalized = str(raw_value or "").replace("\n", ",").replace("，", ",")
    values = [v.strip() for v in normalized.split(",") if v.strip()]
    deduped = []
    seen = set()
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(value)
    if not deduped:
        raise ValueError("attributes are required")
    return deduped


def _parse_int_field(field_name, raw_value):
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")


def _validate_range_item(item, index_label=""):
    start = item.get("start")
    end = item.get("end")
    interval = item.get("interval")

    if start is None or end is None or interval is None:
        raise ValueError(f"{index_label}start/end/interval are required")
    if start < 0 or end < 0:
        raise ValueError(f"{index_label}start/end must be >= 0")
    if end < start:
        raise ValueError(f"{index_label}end must be >= start")
    if interval <= 0:
        raise ValueError(f"{index_label}interval must be > 0")

    span = end - start + 1
    if span % interval != 0:
        raise ValueError(
            f"{index_label}invalid segment: (end - start + 1) must be divisible by interval"
        )

    assignments = item.get("assignments", [])
    if assignments is None:
        assignments = []
    if not isinstance(assignments, list):
        raise ValueError(f"{index_label}assignments must be a list")

    slots = set()
    for assignment in assignments:
        slot = assignment.get("slot")
        if not isinstance(slot, int):
            raise ValueError(f"{index_label}assignment slot must be integer")
        if slot < 1 or slot > interval:
            raise ValueError(f"{index_label}assignment slot out of interval range")
        if slot in slots:
            raise ValueError(f"{index_label}duplicate assignment slot")
        slots.add(slot)


def _validate_ranges(ranges):
    if not ranges:
        return

    ordered = sorted(ranges, key=lambda x: x["start"])
    for idx, item in enumerate(ordered, start=1):
        _validate_range_item(item, index_label=f"range[{idx}] ")

    for idx in range(1, len(ordered)):
        prev = ordered[idx - 1]
        curr = ordered[idx]
        if curr["start"] <= prev["end"]:
            raise ValueError(
                "invalid segments: overlapping ranges are not allowed "
                f"({prev['start']}-{prev['end']} overlaps {curr['start']}-{curr['end']})"
            )


def _sorted_ranges(ranges):
    return sorted(ranges, key=lambda x: x["start"])


def _sorted_assignments(assignments):
    return sorted(assignments, key=lambda x: x["slot"])


def _build_sensor_key(brand, sensor_name):
    return f"{brand}:{sensor_name}"


def _find_group_and_range(device_class, range_id):
    normalized_class = _normalize_device_class(device_class)
    group = mongo.db.device_subsensor_groups.find_one({"device_class": normalized_class})
    if not group:
        abort(404)

    ranges = list(group.get("ranges", []))
    normalized_changed = False
    for item in ranges:
        if item.get("range_id") is None:
            item["range_id"] = str(ObjectId())
            normalized_changed = True
        if item.get("assignments") is None:
            item["assignments"] = []
            normalized_changed = True

    if normalized_changed:
        mongo.db.device_subsensor_groups.update_one(
            {"_id": group["_id"]},
            {
                "$set": {
                    "ranges": _sorted_ranges(ranges),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        group = mongo.db.device_subsensor_groups.find_one({"_id": group["_id"]})
        ranges = list(group.get("ranges", []))

    target = None
    for item in ranges:
        if item.get("range_id") == range_id:
            target = item
            break

    if not target:
        abort(404)

    target.setdefault("assignments", [])
    target["assignments"] = _sorted_assignments(target.get("assignments", []))
    return normalized_class, group, ranges, target


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
    if not product_type:
        return jsonify({"error": "product_type is required"}), 400

    try:
        product_type = _normalize_device_class(product_type)
    except ValueError:
        return jsonify({"error": "invalid_product_type"}), 400

    allowed_class = mongo.db.device_subsensor_groups.find_one({"device_class": product_type})
    if not allowed_class:
        return jsonify({"error": "unsupported_device_class"}), 403

    existing = mongo.db.devices.find_one({"chip_id": chip_id})

    if existing:
        existing_product_type = (existing.get("product_type") or "").strip()
        if existing_product_type and existing_product_type != product_type:
            return jsonify({"error": "device_class_mismatch"}), 403

        status = existing.get("status", "pending")
        if status == "active":
            update_fields = {"last_seen": datetime.now(timezone.utc)}
            if not existing_product_type:
                update_fields["product_type"] = product_type

            mongo.db.devices.update_one(
                {"_id": existing["_id"]},
                {"$set": update_fields},
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

        if not existing_product_type:
            mongo.db.devices.update_one(
                {"_id": existing["_id"]},
                {"$set": {"product_type": product_type}},
            )
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
# Web — Sensor Brand / Profile Catalog
# ---------------------------------------------------------------------------


@app.route("/sensor-profiles", methods=["GET"])
def sensor_profile_list():
    brands = list(mongo.db.sensor_brands.find().sort("brand", 1))
    sensor_profiles = list(mongo.db.sensor_profiles.find().sort([("brand", 1), ("sensor_name", 1)]))
    return render_template(
        "sensor_profiles.html",
        brands=brands,
        sensor_profiles=sensor_profiles,
    )


@app.route("/sensor-brands", methods=["POST"])
def sensor_brand_add():
    try:
        brand = _normalize_sensor_brand(request.form.get("brand"))
        mongo.db.sensor_brands.insert_one(
            {
                "brand": brand,
                "created_at": datetime.now(timezone.utc),
            }
        )
        flash("传感器品牌已新增", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        flash("品牌已存在", "error")

    return redirect(url_for("sensor_profile_list"))


@app.route("/sensor-brands/<brand>/delete", methods=["POST"])
def sensor_brand_delete(brand):
    try:
        normalized_brand = _normalize_sensor_brand(brand)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("sensor_profile_list"))

    used = mongo.db.sensor_profiles.find_one({"brand": normalized_brand})
    if used:
        flash("该品牌下仍有传感器定义，无法删除", "error")
        return redirect(url_for("sensor_profile_list"))

    result = mongo.db.sensor_brands.delete_one({"brand": normalized_brand})
    if result.deleted_count == 0:
        abort(404)

    flash("传感器品牌已删除", "success")
    return redirect(url_for("sensor_profile_list"))


@app.route("/sensor-profiles", methods=["POST"])
def sensor_profile_add():
    try:
        brand = _normalize_sensor_brand(request.form.get("brand"))
        sensor_name = _normalize_sensor_name(request.form.get("sensor_name"))
        attributes = _parse_attributes(request.form.get("attributes"))

        brand_doc = mongo.db.sensor_brands.find_one({"brand": brand})
        if not brand_doc:
            flash("品牌不存在，请先新增品牌", "error")
            return redirect(url_for("sensor_profile_list"))

        sensor_key = _build_sensor_key(brand, sensor_name)
        now = datetime.now(timezone.utc)
        mongo.db.sensor_profiles.insert_one(
            {
                "sensor_key": sensor_key,
                "brand": brand,
                "sensor_name": sensor_name,
                "attributes": attributes,
                "created_at": now,
                "updated_at": now,
            }
        )
        flash("传感器定义已新增", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        flash("同品牌下传感器名称已存在", "error")

    return redirect(url_for("sensor_profile_list"))


@app.route("/sensor-profiles/<sensor_key>/delete", methods=["POST"])
def sensor_profile_delete(sensor_key):
    used = mongo.db.device_subsensor_groups.find_one({"ranges.assignments.sensor_key": sensor_key})
    if used:
        flash("该传感器已被地址段使用，无法删除", "error")
        return redirect(url_for("sensor_profile_list"))

    result = mongo.db.sensor_profiles.delete_one({"sensor_key": sensor_key})
    if result.deleted_count == 0:
        abort(404)

    flash("传感器定义已删除", "success")
    return redirect(url_for("sensor_profile_list"))


# ---------------------------------------------------------------------------
# Web — Device Subsensor Group Address Table
# ---------------------------------------------------------------------------


@app.route("/subsensor-groups", methods=["GET"])
def subsensor_group_list():
    classes = list(mongo.db.device_subsensor_groups.find().sort("device_class", 1))
    for c in classes:
        normalized_ranges = []
        changed = False
        for r in c.get("ranges", []):
            r = dict(r)
            if r.get("range_id") is None:
                r["range_id"] = str(ObjectId())
                changed = True
            if r.get("assignments") is None:
                r["assignments"] = []
                changed = True
            r["assignments"] = _sorted_assignments(r.get("assignments", []))
            normalized_ranges.append(r)

        if changed:
            mongo.db.device_subsensor_groups.update_one(
                {"_id": c["_id"]},
                {
                    "$set": {
                        "ranges": _sorted_ranges(normalized_ranges),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
        c["ranges"] = _sorted_ranges(normalized_ranges)
    return render_template("subsensor_groups.html", classes=classes)


@app.route("/subsensor-groups", methods=["POST"])
def subsensor_group_add():
    try:
        device_class = _normalize_device_class(request.form.get("device_class"))
        new_range = {
            "range_id": str(ObjectId()),
            "start": _parse_int_field("start", request.form.get("start")),
            "end": _parse_int_field("end", request.form.get("end")),
            "interval": _parse_int_field("interval", request.form.get("interval")),
            "assignments": [],
        }
        _validate_range_item(new_range)

        existing = mongo.db.device_subsensor_groups.find_one({"device_class": device_class})
        now = datetime.now(timezone.utc)

        if existing:
            ranges = list(existing.get("ranges", []))
            ranges.append(new_range)
            _validate_ranges(ranges)
            mongo.db.device_subsensor_groups.update_one(
                {"_id": existing["_id"]},
                {"$set": {"ranges": _sorted_ranges(ranges), "updated_at": now}},
            )
        else:
            mongo.db.device_subsensor_groups.insert_one(
                {
                    "device_class": device_class,
                    "ranges": [new_range],
                    "created_at": now,
                    "updated_at": now,
                }
            )

        flash("地址段已保存", "success")
    except ValueError as exc:
        flash(str(exc), "error")

    return redirect(url_for("subsensor_group_list"))


@app.route("/subsensor-groups/<device_class>/<range_id>/update", methods=["POST"])
def subsensor_group_update(device_class, range_id):
    try:
        normalized_class, doc, ranges, target = _find_group_and_range(device_class, range_id)
        start = _parse_int_field("start", request.form.get("start"))
        end = _parse_int_field("end", request.form.get("end"))
        interval = _parse_int_field("interval", request.form.get("interval"))

        assignments = target.get("assignments", [])
        if len(assignments) > interval:
            raise ValueError("interval cannot be smaller than assigned sensor count")

        target["start"] = start
        target["end"] = end
        target["interval"] = interval
        _validate_ranges(ranges)

        mongo.db.device_subsensor_groups.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "ranges": _sorted_ranges(ranges),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        flash("地址段已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")

    return redirect(url_for("subsensor_group_list"))


@app.route("/subsensor-groups/<device_class>/<range_id>/configure", methods=["GET"])
def subsensor_group_configure(device_class, range_id):
    normalized_class, _, _, target = _find_group_and_range(device_class, range_id)
    sensor_profiles = list(mongo.db.sensor_profiles.find().sort([("brand", 1), ("sensor_name", 1)]))

    slot_map = {a["slot"]: a for a in target.get("assignments", []) if isinstance(a.get("slot"), int)}
    slots = []
    interval = target.get("interval", 0)
    for slot in range(1, interval + 1):
        current = slot_map.get(slot)
        slots.append(
            {
                "slot": slot,
                "sensor_key": current.get("sensor_key") if current else "",
                "attributes": current.get("attributes", []) if current else [],
            }
        )

    return render_template(
        "subsensor_range_config.html",
        device_class=normalized_class,
        target_range=target,
        slots=slots,
        sensor_profiles=sensor_profiles,
    )


@app.route("/subsensor-groups/<device_class>/<range_id>/configure", methods=["POST"])
def subsensor_group_configure_save(device_class, range_id):
    try:
        normalized_class, group, ranges, target = _find_group_and_range(device_class, range_id)
        sensor_profiles = list(mongo.db.sensor_profiles.find())
        sensor_map = {s["sensor_key"]: s for s in sensor_profiles}

        interval = target.get("interval", 0)
        assignments = []
        for slot in range(1, interval + 1):
            selected_key = (request.form.get(f"slot_{slot}") or "").strip()
            if not selected_key:
                continue
            sensor = sensor_map.get(selected_key)
            if not sensor:
                raise ValueError(f"slot {slot}: selected sensor does not exist")

            assignments.append(
                {
                    "slot": slot,
                    "sensor_key": sensor["sensor_key"],
                    "brand": sensor["brand"],
                    "sensor_name": sensor["sensor_name"],
                    "attributes": list(sensor.get("attributes", [])),
                }
            )

        target["assignments"] = _sorted_assignments(assignments)
        _validate_ranges(ranges)
        mongo.db.device_subsensor_groups.update_one(
            {"_id": group["_id"]},
            {
                "$set": {
                    "ranges": _sorted_ranges(ranges),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        flash("地址段传感器分配已保存", "success")
    except ValueError as exc:
        flash(str(exc), "error")

    return redirect(url_for("subsensor_group_configure", device_class=device_class, range_id=range_id))


@app.route("/subsensor-groups/<device_class>/<range_id>/delete", methods=["POST"])
def subsensor_group_delete_range(device_class, range_id):
    try:
        normalized_class = _normalize_device_class(device_class)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("subsensor_group_list"))

    doc = mongo.db.device_subsensor_groups.find_one({"device_class": normalized_class})
    if not doc:
        abort(404)

    ranges = [r for r in doc.get("ranges", []) if r.get("range_id") != range_id]
    if len(ranges) == len(doc.get("ranges", [])):
        abort(404)

    if ranges:
        mongo.db.device_subsensor_groups.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "ranges": _sorted_ranges(ranges),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
    else:
        mongo.db.device_subsensor_groups.delete_one({"_id": doc["_id"]})

    flash("地址段已删除", "success")
    return redirect(url_for("subsensor_group_list"))


@app.route("/subsensor-groups/<device_class>/delete", methods=["POST"])
def subsensor_group_delete_class(device_class):
    try:
        normalized_class = _normalize_device_class(device_class)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("subsensor_group_list"))

    result = mongo.db.device_subsensor_groups.delete_one({"device_class": normalized_class})
    if result.deleted_count == 0:
        abort(404)
    flash("设备类型地址表已删除", "success")
    return redirect(url_for("subsensor_group_list"))


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
# Web — Disable / Delete Device
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


@app.route("/devices/<device_id>/delete", methods=["POST"])
def device_delete(device_id):
    result = mongo.db.devices.delete_one({"device_id": device_id})
    if result.deleted_count == 0:
        abort(404)
    app.logger.info("Device deleted: device_id=%s", device_id)
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
    # NOTE: Flask's built-in server is for development only.
    # For production use a WSGI server such as Gunicorn or uWSGI.
    app.run(host="0.0.0.0", port=8080, debug=False)
