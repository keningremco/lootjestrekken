import os
import random
import secrets
import time
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt in .env")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD ontbreekt in .env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)


# ---------------------------------------------------------------- modellen
class Group(db.Model):
    __tablename__ = "draw_groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    drawn = db.Column(db.Boolean, nullable=False, default=False)  # lootjes zijn verdeeld

    members = db.relationship(
        "Participant",
        back_populates="group",
        foreign_keys="Participant.group_id",
        order_by="Participant.name",
    )


class Participant(db.Model):
    __tablename__ = "participants"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(6), nullable=False, unique=True)
    wish = db.Column(db.Text, nullable=False, default="")
    revealed = db.Column(db.Boolean, nullable=False, default=False)
    group_id = db.Column(db.Integer, db.ForeignKey("draw_groups.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("participants.id"), nullable=True)

    group = db.relationship("Group", back_populates="members", foreign_keys=[group_id])
    target = db.relationship("Participant", remote_side=[id], foreign_keys=[target_id])


def ensure_tables():
    """Controleert of alle tabellen bestaan en maakt de ontbrekende aan."""
    with app.app_context():
        try:
            wanted = set(db.metadata.tables.keys())
            present = set(inspect(db.engine).get_table_names())
        except Exception as e:
            raise SystemExit(f"Geen verbinding met de database: {e}")

        missing = sorted(wanted - present)
        if missing:
            print(f"Ontbrekende tabellen: {', '.join(missing)} -> aanmaken")
            db.create_all()
        else:
            print(f"Database OK, alle tabellen aanwezig: {', '.join(sorted(wanted))}")


ensure_tables()


# ---------------------------------------------------------------- helpers
def new_code():
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not Participant.query.filter_by(code=code).first():
            return code


def make_assignments(group):
    """Zet de leden in willekeurige volgorde in één cirkel: iedereen trekt de volgende.
    Zo trekt niemand zichzelf en loopt de trekking nooit vast."""
    members = list(group.members)
    random.SystemRandom().shuffle(members)
    for i, member in enumerate(members):
        member.target_id = members[(i + 1) % len(members)].id
    group.drawn = True


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def current_user():
    uid = session.get("user_id")
    return db.session.get(Participant, uid) if uid else None


def user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            session.pop("user_id", None)
            return redirect(url_for("login"))
        return view(user, *args, **kwargs)

    return wrapped


# ---------------------------------------------------------------- deelnemers
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        user = Participant.query.filter_by(code=code).first() if code.isdigit() else None
        if user:
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("me"))
        time.sleep(1)  # remt het raden van codes af
        flash("Deze code klopt niet. Controleer de 6 cijfers en probeer opnieuw.", "error")
    elif current_user():
        return redirect(url_for("me"))
    return render_template("login.html")


@app.route("/mijn")
@user_required
def me(user):
    return render_template("me.html", user=user)


@app.post("/mijn/wens")
@user_required
def save_wish(user):
    user.wish = request.form.get("wish", "").strip()[:1000]
    db.session.commit()
    flash("Je verlanglijstje is opgeslagen.", "ok")
    return redirect(url_for("me"))


@app.post("/mijn/trek")
@user_required
def draw(user):
    # groep vergrendelen zodat twee gelijktijdige trekkers niet allebei verdelen
    group = (
        db.session.query(Group).filter_by(id=user.group_id).with_for_update().one()
    )
    if not group.drawn:
        if len(group.members) < 2:
            flash("Er zijn minimaal 2 deelnemers in de groep nodig om te trekken.", "error")
            return redirect(url_for("me"))
        make_assignments(group)
    user.revealed = True
    db.session.commit()
    return redirect(url_for("me"))


@app.post("/uitloggen")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- beheer
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin"))
        time.sleep(1)
        flash("Verkeerd wachtwoord.", "error")
    return render_template("admin_login.html")


@app.route("/admin")
@admin_required
def admin():
    # let op: target_id wordt hier bewust nergens opgehaald of getoond
    groups = Group.query.order_by(Group.name).all()
    return render_template("admin.html", groups=groups)


@app.post("/admin/groep")
@admin_required
def add_group():
    name = request.form.get("name", "").strip()[:100]
    if name:
        db.session.add(Group(name=name))
        db.session.commit()
    return redirect(url_for("admin"))


@app.post("/admin/groep/<int:group_id>/verwijder")
@admin_required
def delete_group(group_id):
    group = db.get_or_404(Group, group_id)
    for member in group.members:
        member.target_id = None
    db.session.flush()
    for member in group.members:
        db.session.delete(member)
    db.session.delete(group)
    db.session.commit()
    return redirect(url_for("admin"))


@app.post("/admin/groep/<int:group_id>/reset")
@admin_required
def reset_group(group_id):
    group = db.get_or_404(Group, group_id)
    for member in group.members:
        member.target_id = None
        member.revealed = False
    group.drawn = False
    db.session.commit()
    flash(f"De trekking van “{group.name}” is gereset.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/gebruiker")
@admin_required
def add_user():
    name = request.form.get("name", "").strip()[:100]
    group = db.get_or_404(Group, request.form.get("group_id", type=int))
    if not name:
        flash("Vul een naam in.", "error")
    elif group.drawn:
        flash("Er is al getrokken in deze groep. Reset eerst de trekking om iemand toe te voegen.", "error")
    else:
        db.session.add(Participant(name=name, code=new_code(), group_id=group.id))
        db.session.commit()
    return redirect(url_for("admin"))


@app.post("/admin/gebruiker/<int:user_id>/verwijder")
@admin_required
def delete_user(user_id):
    user = db.get_or_404(Participant, user_id)
    if user.group.drawn:
        flash("Er is al getrokken in deze groep. Reset eerst de trekking om iemand te verwijderen.", "error")
    else:
        db.session.delete(user)
        db.session.commit()
    return redirect(url_for("admin"))


if __name__ == '__main__':
    app.run(port=7901)
