# jobflow_cloud/models.py
import uuid
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


def gen_uuid() -> str:
    return str(uuid.uuid4())


def gen_api_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64-char token


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    plan = db.Column(db.String(50), default="free")
    api_token = db.Column(db.String(128), unique=True, nullable=False, default=gen_api_token)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions = db.relationship("Session", backref="user", lazy=True)
    customers = db.relationship("Customer", backref="user", lazy=True)

    def get_id(self):
        return self.id


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)

    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    email = db.Column(db.String(255))
    address_line1 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    state = db.Column(db.String(50))
    postal_code = db.Column(db.String(20))
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions = db.relationship("Session", backref="customer", lazy=True)


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.String(64), primary_key=True)  # we keep your string session_id
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    customer_id = db.Column(db.String(36), db.ForeignKey("customers.id"))

    title = db.Column(db.String(255))
    cloud_path = db.Column(db.String(500))
    meta_json_path = db.Column(db.String(500))
    report_json_path = db.Column(db.String(500))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
