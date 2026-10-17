"""
Community Borewell & Water Tanker Scheduling System
Python Flask REST API + MySQL

Install:
    pip install flask flask-cors mysql-connector-python pyjwt werkzeug

Create a MySQL database first:
    CREATE DATABASE community_water;

Set environment variables if desired:
    DB_HOST=localhost
    DB_PORT=3306
    DB_USER=root
    DB_PASSWORD=your_password
    DB_NAME=community_water
    JWT_SECRET=change-this-secret

Run:
    python app.py
"""

import os
from datetime import datetime, date, timedelta, time
from functools import wraps

import jwt
import mysql.connector
from mysql.connector import Error
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "community_water"),
}

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-in-production")
JWT_ALGORITHM = "HS256"


def get_db():
    if "db" not in g:
        g.db = mysql.connector.connect(**DB_CONFIG)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db and db.is_connected():
        db.close()


def query(sql, params=(), fetchone=False, commit=False):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        if commit:
            db.commit()
            return cursor.lastrowid
        return cursor.fetchone() if fetchone else cursor.fetchall()
    finally:
        cursor.close()


def execute_many(sql, rows):
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.executemany(sql, rows)
        db.commit()
    finally:
        cursor.close()


def init_db():
    db = get_db()
    cursor = db.cursor()

    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            phone VARCHAR(20) NOT NULL UNIQUE,
            email VARCHAR(150) UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            role ENUM('RESIDENT','ADMIN','OPERATOR') NOT NULL DEFAULT 'RESIDENT',
            address VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS borewells (
            borewell_id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            location VARCHAR(255),
            capacity DECIMAL(12,2) NOT NULL,
            threshold_low DECIMAL(5,2) DEFAULT 40,
            threshold_critical DECIMAL(5,2) DEFAULT 20,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS water_level_readings (
            reading_id INT AUTO_INCREMENT PRIMARY KEY,
            borewell_id INT NOT NULL,
            level_percent DECIMAL(5,2) NOT NULL,
            level_cm DECIMAL(10,2),
            status ENUM('HIGH','MEDIUM','LOW','CRITICAL') NOT NULL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (borewell_id) REFERENCES borewells(borewell_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tankers (
            tanker_id INT AUTO_INCREMENT PRIMARY KEY,
            vehicle_no VARCHAR(50) NOT NULL UNIQUE,
            capacity_litres INT NOT NULL,
            operator_id INT,
            status ENUM('AVAILABLE','ASSIGNED','OUT_FOR_DELIVERY','MAINTENANCE')
                DEFAULT 'AVAILABLE',
            FOREIGN KEY (operator_id) REFERENCES users(user_id)
                ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tanker_requests (
            request_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            quantity_litres INT NOT NULL,
            requested_date DATE NOT NULL,
            preferred_time TIME,
            delivery_address VARCHAR(255) NOT NULL,
            priority ENUM('NORMAL','HIGH','URGENT') DEFAULT 'NORMAL',
            reason VARCHAR(255),
            status ENUM(
                'PENDING','APPROVED','REJECTED','SCHEDULED',
                'ASSIGNED','OUT_FOR_DELIVERY','DELIVERED',
                'CANCELLED','WAITING_FOR_ASSIGNMENT'
            ) DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedules (
            schedule_id INT AUTO_INCREMENT PRIMARY KEY,
            request_id INT NOT NULL,
            tanker_id INT NOT NULL,
            scheduled_date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            status ENUM(
                'SCHEDULED','ASSIGNED','OUT_FOR_DELIVERY','DELIVERED','CANCELLED'
            ) DEFAULT 'SCHEDULED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_request_schedule (request_id),
            FOREIGN KEY (request_id) REFERENCES tanker_requests(request_id)
                ON DELETE CASCADE,
            FOREIGN KEY (tanker_id) REFERENCES tankers(tanker_id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            title VARCHAR(150) NOT NULL,
            message VARCHAR(500) NOT NULL,
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT,
            action VARCHAR(100) NOT NULL,
            entity VARCHAR(100) NOT NULL,
            entity_id INT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
                ON DELETE SET NULL
        )
        """
    ]

    for statement in statements:
        cursor.execute(statement)

    cursor.execute("SELECT user_id FROM users WHERE email = %s", ("admin@community.com",))
    if not cursor.fetchone():
        cursor.execute(
            """
            INSERT INTO users
            (name, phone, email, password_hash, role, address)
            VALUES (%s,%s,%s,%s,'ADMIN',%s)
            """,
            (
                "System Administrator",
                "9999999999",
                "admin@community.com",
                generate_password_hash("Admin@123"),
                "Community Office",
            ),
        )

    cursor.execute("SELECT borewell_id FROM borewells LIMIT 1")
    if not cursor.fetchone():
        cursor.execute(
            """
            INSERT INTO borewells
            (name, location, capacity, threshold_low, threshold_critical)
            VALUES (%s,%s,%s,%s,%s)
            """,
            ("Main Community Borewell", "Community Main Block", 10000, 40, 20),
        )

    db.commit()
    cursor.close()


def status_from_level(level):
    if level > 70:
        return "HIGH"
    if level >= 40:
        return "MEDIUM"
    if level >= 20:
        return "LOW"
    return "CRITICAL"


def create_token(user):
    payload = {
        "user_id": user["user_id"],
        "role": user["role"],
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def auth_required(roles=None):
    roles = roles or []

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return jsonify({"error": "Authorization token required"}), 401

            token = header.split(" ", 1)[1]

            try:
                payload = jwt.decode(
                    token, JWT_SECRET, algorithms=[JWT_ALGORITHM]
                )
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token has expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401

            user = query(
                "SELECT * FROM users WHERE user_id=%s",
                (payload["user_id"],),
                fetchone=True,
            )

            if not user:
                return jsonify({"error": "User not found"}), 401

            if roles and user["role"] not in roles:
                return jsonify({"error": "Access denied"}), 403

            g.current_user = user
            return fn(*args, **kwargs)

        return wrapper
    return decorator


def audit(action, entity, entity_id=None, details=None):
    user_id = getattr(g, "current_user", {}).get("user_id")
    query(
        """
        INSERT INTO audit_logs (user_id, action, entity, entity_id, details)
        VALUES (%s,%s,%s,%s,%s)
        """,
        (user_id, action, entity, entity_id, details),
        commit=True,
    )


def notify(user_id, title, message):
    query(
        """
        INSERT INTO notifications (user_id, title, message)
        VALUES (%s,%s,%s)
        """,
        (user_id, title, message),
        commit=True,
    )


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return None


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "OK", "service": "Community Water Scheduling API"})


# ---------------- AUTHENTICATION ----------------

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    email = str(data.get("email", "")).strip() or None
    password = str(data.get("password", ""))
    address = str(data.get("address", "")).strip()

    if not name or not phone or not password or not address:
        return jsonify({
            "error": "name, phone, password and address are required"
        }), 400

    if len(password) < 6:
        return jsonify({"error": "Password must contain at least 6 characters"}), 400

    exists = query(
        "SELECT user_id FROM users WHERE phone=%s OR (email IS NOT NULL AND email=%s)",
        (phone, email),
        fetchone=True,
    )
    if exists:
        return jsonify({"error": "Phone or email already registered"}), 409

    user_id = query(
        """
        INSERT INTO users
        (name, phone, email, password_hash, role, address)
        VALUES (%s,%s,%s,%s,'RESIDENT',%s)
        """,
        (
            name,
            phone,
            email,
            generate_password_hash(password),
            address,
        ),
        commit=True,
    )

    return jsonify({
        "message": "Resident registered successfully",
        "user_id": user_id
    }), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    login_value = str(data.get("email_or_phone", "")).strip()
    password = str(data.get("password", ""))

    user = query(
        """
        SELECT * FROM users
        WHERE email=%s OR phone=%s
        """,
        (login_value, login_value),
        fetchone=True,
    )

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email/phone or password"}), 401

    token = create_token(user)

    return jsonify({
        "message": "Login successful",
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "name": user["name"],
            "email": user["email"],
            "phone": user["phone"],
            "role": user["role"],
            "address": user["address"],
        }
    })


# ---------------- BOREWELL ----------------

@app.route("/api/borewell/current", methods=["GET"])
def current_borewell():
    borewell_id = request.args.get("borewell_id", type=int)

    if borewell_id:
        reading = query(
            """
            SELECT r.*, b.name AS borewell_name, b.location
            FROM water_level_readings r
            JOIN borewells b ON b.borewell_id=r.borewell_id
            WHERE r.borewell_id=%s
            ORDER BY r.recorded_at DESC
            LIMIT 1
            """,
            (borewell_id,),
            fetchone=True,
        )
    else:
        reading = query(
            """
            SELECT r.*, b.name AS borewell_name, b.location
            FROM water_level_readings r
            JOIN borewells b ON b.borewell_id=r.borewell_id
            ORDER BY r.recorded_at DESC
            LIMIT 1
            """,
            fetchone=True,
        )

    if not reading:
        return jsonify({"message": "No water-level reading available"}), 404

    return jsonify(reading)


@app.route("/api/borewell/history", methods=["GET"])
def borewell_history():
    borewell_id = request.args.get("borewell_id", type=int)
    limit = min(request.args.get("limit", 100, type=int), 500)

    if borewell_id:
        rows = query(
            """
            SELECT * FROM water_level_readings
            WHERE borewell_id=%s
            ORDER BY recorded_at DESC
            LIMIT %s
            """,
            (borewell_id, limit),
        )
    else:
        rows = query(
            """
            SELECT * FROM water_level_readings
            ORDER BY recorded_at DESC
            LIMIT %s
            """,
            (limit,),
        )

    return jsonify(rows)


@app.route("/api/borewell/readings", methods=["POST"])
def receive_reading():
    data = request.get_json(silent=True) or {}

    borewell_id = data.get("borewellId")
    level_percent = data.get("levelPercent")
    level_cm = data.get("levelCm")

    try:
        borewell_id = int(borewell_id)
        level_percent = float(level_percent)
        level_cm = float(level_cm) if level_cm is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid sensor payload"}), 400

    if not 0 <= level_percent <= 100:
        return jsonify({"error": "levelPercent must be between 0 and 100"}), 400

    borewell = query(
        "SELECT * FROM borewells WHERE borewell_id=%s",
        (borewell_id,),
        fetchone=True,
    )
    if not borewell:
        return jsonify({"error": "Borewell not found"}), 404

    status = status_from_level(level_percent)

    reading_id = query(
        """
        INSERT INTO water_level_readings
        (borewell_id, level_percent, level_cm, status)
        VALUES (%s,%s,%s,%s)
        """,
        (borewell_id, level_percent, level_cm, status),
        commit=True,
    )

    # Create an alert for administrators when the level becomes LOW/CRITICAL.
    if status in ("LOW", "CRITICAL"):
        admins = query("SELECT user_id FROM users WHERE role='ADMIN'")
        title = f"Borewell Level {status}"
        message = (
            f"{borewell['name']} water level is {level_percent:.1f}% "
            f"({status})."
        )
        for admin in admins:
            notify(admin["user_id"], title, message)

    return jsonify({
        "message": "Reading stored",
        "reading_id": reading_id,
        "level_percent": level_percent,
        "status": status,
    }), 201


# ---------------- TANKER REQUESTS ----------------

@app.route("/api/tankers/requests", methods=["POST"])
@auth_required(["RESIDENT", "ADMIN"])
def create_request():
    data = request.get_json(silent=True) or {}

    try:
        quantity = int(data.get("quantity_litres"))
    except (TypeError, ValueError):
        return jsonify({"error": "quantity_litres must be a number"}), 400

    requested_date = parse_date(data.get("requested_date"))
    preferred_time = parse_time(data.get("preferred_time"))

    if quantity <= 0:
        return jsonify({"error": "Quantity must be greater than zero"}), 400
    if not requested_date:
        return jsonify({"error": "requested_date must use YYYY-MM-DD"}), 400

    address = str(data.get("delivery_address", "")).strip()
    if not address:
        address = g.current_user.get("address") or ""

    if not address:
        return jsonify({"error": "delivery_address is required"}), 400

    priority = str(data.get("priority", "NORMAL")).upper()
    if priority not in ("NORMAL", "HIGH", "URGENT"):
        return jsonify({"error": "Invalid priority"}), 400

    request_id = query(
        """
        INSERT INTO tanker_requests
        (user_id, quantity_litres, requested_date, preferred_time,
         delivery_address, priority, reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            g.current_user["user_id"],
            quantity,
            requested_date,
            preferred_time,
            address,
            priority,
            data.get("reason"),
        ),
        commit=True,
    )

    notify(
        g.current_user["user_id"],
        "Tanker Request Submitted",
        f"Your tanker request #{request_id} has been submitted.",
    )
    audit("CREATE", "tanker_request", request_id)

    return jsonify({
        "message": "Tanker request created",
        "request_id": request_id,
        "status": "PENDING",
    }), 201


@app.route("/api/tankers/requests", methods=["GET"])
@auth_required(["RESIDENT", "ADMIN", "OPERATOR"])
def list_requests():
    status = request.args.get("status")
    user = g.current_user

    sql = """
        SELECT tr.*, u.name AS resident_name, u.phone AS resident_phone
        FROM tanker_requests tr
        JOIN users u ON u.user_id=tr.user_id
    """
    conditions = []
    params = []

    if user["role"] == "RESIDENT":
        conditions.append("tr.user_id=%s")
        params.append(user["user_id"])

    if status:
        conditions.append("tr.status=%s")
        params.append(status.upper())

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY tr.created_at DESC"

    return jsonify(query(sql, tuple(params)))


@app.route("/api/tankers/requests/<int:request_id>/status", methods=["PUT"])
@auth_required(["ADMIN"])
def update_request_status(request_id):
    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status", "")).upper()

    allowed = {"APPROVED", "REJECTED", "CANCELLED", "WAITING_FOR_ASSIGNMENT"}
    if new_status not in allowed:
        return jsonify({"error": f"Status must be one of {sorted(allowed)}"}), 400

    req = query(
        "SELECT * FROM tanker_requests WHERE request_id=%s",
        (request_id,),
        fetchone=True,
    )
    if not req:
        return jsonify({"error": "Request not found"}), 404

    query(
        "UPDATE tanker_requests SET status=%s WHERE request_id=%s",
        (new_status, request_id),
        commit=True,
    )

    title = "Tanker Request Updated"
    message = f"Request #{request_id} status changed to {new_status}."
    notify(req["user_id"], title, message)
    audit("UPDATE_STATUS", "tanker_request", request_id, new_status)

    return jsonify({
        "message": "Request status updated",
        "request_id": request_id,
        "status": new_status,
    })


# ---------------- TANKERS / SCHEDULING ----------------

@app.route("/api/tankers", methods=["GET"])
@auth_required(["ADMIN", "OPERATOR"])
def list_tankers():
    return jsonify(query("""
        SELECT t.*, u.name AS operator_name, u.phone AS operator_phone
        FROM tankers t
        LEFT JOIN users u ON u.user_id=t.operator_id
        ORDER BY t.tanker_id
    """))


@app.route("/api/tankers", methods=["POST"])
@auth_required(["ADMIN"])
def create_tanker():
    data = request.get_json(silent=True) or {}

    vehicle_no = str(data.get("vehicle_no", "")).strip()
    try:
        capacity = int(data.get("capacity_litres"))
    except (TypeError, ValueError):
        return jsonify({"error": "capacity_litres must be a number"}), 400

    operator_id = data.get("operator_id")

    if not vehicle_no or capacity <= 0:
        return jsonify({"error": "vehicle_no and valid capacity are required"}), 400

    if operator_id:
        operator = query(
            "SELECT user_id FROM users WHERE user_id=%s AND role='OPERATOR'",
            (operator_id,),
            fetchone=True,
        )
        if not operator:
            return jsonify({"error": "Invalid tanker operator"}), 400

    try:
        tanker_id = query(
            """
            INSERT INTO tankers
            (vehicle_no, capacity_litres, operator_id, status)
            VALUES (%s,%s,%s,'AVAILABLE')
            """,
            (vehicle_no, capacity, operator_id),
            commit=True,
        )
    except Error as e:
        return jsonify({"error": str(e)}), 400

    audit("CREATE", "tanker", tanker_id)

    return jsonify({
        "message": "Tanker created",
        "tanker_id": tanker_id
    }), 201


def time_ranges_overlap(start1, end1, start2, end2):
    return start1 < end2 and start2 < end1


@app.route("/api/schedules", methods=["POST"])
@auth_required(["ADMIN"])
def create_schedule():
    data = request.get_json(silent=True) or {}

    try:
        request_id = int(data.get("request_id"))
        tanker_id = int(data.get("tanker_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "request_id and tanker_id are required"}), 400

    scheduled_date = parse_date(data.get("scheduled_date"))
    start_time = parse_time(data.get("start_time"))
    end_time = parse_time(data.get("end_time"))

    if not scheduled_date or not start_time or not end_time:
        return jsonify({
            "error": "scheduled_date, start_time and end_time are required"
        }), 400

    if start_time >= end_time:
        return jsonify({"error": "end_time must be after start_time"}), 400

    req = query(
        "SELECT * FROM tanker_requests WHERE request_id=%s",
        (request_id,),
        fetchone=True,
    )
    tanker = query(
        "SELECT * FROM tankers WHERE tanker_id=%s",
        (tanker_id,),
        fetchone=True,
    )

    if not req:
        return jsonify({"error": "Tanker request not found"}), 404
    if not tanker:
        return jsonify({"error": "Tanker not found"}), 404

    if req["status"] not in ("APPROVED", "WAITING_FOR_ASSIGNMENT"):
        return jsonify({
            "error": "Only approved/waiting requests can be scheduled"
        }), 400

    if req["quantity_litres"] > tanker["capacity_litres"]:
        return jsonify({
            "error": "Tanker capacity is insufficient for this request"
        }), 400

    # Prevent two schedules for the same tanker from overlapping.
    existing = query(
        """
        SELECT s.schedule_id, s.start_time, s.end_time
        FROM schedules s
        WHERE s.tanker_id=%s
          AND s.scheduled_date=%s
          AND s.status NOT IN ('DELIVERED','CANCELLED')
        """,
        (tanker_id, scheduled_date),
    )

    for row in existing:
        if time_ranges_overlap(
            start_time, end_time,
            row["start_time"], row["end_time"]
        ):
            return jsonify({
                "error": "Tanker is already scheduled during this time slot",
                "conflicting_schedule_id": row["schedule_id"],
            }), 409

    # Only one active schedule per request.
    old = query(
        """
        SELECT schedule_id FROM schedules
        WHERE request_id=%s
        """,
        (request_id,),
        fetchone=True,
    )
    if old:
        return jsonify({"error": "Request already has a schedule"}), 409

    schedule_id = query(
        """
        INSERT INTO schedules
        (request_id, tanker_id, scheduled_date, start_time, end_time, status)
        VALUES (%s,%s,%s,%s,%s,'SCHEDULED')
        """,
        (
            request_id,
            tanker_id,
            scheduled_date,
            start_time,
            end_time,
        ),
        commit=True,
    )

    query(
        """
        UPDATE tanker_requests
        SET status='SCHEDULED'
        WHERE request_id=%s
        """,
        (request_id,),
        commit=True,
    )

    if tanker["operator_id"]:
        notify(
            tanker["operator_id"],
            "New Tanker Assignment",
            f"Tanker {tanker['vehicle_no']} has been assigned to request #{request_id}.",
        )

    notify(
        req["user_id"],
        "Tanker Scheduled",
        f"Request #{request_id} has been scheduled.",
    )

    audit("CREATE", "schedule", schedule_id)

    return jsonify({
        "message": "Tanker schedule created",
        "schedule_id": schedule_id,
    }), 201


@app.route("/api/schedules", methods=["GET"])
@auth_required(["ADMIN", "OPERATOR", "RESIDENT"])
def list_schedules():
    user = g.current_user

    sql = """
        SELECT s.*, tr.user_id, tr.quantity_litres,
               tr.delivery_address, tr.priority,
               u.name AS resident_name,
               t.vehicle_no, t.capacity_litres,
               op.name AS operator_name
        FROM schedules s
        JOIN tanker_requests tr ON tr.request_id=s.request_id
        JOIN users u ON u.user_id=tr.user_id
        JOIN tankers t ON t.tanker_id=s.tanker_id
        LEFT JOIN users op ON op.user_id=t.operator_id
    """
    params = []
    conditions = []

    if user["role"] == "RESIDENT":
        conditions.append("tr.user_id=%s")
        params.append(user["user_id"])
    elif user["role"] == "OPERATOR":
        conditions.append("t.operator_id=%s")
        params.append(user["user_id"])

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY s.scheduled_date, s.start_time"

    return jsonify(query(sql, tuple(params)))


@app.route("/api/schedules/<int:schedule_id>/status", methods=["PUT"])
@auth_required(["ADMIN", "OPERATOR"])
def update_schedule_status(schedule_id):
    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status", "")).upper()

    allowed = {
        "SCHEDULED",
        "ASSIGNED",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
        "CANCELLED",
    }
    if new_status not in allowed:
        return jsonify({"error": "Invalid schedule status"}), 400

    schedule = query(
        """
        SELECT s.*, tr.user_id, t.operator_id, t.tanker_id
        FROM schedules s
        JOIN tanker_requests tr ON tr.request_id=s.request_id
        JOIN tankers t ON t.tanker_id=s.tanker_id
        WHERE s.schedule_id=%s
        """,
        (schedule_id,),
        fetchone=True,
    )

    if not schedule:
        return jsonify({"error": "Schedule not found"}), 404

    if (
        g.current_user["role"] == "OPERATOR"
        and schedule["operator_id"] != g.current_user["user_id"]
    ):
        return jsonify({"error": "This schedule is not assigned to you"}), 403

    query(
        "UPDATE schedules SET status=%s WHERE schedule_id=%s",
        (new_status, schedule_id),
        commit=True,
    )

    request_status = new_status
    query(
        """
        UPDATE tanker_requests
        SET status=%s
        WHERE request_id=%s
        """,
        (request_status, schedule["request_id"]),
        commit=True,
    )

    if new_status == "OUT_FOR_DELIVERY":
        query(
            """
            UPDATE tankers SET status='OUT_FOR_DELIVERY'
            WHERE tanker_id=%s
            """,
            (schedule["tanker_id"],),
            commit=True,
        )
    elif new_status == "DELIVERED":
        query(
            """
            UPDATE tankers SET status='AVAILABLE'
            WHERE tanker_id=%s
            """,
            (schedule["tanker_id"],),
            commit=True,
        )
    elif new_status == "ASSIGNED":
        query(
            """
            UPDATE tankers SET status='ASSIGNED'
            WHERE tanker_id=%s
            """,
            (schedule["tanker_id"],),
            commit=True,
        )

    notify(
        schedule["user_id"],
        "Delivery Status Updated",
        f"Your request #{schedule['request_id']} is now {new_status}.",
    )

    audit("UPDATE_STATUS", "schedule", schedule_id, new_status)

    return jsonify({
        "message": "Delivery status updated",
        "schedule_id": schedule_id,
        "status": new_status,
    })


# ---------------- NOTIFICATIONS ----------------

@app.route("/api/notifications", methods=["GET"])
@auth_required(["RESIDENT", "ADMIN", "OPERATOR"])
def notifications():
    rows = query(
        """
        SELECT * FROM notifications
        WHERE user_id=%s
        ORDER BY created_at DESC
        """,
        (g.current_user["user_id"],),
    )
    return jsonify(rows)


@app.route("/api/notifications/<int:notification_id>/read", methods=["PUT"])
@auth_required(["RESIDENT", "ADMIN", "OPERATOR"])
def mark_notification_read(notification_id):
    query(
        """
        UPDATE notifications
        SET is_read=TRUE
        WHERE notification_id=%s AND user_id=%s
        """,
        (notification_id, g.current_user["user_id"]),
        commit=True,
    )
    return jsonify({"message": "Notification marked as read"})


# ---------------- ADMIN REPORTS ----------------

@app.route("/api/reports/summary", methods=["GET"])
@auth_required(["ADMIN"])
def report_summary():
    counts = {}

    for status in [
        "PENDING",
        "APPROVED",
        "REJECTED",
        "SCHEDULED",
        "ASSIGNED",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
        "CANCELLED",
    ]:
        row = query(
            """
            SELECT COUNT(*) AS count
            FROM tanker_requests
            WHERE status=%s
            """,
            (status,),
            fetchone=True,
        )
        counts[status.lower()] = row["count"]

    today = date.today()

    today_requests = query(
        """
        SELECT COUNT(*) AS count
        FROM tanker_requests
        WHERE requested_date=%s
        """,
        (today,),
        fetchone=True,
    )["count"]

    today_deliveries = query(
        """
        SELECT COUNT(*) AS count
        FROM schedules
        WHERE scheduled_date=%s AND status='DELIVERED'
        """,
        (today,),
        fetchone=True,
    )["count"]

    latest_level = query(
        """
        SELECT level_percent, level_cm, status, recorded_at
        FROM water_level_readings
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        fetchone=True,
    )

    return jsonify({
        "today": str(today),
        "today_requests": today_requests,
        "today_completed_deliveries": today_deliveries,
        "requests_by_status": counts,
        "latest_borewell": latest_level,
    })


@app.route("/api/reports/water-levels", methods=["GET"])
@auth_required(["ADMIN"])
def report_water_levels():
    return jsonify(query(
        """
        SELECT r.reading_id, r.borewell_id, b.name AS borewell_name,
               r.level_percent, r.level_cm, r.status, r.recorded_at
        FROM water_level_readings r
        JOIN borewells b ON b.borewell_id=r.borewell_id
        ORDER BY r.recorded_at DESC
        LIMIT 500
        """
    ))


# ---------------- ERROR HANDLERS ----------------

@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request"}), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    with app.app_context():
        try:
            init_db()
            print("Database tables initialized.")
            print("Default admin:")
            print("  Email: admin@community.com")
            print("  Password: Admin@123")
        except Error as e:
            print("Database connection failed:", e)
            raise

    app.run(host="0.0.0.0", port=5000, debug=True)
