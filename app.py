"""
KPI Dashboard - Flask + SQL database (SQLite locally, Postgres on Render) with full CRUD.
Run locally:  python app.py   ->  http://localhost:5000
"""
import csv, io, os, random, secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

db_url = os.environ.get("DATABASE_URL", "sqlite:///kpi.db")
if db_url.startswith("postgres://"):  # Render/Heroku style URL
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))
# If ADMIN_PASSWORD is set, only logged-in users can create/edit/delete. Reading is always public.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

db = SQLAlchemy(app)


class KPI(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    unit = db.Column(db.String(20), default="")
    target = db.Column(db.Float, nullable=False)
    direction = db.Column(db.String(10), default="higher")  # higher = bigger is better, lower = smaller is better
    color = db.Column(db.String(9), default="#3b82f6")
    description = db.Column(db.String(400), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    entries = db.relationship("Entry", backref="kpi", cascade="all, delete-orphan", order_by="Entry.date")


class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kpi_id = db.Column(db.Integer, db.ForeignKey("kpi.id", ondelete="CASCADE"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    value = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(300), default="")


# ---------- helpers ----------
def status_for(value, target, direction):
    if value is None:
        return "none"
    if direction == "lower":
        if value <= target: return "good"
        return "warn" if value <= target * 1.05 else "bad"
    if value >= target: return "good"
    return "warn" if value >= target * 0.95 else "bad"


def kpi_json(k, with_stats=True):
    d = {"id": k.id, "name": k.name, "unit": k.unit, "target": k.target, "direction": k.direction,
         "color": k.color, "description": k.description}
    if with_stats:
        es = k.entries
        last = es[-1] if es else None
        prev = es[-2] if len(es) > 1 else None
        d.update(value=last.value if last else None, last_date=last.date.isoformat() if last else None,
                 delta=(last.value - prev.value) if last and prev else None,
                 status=status_for(last.value if last else None, k.target, k.direction),
                 entry_count=len(es))
    return d


def entry_json(e):
    return {"id": e.id, "kpi_id": e.kpi_id, "date": e.date.isoformat(), "value": e.value, "note": e.note}


def forecast(entries, steps=6):
    """Simple least-squares linear trend over the entries; returns next `steps` monthly points."""
    n = len(entries)
    if n < 3:
        return []
    ys = [e.value for e in entries]
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs) or 1
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    icpt = my - slope * mx
    resid = (sum((y - (icpt + slope * x)) ** 2 for x, y in zip(xs, ys)) / max(n - 2, 1)) ** 0.5
    out, d = [], entries[-1].date
    for i in range(1, steps + 1):
        d = d + timedelta(days=30)
        p = icpt + slope * (n - 1 + i)
        out.append({"date": d.isoformat(), "value": round(p, 2), "upper": round(p + 1.5 * resid, 2),
                    "lower": round(p - 1.5 * resid, 2)})
    return out


def parse_kpi(data, k):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required.")
    try:
        target = float(data.get("target"))
    except (TypeError, ValueError):
        raise ValueError("Target must be a number.")
    direction = data.get("direction", "higher")
    if direction not in ("higher", "lower"):
        raise ValueError("Direction must be 'higher' or 'lower'.")
    k.name, k.target, k.direction = name[:120], target, direction
    k.unit = (data.get("unit") or "")[:20]
    k.color = (data.get("color") or "#3b82f6")[:9]
    k.description = (data.get("description") or "")[:400]


def parse_entry(data, e):
    try:
        e.value = float(data.get("value"))
        e.date = date.fromisoformat(str(data.get("date"))[:10])
    except (TypeError, ValueError):
        raise ValueError("A valid date (YYYY-MM-DD) and numeric value are required.")
    e.note = (data.get("note") or "")[:300]


def writer(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if ADMIN_PASSWORD and not session.get("admin"):
            return jsonify({"error": "Please log in to make changes."}), 401
        try:
            return fn(*a, **kw)
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
    return wrap


def seed_sample():
    samples = [
        ("Sales Growth Rate", "%", 8, "higher", "#2a78d6", 4.2, 0.3, "Month-over-month revenue growth"),
        ("Customer Churn Rate", "%", 5, "lower", "#eb6834", 8.3, 0.4, "Share of customers lost per month"),
        ("Avg. Order Value", "$", 120, "higher", "#1baf7a", 108, 3, "Average value of an order"),
        ("Process Cycle Time", "days", 3, "lower", "#a855f7", 4.1, 0.2, "Order-to-delivery time"),
        ("On-Time Delivery", "%", 95, "higher", "#f59e0b", 87, 1.2, "Orders delivered on schedule"),
        ("First Contact Resolution", "%", 80, "higher", "#06b6d4", 63, 1.5, "Tickets solved on first contact"),
    ]
    rnd = random.Random(42)
    start = date.today().replace(day=1) - timedelta(days=30 * 11)
    for name, unit, target, direction, color, base, noise, desc in samples:
        k = KPI(name=name, unit=unit, target=target, direction=direction, color=color, description=desc)
        v = base
        goal = target * (0.99 if direction == "higher" else 1.02)
        for i in range(12):
            v += (goal - base) / 11 + rnd.uniform(-noise, noise)
            k.entries.append(Entry(date=start + timedelta(days=30 * i), value=round(v, 2)))
        db.session.add(k)
    db.session.commit()


with app.app_context():
    db.create_all()
    if KPI.query.count() == 0 and os.environ.get("SEED_SAMPLE", "1") == "1":
        seed_sample()


# ---------- pages & auth ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/session")
def get_session():
    return jsonify({"auth_required": bool(ADMIN_PASSWORD), "logged_in": (not ADMIN_PASSWORD) or bool(session.get("admin"))})


@app.post("/api/login")
def login():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if ADMIN_PASSWORD and secrets.compare_digest(pw, ADMIN_PASSWORD):
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Wrong password."}), 401


@app.post("/api/logout")
def logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


# ---------- KPI CRUD ----------
@app.get("/api/kpis")
def list_kpis():
    return jsonify([kpi_json(k) for k in KPI.query.order_by(KPI.id).all()])


@app.post("/api/kpis")
@writer
def create_kpi():
    k = KPI()
    parse_kpi(request.get_json(silent=True) or {}, k)
    db.session.add(k); db.session.commit()
    return jsonify(kpi_json(k)), 201


@app.get("/api/kpis/<int:kid>")
def get_kpi(kid):
    return jsonify(kpi_json(db.get_or_404(KPI, kid)))


@app.put("/api/kpis/<int:kid>")
@writer
def update_kpi(kid):
    k = db.get_or_404(KPI, kid)
    parse_kpi(request.get_json(silent=True) or {}, k)
    db.session.commit()
    return jsonify(kpi_json(k))


@app.delete("/api/kpis/<int:kid>")
@writer
def delete_kpi(kid):
    db.session.delete(db.get_or_404(KPI, kid)); db.session.commit()
    return jsonify({"deleted": kid})


# ---------- Entry CRUD ----------
@app.get("/api/kpis/<int:kid>/entries")
def list_entries(kid):
    k = db.get_or_404(KPI, kid)
    return jsonify([entry_json(e) for e in k.entries])


@app.post("/api/kpis/<int:kid>/entries")
@writer
def create_entry(kid):
    k = db.get_or_404(KPI, kid)
    e = Entry(kpi_id=k.id)
    parse_entry(request.get_json(silent=True) or {}, e)
    db.session.add(e); db.session.commit()
    return jsonify(entry_json(e)), 201


@app.put("/api/entries/<int:eid>")
@writer
def update_entry(eid):
    e = db.get_or_404(Entry, eid)
    parse_entry(request.get_json(silent=True) or {}, e)
    db.session.commit()
    return jsonify(entry_json(e))


@app.delete("/api/entries/<int:eid>")
@writer
def delete_entry(eid):
    db.session.delete(db.get_or_404(Entry, eid)); db.session.commit()
    return jsonify({"deleted": eid})


# ---------- analytics ----------
@app.get("/api/kpis/<int:kid>/history")
def history(kid):
    k = db.get_or_404(KPI, kid)
    return jsonify({"kpi": kpi_json(k), "history": [entry_json(e) for e in k.entries], "forecast": forecast(k.entries)})


@app.get("/api/alerts")
def alerts():
    out = []
    for k in KPI.query.all():
        s = kpi_json(k)
        u = k.unit
        if s["status"] in ("bad", "warn"):
            word = "above" if k.direction == "lower" else "below"
            out.append({"level": "critical" if s["status"] == "bad" else "warning", "kpi_id": k.id,
                        "msg": f"{k.name} is {s['value']:g}{u}, {word} its target of {k.target:g}{u}",
                        "date": s["last_date"]})
        fc = forecast(k.entries)
        if fc and s["status"] != "none":
            end = fc[-1]["value"]
            fs = status_for(end, k.target, k.direction)
            if fs == "good" and s["status"] != "good":
                out.append({"level": "info", "kpi_id": k.id, "date": s["last_date"],
                            "msg": f"{k.name} is forecast to reach target ({end:g}{u}) within 6 months"})
            elif fs != "good" and s["status"] == "good":
                out.append({"level": "warning", "kpi_id": k.id, "date": s["last_date"],
                            "msg": f"{k.name} is on target now but forecast to slip to {end:g}{u}"})
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: order[a["level"]])
    return jsonify(out)


# ---------- CSV import/export & reset ----------
@app.get("/api/export")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["kpi", "date", "value", "note"])
    for e in Entry.query.join(KPI).order_by(KPI.id, Entry.date).all():
        w.writerow([e.kpi.name, e.date.isoformat(), e.value, e.note])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=kpi-entries.csv"})


@app.post("/api/import")
@writer
def import_csv():
    f = request.files.get("file")
    if not f:
        raise ValueError("Choose a CSV file with columns: kpi,date,value[,note].")
    rows = list(csv.DictReader(io.StringIO(f.read().decode("utf-8-sig"))))
    by_name = {k.name.lower(): k for k in KPI.query.all()}
    added = skipped = 0
    for r in rows:
        r = {(h or "").strip().lower(): v for h, v in r.items()}
        k = by_name.get((r.get("kpi") or "").strip().lower())
        if not k:
            skipped += 1; continue
        try:
            e = Entry(kpi_id=k.id); parse_entry(r, e); db.session.add(e); added += 1
        except ValueError:
            skipped += 1
    db.session.commit()
    return jsonify({"added": added, "skipped": skipped})


@app.post("/api/reset-sample")
@writer
def reset_sample():
    Entry.query.delete(); KPI.query.delete(); db.session.commit()
    seed_sample()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
