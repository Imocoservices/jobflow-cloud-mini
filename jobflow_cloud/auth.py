# jobflow_cloud/auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from .models import db, User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("sessions.sessions_view"))

    access_code_required = current_app.config.get("ACCESS_CODE") or ""

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        access_code = request.form.get("access_code", "").strip()

        if access_code_required and access_code != access_code_required:
            flash("Invalid access code.", "error")
            return render_template("auth/register.html")

        if not name or not email or not password:
            flash("Name, email, and password are required.", "error")
            return render_template("auth/register.html")

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("auth/register.html")

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("sessions.sessions_view"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html")

        login_user(user)
        return redirect(url_for("sessions.sessions_view"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
def logout():
    if current_user.is_authenticated:
        logout_user()
        flash("Logged out.", "success")
    return redirect(url_for("auth.login"))
