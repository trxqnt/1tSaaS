"""
Relancio - SaaS de relance automatique des factures impayees (France).

Un seul fichier : serveur web (Flask), base SQLite, moteur de relances,
paiement par abonnement (Stripe Payment Link + webhook), pages marketing.

Commandes utiles :
    python app.py                       -> lance le serveur de dev
    python app.py run-reminders         -> envoie les relances dues (a lancer 1x/jour)
    python app.py set-plan EMAIL pro    -> passe un compte en 'pro' ou 'free'
"""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from email.utils import formataddr
from functools import wraps
from urllib.parse import urlencode

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# Configuration (variables d'environnement)
# --------------------------------------------------------------------------
APP_NAME = os.environ.get("APP_NAME", "Relancio")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
DATABASE = os.environ.get("DATABASE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "relancio.db"))
SECRET_KEY = os.environ.get("SECRET_KEY")
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "relances@example.com")

STRIPE_PAYMENT_LINK = os.environ.get("STRIPE_PAYMENT_LINK", "")
STRIPE_PORTAL_LINK = os.environ.get("STRIPE_PORTAL_LINK", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

PRICE_LABEL = os.environ.get("PRICE_LABEL", "29 €")
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "3"))          # factures impayees suivies en gratuit
DEFAULT_PENALTY_RATE = float(os.environ.get("DEFAULT_PENALTY_RATE", "8.25"))  # 3 x taux legal S2 2026 (2,75 %)
INDEMNITY_CENTS = 4000  # indemnite forfaitaire de recouvrement : 40 EUR (B2B)

# Etapes de relance : (numero, libelle, jours apres l'echeance)
STEPS = [
    (1, "Rappel amical", 1),
    (2, "Relance ferme", 8),
    (3, "Dernier rappel", 20),
    (4, "Avant recouvrement", 35),
]
STEP_LABEL = {n: label for n, label, _ in STEPS}

if not SECRET_KEY:
    # En dev uniquement : cle aleatoire (les sessions sautent a chaque redemarrage).
    # En production, DEFINISSEZ SECRET_KEY.
    SECRET_KEY = secrets.token_hex(32)
    print("ATTENTION : SECRET_KEY non definie, cle temporaire utilisee.", file=sys.stderr)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# --------------------------------------------------------------------------
# Base de donnees
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT 'free',
    auto_enabled INTEGER NOT NULL DEFAULT 1,
    penalty_rate REAL NOT NULL DEFAULT 8.25,
    stripe_customer TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    UNIQUE(user_id, email)
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    number TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid',
    paid_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    step INTEGER NOT NULL,
    status TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(user_id, status);
CREATE INDEX IF NOT EXISTS idx_reminders_invoice ON reminders(invoice_id);
"""


def connect():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    db = connect()
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# Utilitaires metier
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def parse_amount(text):
    cleaned = (text or "").replace(" ", "").replace("\xa0", "").replace(" ", "").replace("€", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Montant invalide.")
    if value <= 0 or value > Decimal("100000000"):
        raise ValueError("Montant invalide.")
    return int((value * 100).to_integral_value(ROUND_HALF_UP))


def parse_date(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError("Date invalide.")


def format_eur(cents):
    s = f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def format_date(iso):
    try:
        return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


app.jinja_env.filters["eur"] = format_eur
app.jinja_env.filters["fr_date"] = format_date


def days_late(due_iso, today=None):
    today = today or date.today()
    return (today - date.fromisoformat(due_iso)).days


def step_for(late_days):
    current = 0
    for number, _label, threshold in STEPS:
        if late_days >= threshold:
            current = number
    return current


def penalties(amount_cents, rate, late_days):
    """Retourne (interets, indemnite forfaitaire) en centimes."""
    if late_days <= 0:
        return 0, 0
    interest = int(round(amount_cents * rate / 100 * late_days / 365))
    return interest, INDEMNITY_CENTS


def build_email(user, client, invoice, step, today=None):
    today = today or date.today()
    late = max(days_late(invoice["due_date"], today), 0)
    amount = invoice["amount_cents"]
    interest, indemnity = penalties(amount, user["penalty_rate"], late)
    total = amount + interest + indemnity
    sender = user["company"] or user["email"]
    ref = f"n° {invoice['number']}"
    due = format_date(invoice["due_date"])
    rate = f"{user['penalty_rate']:g}".replace(".", ",")

    greeting = f"Bonjour {client['name']},\n\n"
    signature = f"\nCordialement,\n{sender}"

    if step == 1:
        subject = f"Rappel : facture {ref} échue le {due}"
        body = (
            f"{greeting}"
            f"Sauf erreur de notre part, la facture {ref} d'un montant de {format_eur(amount)}, "
            f"échue le {due}, n'a pas encore été réglée.\n\n"
            "Il s'agit peut-être d'un simple oubli. Pourriez-vous procéder au règlement dès que possible ? "
            "Si le paiement a déjà été effectué, merci d'ignorer ce message.\n"
            f"{signature}"
        )
    elif step == 2:
        subject = f"Relance : facture {ref} en retard de {late} jours"
        body = (
            f"{greeting}"
            f"Malgré notre précédent rappel, la facture {ref} de {format_eur(amount)}, "
            f"échue le {due}, reste impayée ({late} jours de retard).\n\n"
            f"Des pénalités de retard au taux de {rate} % par an, ainsi qu'une indemnité forfaitaire "
            f"de recouvrement de {format_eur(INDEMNITY_CENTS)}, sont exigibles de plein droit en cas de retard de paiement.\n\n"
            "Merci de régulariser cette situation rapidement, ou de nous contacter si un point bloque le règlement.\n"
            f"{signature}"
        )
    elif step == 3:
        subject = f"Dernier rappel : facture {ref} impayée ({late} jours de retard)"
        body = (
            f"{greeting}"
            f"Sans réponse de votre part, la facture {ref} échue le {due} demeure impayée ({late} jours de retard).\n\n"
            "Détail des sommes dues à ce jour :\n"
            f"  - Principal : {format_eur(amount)}\n"
            f"  - Pénalités de retard ({rate} % par an) : {format_eur(interest)}\n"
            f"  - Indemnité forfaitaire de recouvrement : {format_eur(indemnity)}\n"
            f"  - Total : {format_eur(total)}\n\n"
            "Nous vous demandons de régler cette somme sous 8 jours.\n"
            f"{signature}"
        )
    else:
        subject = f"Dernière relance amiable avant recouvrement : facture {ref}"
        body = (
            f"{greeting}"
            f"Malgré nos relances successives, la facture {ref} échue le {due} reste impayée ({late} jours de retard).\n\n"
            f"Somme due à ce jour : {format_eur(total)} "
            f"(principal {format_eur(amount)}, pénalités {format_eur(interest)}, "
            f"indemnité de recouvrement {format_eur(indemnity)}).\n\n"
            "À défaut de règlement sous 8 jours, nous nous réservons le droit d'engager "
            "une procédure de recouvrement (notamment une injonction de payer), dont les frais pourront être "
            "mis à votre charge.\n"
            f"{signature}"
        )

    body += (
        f"\n\n--\nMessage envoyé automatiquement au nom de {sender} via {APP_NAME}. "
        f"Répondez directement à cet e-mail pour contacter {sender}."
    )
    return subject, body


def send_email(to_addr, subject, body, reply_to, from_name):
    """Retourne 'sent', 'dry_run' ou 'error'. Sans SMTP configure : mode simulation."""
    if not SMTP_HOST:
        print(f"[DRY-RUN] -> {to_addr} | {subject}")
        return "dry_run"
    msg = EmailMessage()
    msg["From"] = formataddr((f"{from_name} via {APP_NAME}", MAIL_FROM))
    msg["To"] = to_addr
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return "sent"
    except Exception as exc:  # noqa: BLE001
        print(f"[ERREUR SMTP] {exc}", file=sys.stderr)
        return "error"


def dispatch_reminder(db, user, client, invoice, step):
    subject, body = build_email(user, client, invoice, step)
    status = send_email(client["email"], subject, body, user["email"], user["company"] or user["email"])
    db.execute(
        "INSERT INTO reminders (invoice_id, step, status, subject, body, sent_at) VALUES (?,?,?,?,?,?)",
        (invoice["id"], step, status, subject, body, now_iso()),
    )
    db.commit()
    return status


def run_reminders(db=None):
    """Parcourt toutes les factures impayees et envoie la relance due. Retourne un resume."""
    own = db is None
    db = db or connect()
    rows = db.execute(
        """SELECT i.id AS iid FROM invoices i JOIN users u ON u.id = i.user_id
           WHERE i.status = 'unpaid' AND u.auto_enabled = 1"""
    ).fetchall()
    summary = {"checked": len(rows), "sent": 0, "dry_run": 0, "error": 0}
    for row in rows:
        invoice = db.execute("SELECT * FROM invoices WHERE id = ?", (row["iid"],)).fetchone()
        late = days_late(invoice["due_date"])
        step = step_for(late)
        if step == 0:
            continue
        already = db.execute(
            "SELECT 1 FROM reminders WHERE invoice_id = ? AND step >= ? AND status IN ('sent','dry_run')",
            (invoice["id"], step),
        ).fetchone()
        if already:
            continue
        user = db.execute("SELECT * FROM users WHERE id = ?", (invoice["user_id"],)).fetchone()
        client = db.execute("SELECT * FROM clients WHERE id = ?", (invoice["client_id"],)).fetchone()
        status = dispatch_reminder(db, user, client, invoice, step)
        summary[status] = summary.get(status, 0) + 1
    if own:
        db.close()
    return summary


# --------------------------------------------------------------------------
# Authentification & CSRF
# --------------------------------------------------------------------------
@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()


@app.before_request
def csrf_protect():
    if request.method == "POST" and request.endpoint not in ("stripe_webhook", "cron_run"):
        token = session.get("csrf")
        sent = request.form.get("csrf", "")
        if not token or not hmac.compare_digest(token, sent):
            abort(400, "Jeton de sécurité invalide. Rechargez la page.")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.context_processor
def inject_globals():
    return dict(csrf_token=csrf_token, app_name=APP_NAME, price_label=PRICE_LABEL,
                free_limit=FREE_LIMIT, step_label=STEP_LABEL)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# --------------------------------------------------------------------------
# Pages publiques
# --------------------------------------------------------------------------
@app.route("/")
def landing():
    if g.user:
        return redirect(url_for("dashboard"))
    return render_template("landing.html", steps=STEPS)


@app.route("/cgu")
def cgu():
    return render_template("legal.html", title="Conditions générales d'utilisation", kind="cgu")


@app.route("/confidentialite")
def privacy():
    return render_template("legal.html", title="Politique de confidentialité", kind="privacy")


@app.route("/mentions-legales")
def legal_notice():
    return render_template("legal.html", title="Mentions légales", kind="mentions")


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        company = request.form.get("company", "").strip()[:120]
        if not EMAIL_RE.match(email):
            flash("Adresse e-mail invalide.", "error")
        elif len(password) < 8:
            flash("Le mot de passe doit faire au moins 8 caractères.", "error")
        elif not company:
            flash("Indiquez le nom de votre activité ou entreprise.", "error")
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, company, penalty_rate, created_at) VALUES (?,?,?,?,?)",
                    (email, generate_password_hash(password), company, DEFAULT_PENALTY_RATE, now_iso()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("Un compte existe déjà avec cet e-mail.", "error")
            else:
                session.clear()
                session["uid"] = cur.lastrowid
                session.permanent = True
                return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["uid"] = user["id"]
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash("E-mail ou mot de passe incorrect.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("landing"))


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
def enrich(row):
    d = dict(row)
    late = days_late(row["due_date"])
    d["late"] = late
    if row["status"] == "paid":
        d["state"], d["state_label"] = "paid", "Payée"
    elif late > 0:
        d["state"], d["state_label"] = "overdue", f"En retard ({late} j)"
    else:
        d["state"], d["state_label"] = "pending", "À échoir"
    return d


@app.route("/app")
@login_required
def dashboard():
    db = get_db()
    rows = db.execute(
        """SELECT i.*, c.name AS client_name, c.email AS client_email,
                  (SELECT MAX(step) FROM reminders r WHERE r.invoice_id = i.id AND r.status IN ('sent','dry_run')) AS last_step
           FROM invoices i JOIN clients c ON c.id = i.client_id
           WHERE i.user_id = ?
           ORDER BY (i.status = 'paid'), i.due_date ASC""",
        (g.user["id"],),
    ).fetchall()
    invoices = [enrich(r) for r in rows]
    unpaid = [i for i in invoices if i["status"] == "unpaid"]
    overdue = [i for i in unpaid if i["late"] > 0]
    recovered = db.execute(
        """SELECT COALESCE(SUM(i.amount_cents), 0) AS s FROM invoices i
           WHERE i.user_id = ? AND i.status = 'paid'
             AND EXISTS (SELECT 1 FROM reminders r WHERE r.invoice_id = i.id)""",
        (g.user["id"],),
    ).fetchone()["s"]
    stats = dict(
        outstanding=sum(i["amount_cents"] for i in unpaid),
        overdue_amount=sum(i["amount_cents"] for i in overdue),
        overdue_count=len(overdue),
        recovered=recovered,
        unpaid_count=len(unpaid),
    )
    return render_template("dashboard.html", invoices=invoices, stats=stats,
                           limit_reached=g.user["plan"] == "free" and len(unpaid) >= FREE_LIMIT)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    db = get_db()
    unpaid_count = db.execute(
        "SELECT COUNT(*) AS n FROM invoices WHERE user_id = ? AND status = 'unpaid'", (g.user["id"],)
    ).fetchone()["n"]
    if g.user["plan"] == "free" and unpaid_count >= FREE_LIMIT:
        flash(f"Le plan gratuit est limité à {FREE_LIMIT} factures impayées suivies. Passez en Pro pour en suivre sans limite.", "error")
        return redirect(url_for("upgrade"))

    form = {"issue_date": date.today().isoformat(), "due_date": (date.today() + timedelta(days=30)).isoformat()}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("client_name", "client_email", "number", "amount", "issue_date", "due_date")}
        try:
            if not form["client_name"] or len(form["client_name"]) > 120:
                raise ValueError("Nom du client requis.")
            if not EMAIL_RE.match(form["client_email"].lower()):
                raise ValueError("E-mail du client invalide.")
            if not form["number"] or len(form["number"]) > 40:
                raise ValueError("Numéro de facture requis.")
            amount = parse_amount(form["amount"])
            issue = parse_date(form["issue_date"])
            due = parse_date(form["due_date"])
            if due < issue:
                raise ValueError("L'échéance ne peut pas précéder la date de la facture.")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            email = form["client_email"].lower()
            client = db.execute("SELECT * FROM clients WHERE user_id = ? AND email = ?",
                                (g.user["id"], email)).fetchone()
            if client is None:
                cur = db.execute("INSERT INTO clients (user_id, name, email) VALUES (?,?,?)",
                                 (g.user["id"], form["client_name"], email))
                client_id = cur.lastrowid
            else:
                client_id = client["id"]
            cur = db.execute(
                """INSERT INTO invoices (user_id, client_id, number, amount_cents, issue_date, due_date, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (g.user["id"], client_id, form["number"], amount, issue.isoformat(), due.isoformat(), now_iso()),
            )
            db.commit()
            flash("Facture ajoutée. Les relances partiront automatiquement après l'échéance.", "ok")
            return redirect(url_for("invoice_detail", invoice_id=cur.lastrowid))
    return render_template("invoice_new.html", form=form)


def get_invoice_or_404(invoice_id):
    row = get_db().execute(
        """SELECT i.*, c.name AS client_name, c.email AS client_email
           FROM invoices i JOIN clients c ON c.id = i.client_id
           WHERE i.id = ? AND i.user_id = ?""",
        (invoice_id, g.user["id"]),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    row = get_invoice_or_404(invoice_id)
    inv = enrich(row)
    db = get_db()
    history = db.execute("SELECT * FROM reminders WHERE invoice_id = ? ORDER BY id DESC", (invoice_id,)).fetchall()
    interest, indemnity = penalties(row["amount_cents"], g.user["penalty_rate"], inv["late"])
    next_step = step_for(inv["late"])
    preview = None
    if row["status"] == "unpaid" and next_step:
        client = db.execute("SELECT * FROM clients WHERE id = ?", (row["client_id"],)).fetchone()
        preview = build_email(g.user, client, row, next_step)
    schedule = []
    for number, label, threshold in STEPS:
        when = date.fromisoformat(row["due_date"]) + timedelta(days=threshold)
        done = any(h["step"] == number and h["status"] in ("sent", "dry_run") for h in history)
        schedule.append(dict(number=number, label=label, when=when.isoformat(), done=done))
    return render_template("invoice_detail.html", inv=inv, history=history, interest=interest,
                           indemnity=indemnity, preview=preview, next_step=next_step, schedule=schedule)


@app.route("/invoices/<int:invoice_id>/paid", methods=["POST"])
@login_required
def invoice_paid(invoice_id):
    get_invoice_or_404(invoice_id)
    db = get_db()
    db.execute("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?", (now_iso(), invoice_id))
    db.commit()
    flash("Facture marquée comme payée. Les relances sont arrêtées.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/invoices/<int:invoice_id>/send", methods=["POST"])
@login_required
def invoice_send(invoice_id):
    row = get_invoice_or_404(invoice_id)
    late = days_late(row["due_date"])
    step = step_for(late)
    if row["status"] != "unpaid" or step == 0:
        flash("Cette facture n'est pas encore éligible à une relance.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (row["client_id"],)).fetchone()
    status = dispatch_reminder(db, g.user, client, row, step)
    if status == "sent":
        flash("Relance envoyée.", "ok")
    elif status == "dry_run":
        flash("Mode simulation (SMTP non configuré) : relance enregistrée mais pas envoyée.", "ok")
    else:
        flash("L'envoi a échoué. Réessayez plus tard.", "error")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
def invoice_delete(invoice_id):
    get_invoice_or_404(invoice_id)
    db = get_db()
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    flash("Facture supprimée.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        company = request.form.get("company", "").strip()[:120]
        auto = 1 if request.form.get("auto_enabled") else 0
        try:
            rate = float(request.form.get("penalty_rate", "").replace(",", "."))
            if not 0 < rate <= 100:
                raise ValueError
        except ValueError:
            flash("Taux de pénalités invalide.", "error")
            return redirect(url_for("settings"))
        if not company:
            flash("Le nom de l'entreprise est requis.", "error")
            return redirect(url_for("settings"))
        db = get_db()
        db.execute("UPDATE users SET company = ?, auto_enabled = ?, penalty_rate = ? WHERE id = ?",
                   (company, auto, rate, g.user["id"]))
        db.commit()
        flash("Réglages enregistrés.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", portal=STRIPE_PORTAL_LINK)


@app.route("/upgrade")
@login_required
def upgrade():
    link = None
    if STRIPE_PAYMENT_LINK:
        sep = "&" if "?" in STRIPE_PAYMENT_LINK else "?"
        link = STRIPE_PAYMENT_LINK + sep + urlencode(
            {"client_reference_id": g.user["id"], "prefilled_email": g.user["email"]})
    return render_template("upgrade.html", link=link)


# --------------------------------------------------------------------------
# Paiement : webhook Stripe (signature verifiee a la main, sans bibliotheque)
# --------------------------------------------------------------------------
def verify_stripe_signature(payload, header, secret, tolerance=300):
    try:
        items = [p.split("=", 1) for p in header.split(",") if "=" in p]
        timestamp = next(v for k, v in items if k == "t")
        signatures = [v for k, v in items if k == "v1"]
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        fresh = abs(time.time() - int(timestamp)) <= tolerance
        return fresh and any(hmac.compare_digest(expected, s) for s in signatures)
    except (StopIteration, ValueError):
        return False


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        abort(404)
    payload = request.get_data()
    if not verify_stripe_signature(payload, request.headers.get("Stripe-Signature", ""), STRIPE_WEBHOOK_SECRET):
        abort(400)
    event = request.get_json(silent=True) or {}
    obj = (event.get("data") or {}).get("object") or {}
    db = get_db()
    if event.get("type") == "checkout.session.completed":
        ref = str(obj.get("client_reference_id") or "")
        customer = obj.get("customer")
        if ref.isdigit():
            db.execute("UPDATE users SET plan = 'pro', stripe_customer = ? WHERE id = ?", (customer, int(ref)))
        else:
            email = ((obj.get("customer_details") or {}).get("email") or "").lower()
            if email:
                db.execute("UPDATE users SET plan = 'pro', stripe_customer = ? WHERE email = ?", (customer, email))
        db.commit()
    elif event.get("type") == "customer.subscription.deleted":
        customer = obj.get("customer")
        if customer:
            db.execute("UPDATE users SET plan = 'free' WHERE stripe_customer = ?", (customer,))
            db.commit()
    return jsonify(received=True)


# --------------------------------------------------------------------------
# Declencheur des relances (appele 1x/jour par une tache planifiee)
# --------------------------------------------------------------------------
@app.route("/cron/run", methods=["GET", "POST"])
def cron_run():
    if not CRON_TOKEN:
        abort(404)
    supplied = request.headers.get("X-Cron-Token") or request.args.get("token", "")
    if not hmac.compare_digest(supplied, CRON_TOKEN):
        abort(403)
    return jsonify(run_reminders(get_db()))


@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Page introuvable", text="Cette page n'existe pas."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("message.html", title="Requête invalide", text=getattr(e, "description", "")), 400


# --------------------------------------------------------------------------
# Gabarits HTML (inclus dans le fichier pour tout garder simple)
# --------------------------------------------------------------------------
TEMPLATES = {}

TEMPLATES["base.html"] = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}{{ app_name }}{% endblock %} · {{ app_name }}</title>
<meta name="description" content="{% block desc %}Relances automatiques de factures impayées pour freelances et TPE.{% endblock %}">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14213d;--muted:#5b6475;--line:#e4e7ec;--brand:#0f766e;--brand-d:#0b5a54;--warn:#b45309;--bad:#b91c1c;--ok:#15803d}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--brand)}h1,h2,h3{line-height:1.2;margin:0 0 .5em}
.wrap{max-width:980px;margin:0 auto;padding:0 20px}
header.top{background:var(--card);border-bottom:1px solid var(--line)}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;height:60px}
.logo{font-weight:800;font-size:1.2rem;color:var(--ink);text-decoration:none}.logo span{color:var(--brand)}
nav{display:flex;gap:16px;align-items:center}nav a{text-decoration:none;color:var(--muted)}nav form{margin:0}
.btn{display:inline-block;background:var(--brand);color:#fff;border:0;border-radius:8px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.btn:hover{background:var(--brand-d)}.btn.ghost{background:transparent;color:var(--brand);border:1px solid var(--brand)}
.btn.danger{background:transparent;color:var(--bad);border:1px solid var(--bad)}.btn.small{padding:6px 12px;font-size:.9rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;margin:18px 0}
.grid{display:grid;gap:16px}.g4{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.g3{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.stat{font-size:1.7rem;font-weight:800}.muted{color:var(--muted)}.small{font-size:.9rem}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}th{font-size:.8rem;text-transform:uppercase;color:var(--muted);letter-spacing:.04em}
.right{text-align:right}.tag{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.82rem;font-weight:600}
.tag.paid{background:#dcfce7;color:var(--ok)}.tag.overdue{background:#fee2e2;color:var(--bad)}.tag.pending{background:#e0f2fe;color:#075985}
label{display:block;font-weight:600;margin:14px 0 4px}input[type=text],input[type=email],input[type=password],input[type=date],input[type=number]{width:100%;padding:10px 12px;border:1px solid #c9ced8;border-radius:8px;font:inherit;background:#fff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:640px){.row{grid-template-columns:1fr}table{font-size:.9rem}}
.flash{padding:12px 16px;border-radius:8px;margin:16px 0;font-weight:500}.flash.ok{background:#dcfce7;color:#14532d}.flash.error{background:#fee2e2;color:#7f1d1d}
.hero{padding:64px 0 40px;text-align:center}.hero h1{font-size:2.5rem;max-width:760px;margin:0 auto .4em}.hero p{font-size:1.2rem;color:var(--muted);max-width:640px;margin:0 auto 24px}
.price{font-size:2.4rem;font-weight:800}.price small{font-size:1rem;font-weight:500;color:var(--muted)}
ul.clean{padding-left:20px;margin:10px 0}pre.mail{white-space:pre-wrap;background:#f3f4f6;border-radius:8px;padding:14px;font:14px/1.5 ui-monospace,Menlo,monospace}
footer{padding:30px 0;color:var(--muted);font-size:.9rem;text-align:center}footer a{color:var(--muted)}
.step-done{color:var(--ok);font-weight:600}
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="{{ url_for('landing') if not g.user else url_for('dashboard') }}">{{ app_name[:-2] }}<span>{{ app_name[-2:] }}</span></a>
  <nav>
    {% if g.user %}
      <a href="{{ url_for('dashboard') }}">Factures</a>
      <a href="{{ url_for('settings') }}">Réglages</a>
      <form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn ghost small" type="submit">Déconnexion</button></form>
    {% else %}
      <a href="{{ url_for('login') }}">Connexion</a>
      <a class="btn small" href="{{ url_for('signup') }}">Essayer gratuitement</a>
    {% endif %}
  </nav>
</div></header>
<main class="wrap">
  {% with messages = get_flashed_messages(with_categories=true) %}{% for cat, msg in messages %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}{% endwith %}
  {% block content %}{% endblock %}
</main>
<footer><div class="wrap">
  © {{ app_name }} · <a href="{{ url_for('legal_notice') }}">Mentions légales</a> · <a href="{{ url_for('cgu') }}">CGU</a> · <a href="{{ url_for('privacy') }}">Confidentialité</a>
</div></footer>
</body></html>"""

TEMPLATES["landing.html"] = """{% extends 'base.html' %}
{% block title %}Relances de factures impayées automatiques{% endblock %}
{% block content %}
<section class="hero">
  <h1>Ne courez plus après vos factures impayées.</h1>
  <p>{{ app_name }} relance vos clients à votre place, par e-mail, avec le bon ton au bon moment, et calcule les pénalités de retard légales.</p>
  <a class="btn" href="{{ url_for('signup') }}">Créer mon compte gratuit</a>
  <p class="small muted" style="margin-top:12px">Gratuit jusqu'à {{ free_limit }} factures impayées suivies. Sans carte bancaire.</p>
</section>

<section class="grid g3">
  <div class="card"><h3>1. Ajoutez vos factures</h3><p class="muted">Client, numéro, montant, échéance : 30 secondes par facture.</p></div>
  <div class="card"><h3>2. Les relances partent seules</h3><p class="muted">Quatre étapes graduées après l'échéance, du rappel amical à la dernière relance avant recouvrement.</p></div>
  <div class="card"><h3>3. Vous êtes payé</h3><p class="muted">Vos clients répondent directement à votre adresse. Marquez la facture payée : les relances s'arrêtent.</p></div>
</section>

<section class="card">
  <h2>Le calendrier de relance</h2>
  <table>
    <tr><th>Étape</th><th>Quand</th><th>Ton</th></tr>
    {% for n, label, days in steps %}
    <tr><td>{{ label }}</td><td>échéance + {{ days }} jour{{ 's' if days > 1 }}</td>
      <td class="muted">{% if n == 1 %}Courtois, pense à un oubli{% elif n == 2 %}Ferme, rappelle les pénalités légales{% elif n == 3 %}Détail chiffré des sommes dues{% else %}Dernière relance avant recouvrement{% endif %}</td></tr>
    {% endfor %}
  </table>
  <p class="small muted">Pénalités de retard et indemnité forfaitaire de recouvrement de 40 € calculées automatiquement (professionnels, B2B).</p>
</section>

<section class="grid g3" style="align-items:stretch">
  <div class="card"><h3>Gratuit</h3><div class="price">0 €</div>
    <ul class="clean"><li>{{ free_limit }} factures impayées suivies</li><li>Relances automatiques</li><li>Calcul des pénalités</li></ul>
    <a class="btn ghost" href="{{ url_for('signup') }}">Commencer</a></div>
  <div class="card" style="border-color:var(--brand)"><h3>Pro</h3><div class="price">{{ price_label }}<small> /mois</small></div>
    <ul class="clean"><li>Factures illimitées</li><li>Relances automatiques</li><li>Historique complet</li><li>Résiliable à tout moment</li></ul>
    <a class="btn" href="{{ url_for('signup') }}">Essayer, puis passer Pro</a></div>
</section>

<section class="card">
  <h2>Questions fréquentes</h2>
  <p><strong>Les e-mails partent-ils de mon adresse ?</strong><br><span class="muted">Ils sont envoyés au nom de votre entreprise, avec votre adresse en « répondre à ». Vos clients vous répondent directement.</span></p>
  <p><strong>Est-ce légal ?</strong><br><span class="muted">Relancer un client pour un impayé est courant et légal. Les pénalités de retard doivent figurer sur vos factures et conditions de vente pour être réclamées.</span></p>
  <p><strong>Et si le client a déjà payé ?</strong><br><span class="muted">Marquez la facture comme payée : plus aucune relance ne part.</span></p>
</section>
{% endblock %}"""

TEMPLATES["auth.html"] = """{% extends 'base.html' %}
{% block title %}{{ 'Créer un compte' if mode == 'signup' else 'Connexion' }}{% endblock %}
{% block content %}
<div class="card" style="max-width:440px;margin:40px auto">
  <h2>{{ 'Créer mon compte' if mode == 'signup' else 'Connexion' }}</h2>
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    {% if mode == 'signup' %}
      <label for="company">Nom de votre activité / entreprise</label>
      <input id="company" type="text" name="company" required maxlength="120" value="{{ request.form.get('company','') }}">
    {% endif %}
    <label for="email">E-mail</label>
    <input id="email" type="email" name="email" required value="{{ request.form.get('email','') }}">
    <label for="password">Mot de passe{% if mode == 'signup' %} (8 caractères minimum){% endif %}</label>
    <input id="password" type="password" name="password" required minlength="{{ 8 if mode == 'signup' else 1 }}">
    <p><button class="btn" type="submit">{{ 'Créer mon compte' if mode == 'signup' else 'Se connecter' }}</button></p>
  </form>
  <p class="small muted">
    {% if mode == 'signup' %}Déjà un compte ? <a href="{{ url_for('login') }}">Connexion</a>
    {% else %}Pas encore de compte ? <a href="{{ url_for('signup') }}">Créer un compte</a>{% endif %}
  </p>
</div>
{% endblock %}"""

TEMPLATES["dashboard.html"] = """{% extends 'base.html' %}
{% block title %}Mes factures{% endblock %}
{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:24px;flex-wrap:wrap;gap:10px">
  <h1 style="margin:0">Mes factures</h1>
  {% if limit_reached %}<a class="btn" href="{{ url_for('upgrade') }}">Passer en Pro pour en ajouter</a>
  {% else %}<a class="btn" href="{{ url_for('invoice_new') }}">+ Ajouter une facture</a>{% endif %}
</div>

<div class="grid g4" style="margin-top:16px">
  <div class="card" style="margin:0"><div class="muted small">En attente de paiement</div><div class="stat">{{ stats.outstanding | eur }}</div></div>
  <div class="card" style="margin:0"><div class="muted small">Dont en retard ({{ stats.overdue_count }})</div><div class="stat" style="color:var(--bad)">{{ stats.overdue_amount | eur }}</div></div>
  <div class="card" style="margin:0"><div class="muted small">Encaissé après relance</div><div class="stat" style="color:var(--ok)">{{ stats.recovered | eur }}</div></div>
</div>

{% if g.user.plan == 'free' %}
<p class="small muted">Plan gratuit : {{ stats.unpaid_count }}/{{ free_limit }} factures impayées suivies. <a href="{{ url_for('upgrade') }}">Passer en Pro</a></p>
{% endif %}

<div class="card" style="overflow-x:auto">
{% if invoices %}
<table>
  <tr><th>Facture</th><th>Client</th><th class="right">Montant</th><th>Échéance</th><th>Statut</th><th>Dernière relance</th></tr>
  {% for i in invoices %}
  <tr>
    <td><a href="{{ url_for('invoice_detail', invoice_id=i.id) }}">{{ i.number }}</a></td>
    <td>{{ i.client_name }}</td>
    <td class="right">{{ i.amount_cents | eur }}</td>
    <td>{{ i.due_date | fr_date }}</td>
    <td><span class="tag {{ i.state }}">{{ i.state_label }}</span></td>
    <td class="muted small">{{ step_label[i.last_step] if i.last_step else '—' }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p class="muted">Aucune facture pour l'instant. Ajoutez votre première facture impayée : les relances se feront toutes seules.</p>
{% endif %}
</div>
{% endblock %}"""

TEMPLATES["invoice_new.html"] = """{% extends 'base.html' %}
{% block title %}Nouvelle facture{% endblock %}
{% block content %}
<div class="card" style="max-width:640px;margin:30px auto">
  <h2>Ajouter une facture</h2>
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <div class="row">
      <div><label for="client_name">Nom du client</label><input id="client_name" type="text" name="client_name" required maxlength="120" value="{{ form.get('client_name','') }}"></div>
      <div><label for="client_email">E-mail du client</label><input id="client_email" type="email" name="client_email" required value="{{ form.get('client_email','') }}"></div>
    </div>
    <div class="row">
      <div><label for="number">Numéro de facture</label><input id="number" type="text" name="number" required maxlength="40" value="{{ form.get('number','') }}"></div>
      <div><label for="amount">Montant TTC (€)</label><input id="amount" type="text" inputmode="decimal" name="amount" required placeholder="1 250,00" value="{{ form.get('amount','') }}"></div>
    </div>
    <div class="row">
      <div><label for="issue_date">Date de la facture</label><input id="issue_date" type="date" name="issue_date" required value="{{ form.get('issue_date','') }}"></div>
      <div><label for="due_date">Date d'échéance</label><input id="due_date" type="date" name="due_date" required value="{{ form.get('due_date','') }}"></div>
    </div>
    <p><button class="btn" type="submit">Enregistrer</button> <a class="btn ghost" href="{{ url_for('dashboard') }}">Annuler</a></p>
  </form>
</div>
{% endblock %}"""

TEMPLATES["invoice_detail.html"] = """{% extends 'base.html' %}
{% block title %}Facture {{ inv.number }}{% endblock %}
{% block content %}
<p style="margin-top:24px"><a href="{{ url_for('dashboard') }}">← Retour aux factures</a></p>
<div class="card">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
    <div><h2 style="margin:0">Facture {{ inv.number }}</h2>
      <div class="muted">{{ inv.client_name }} · {{ inv.client_email }}</div></div>
    <div><span class="tag {{ inv.state }}">{{ inv.state_label }}</span></div>
  </div>
  <div class="grid g4" style="margin-top:16px">
    <div><div class="muted small">Montant</div><div class="stat">{{ inv.amount_cents | eur }}</div></div>
    <div><div class="muted small">Échéance</div><div class="stat">{{ inv.due_date | fr_date }}</div></div>
    {% if inv.status == 'unpaid' and inv.late > 0 %}
    <div><div class="muted small">Pénalités à ce jour</div><div class="stat">{{ (interest + indemnity) | eur }}</div>
      <div class="small muted">dont {{ indemnity | eur }} d'indemnité forfaitaire</div></div>
    {% endif %}
  </div>
  {% if inv.status == 'unpaid' %}
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:18px">
    <form method="post" action="{{ url_for('invoice_paid', invoice_id=inv.id) }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn" type="submit">Marquer comme payée</button></form>
    {% if next_step %}<form method="post" action="{{ url_for('invoice_send', invoice_id=inv.id) }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn ghost" type="submit">Envoyer la relance maintenant</button></form>{% endif %}
    <form method="post" action="{{ url_for('invoice_delete', invoice_id=inv.id) }}" onsubmit="return confirm('Supprimer cette facture ?')"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn danger" type="submit">Supprimer</button></form>
  </div>
  {% endif %}
</div>

<div class="card">
  <h3>Calendrier des relances</h3>
  <table>
    {% for s in schedule %}
    <tr><td>{{ s.label }}</td><td>{{ s.when | fr_date }}</td>
      <td>{% if s.done %}<span class="step-done">✓ envoyée</span>{% elif inv.status == 'paid' %}<span class="muted">annulée</span>{% else %}<span class="muted">prévue</span>{% endif %}</td></tr>
    {% endfor %}
  </table>
  {% if not g.user.auto_enabled %}<p class="small" style="color:var(--warn)">Les relances automatiques sont désactivées dans vos réglages.</p>{% endif %}
</div>

{% if preview %}
<div class="card"><h3>Prochaine relance : aperçu</h3>
  <div class="muted small">Objet : {{ preview[0] }}</div><pre class="mail">{{ preview[1] }}</pre></div>
{% endif %}

{% if history %}
<div class="card"><h3>Historique</h3>
  {% for h in history %}
  <details style="margin:8px 0"><summary>{{ h.sent_at.replace('T',' ') }} · {{ step_label[h.step] }} ·
    {{ 'envoyée' if h.status == 'sent' else ('simulée' if h.status == 'dry_run' else 'échec') }}</summary>
    <div class="muted small">Objet : {{ h.subject }}</div><pre class="mail">{{ h.body }}</pre></details>
  {% endfor %}
</div>
{% endif %}
{% endblock %}"""

TEMPLATES["settings.html"] = """{% extends 'base.html' %}
{% block title %}Réglages{% endblock %}
{% block content %}
<div class="card" style="max-width:600px;margin:30px auto">
  <h2>Réglages</h2>
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <label for="company">Nom de l'entreprise (apparaît dans les relances)</label>
    <input id="company" type="text" name="company" required maxlength="120" value="{{ g.user.company }}">
    <label for="penalty_rate">Taux annuel des pénalités de retard (%)</label>
    <input id="penalty_rate" type="text" name="penalty_rate" required value="{{ ('%g' % g.user.penalty_rate).replace('.', ',') }}">
    <p class="small muted">Ce taux doit être celui indiqué dans vos conditions de vente ou sur vos factures. Le minimum légal est de 3 fois le taux d'intérêt légal.</p>
    <label style="display:flex;gap:10px;align-items:center;font-weight:500"><input type="checkbox" name="auto_enabled" value="1" {{ 'checked' if g.user.auto_enabled }}> Envoyer les relances automatiquement</label>
    <p><button class="btn" type="submit">Enregistrer</button></p>
  </form>
  <hr style="border:0;border-top:1px solid var(--line);margin:22px 0">
  <p><strong>Abonnement :</strong> {{ 'Pro' if g.user.plan == 'pro' else 'Gratuit' }}
    {% if g.user.plan == 'pro' %}{% if portal %}· <a href="{{ portal }}">Gérer / résilier mon abonnement</a>{% endif %}
    {% else %}· <a href="{{ url_for('upgrade') }}">Passer en Pro</a>{% endif %}</p>
  <p class="small muted">Compte : {{ g.user.email }}</p>
</div>
{% endblock %}"""

TEMPLATES["upgrade.html"] = """{% extends 'base.html' %}
{% block title %}Passer en Pro{% endblock %}
{% block content %}
<div class="card" style="max-width:520px;margin:40px auto;text-align:center">
  <h2>Plan Pro</h2>
  <div class="price">{{ price_label }}<small> /mois</small></div>
  <ul class="clean" style="text-align:left;display:inline-block"><li>Factures impayées illimitées</li><li>Relances automatiques</li><li>Résiliable à tout moment</li></ul>
  {% if g.user.plan == 'pro' %}
    <p><strong>Vous êtes déjà en Pro. Merci !</strong></p>
  {% elif link %}
    <p><a class="btn" href="{{ link }}">Passer en Pro</a></p>
    <p class="small muted">Paiement sécurisé par Stripe. L'activation est automatique après le paiement.</p>
  {% else %}
    <p class="muted">Le paiement n'est pas encore configuré.</p>
  {% endif %}
</div>
{% endblock %}"""

TEMPLATES["legal.html"] = """{% extends 'base.html' %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<div class="card" style="margin-top:30px">
  <h1>{{ title }}</h1>
  <p style="background:#fef3c7;padding:10px 14px;border-radius:8px"><strong>À compléter avant la mise en ligne</strong> : remplacez les passages entre crochets par vos informations réelles.</p>
  {% if kind == 'mentions' %}
    <p>Éditeur du site : [Votre nom ou raison sociale], [forme juridique], [adresse], SIRET [numéro], e-mail : [contact].</p>
    <p>Directeur de la publication : [Votre nom].</p>
    <p>Hébergeur : [Nom, adresse et site de l'hébergeur].</p>
  {% elif kind == 'cgu' %}
    <p>{{ app_name }} est un service en ligne d'aide au suivi et à la relance de factures impayées.</p>
    <p><strong>Responsabilité.</strong> Le service automatise l'envoi de relances rédigées à partir des informations saisies par l'utilisateur, qui reste seul responsable de l'exactitude de ces informations, du bien-fondé des créances et du respect de ses obligations légales. {{ app_name }} ne fournit pas de conseil juridique.</p>
    <p><strong>Abonnement.</strong> Le plan Pro est facturé {{ price_label }} par mois, sans engagement, résiliable à tout moment depuis les réglages. [Précisez TVA / mentions de facturation.]</p>
    <p><strong>Contact.</strong> [Votre e-mail de support].</p>
  {% else %}
    <p>Responsable du traitement : [Votre nom ou raison sociale], [contact].</p>
    <p><strong>Données traitées :</strong> e-mail et nom d'entreprise de l'utilisateur, noms et e-mails de ses clients débiteurs, données de factures saisies. Finalité : fournir le service de relance. L'utilisateur agit en responsable de traitement pour les données de ses propres clients ; {{ app_name }} agit comme sous-traitant.</p>
    <p><strong>Durée de conservation :</strong> pendant la durée du compte, puis [durée] après sa suppression.</p>
    <p><strong>Vos droits :</strong> accès, rectification, suppression, opposition, en écrivant à [contact]. Réclamation possible auprès de la CNIL.</p>
    <p><strong>Prestataires :</strong> [hébergeur], [service d'envoi d'e-mails], Stripe (paiement).</p>
  {% endif %}
</div>
{% endblock %}"""

TEMPLATES["message.html"] = """{% extends 'base.html' %}
{% block title %}{{ title }}{% endblock %}
{% block content %}<div class="card" style="margin-top:40px"><h2>{{ title }}</h2><p class="muted">{{ text }}</p>
<p><a class="btn" href="{{ url_for('landing') }}">Retour à l'accueil</a></p></div>{% endblock %}"""

app.jinja_env.loader = DictLoader(TEMPLATES)

init_db()


# --------------------------------------------------------------------------
# Point d'entree en ligne de commande
# --------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "run-reminders":
        print(run_reminders())
    elif args and args[0] == "set-plan" and len(args) == 3 and args[2] in ("free", "pro"):
        conn = connect()
        cur = conn.execute("UPDATE users SET plan = ? WHERE email = ?", (args[2], args[1].lower()))
        conn.commit()
        print("OK" if cur.rowcount else "Utilisateur introuvable")
        conn.close()
    elif args:
        print(__doc__)
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
