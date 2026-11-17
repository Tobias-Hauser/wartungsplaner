#!/usr/bin/env python3
"""
Wartungsplaner – Backend (nur Python-Standardbibliothek)

Stellt eine JSON-API bereit, damit alle Mitarbeiter eines Betriebs
dieselben Anlagen, Wartungen und Störungen sehen.

- Betriebe (Firmen) mit Einladungscode
- Benutzer mit Rollen: chef / mitarbeiter
- Passwörter gehasht (PBKDF2-SHA256), Sitzungen per Token
- SQLite als Datenbank
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = os.environ.get("WP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WP_PORT", "8093"))
DB = os.environ.get("WP_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wartung.db"))
PREFIX = "/wartung-api"
ERLAUBTE_ORIGINS = {
    "https://tobias-hauser.github.io",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
}
SITZUNG_TAGE = 90
PBKDF2_RUNDEN = 200_000

_lock = threading.Lock()
_versuche = {}  # IP -> [Zeitstempel] für Login-Bremse


# ---------------------------------------------------------------- Datenbank
def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS firmen (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
                erstellt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS benutzer (
                id INTEGER PRIMARY KEY, firma_id INTEGER NOT NULL REFERENCES firmen(id) ON DELETE CASCADE,
                name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
                rolle TEXT NOT NULL CHECK (rolle IN ('chef','mitarbeiter')), erstellt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sitzungen (
                token_hash TEXT PRIMARY KEY, benutzer_id INTEGER NOT NULL REFERENCES benutzer(id) ON DELETE CASCADE,
                ablauf TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS anlagen (
                id INTEGER PRIMARY KEY, firma_id INTEGER NOT NULL REFERENCES firmen(id) ON DELETE CASCADE,
                nr INTEGER NOT NULL, name TEXT NOT NULL, ort TEXT NOT NULL, typ TEXT, intervall INTEGER NOT NULL,
                letzte TEXT NOT NULL, checkliste TEXT NOT NULL DEFAULT '[]', UNIQUE (firma_id, nr));
            CREATE TABLE IF NOT EXISTS historie (
                id INTEGER PRIMARY KEY, anlage_id INTEGER NOT NULL REFERENCES anlagen(id) ON DELETE CASCADE,
                datum TEXT NOT NULL, art TEXT NOT NULL, text TEXT NOT NULL, wer TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stoerungen (
                id INTEGER PRIMARY KEY, anlage_id INTEGER NOT NULL REFERENCES anlagen(id) ON DELETE CASCADE,
                datum TEXT NOT NULL, text TEXT NOT NULL, prio TEXT NOT NULL, offen INTEGER NOT NULL DEFAULT 1,
                gemeldet_von TEXT NOT NULL);
            """
        )


# ---------------------------------------------------------------- Hilfen
def pw_hash(pw, salz=None):
    salz = salz or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salz), PBKDF2_RUNDEN).hex()
    return f"pbkdf2${PBKDF2_RUNDEN}${salz}${h}"


def pw_pruefen(pw, gespeichert):
    try:
        _, runden, salz, h = gespeichert.split("$")
        neu = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salz), int(runden)).hex()
        return hmac.compare_digest(neu, h)
    except Exception:
        return False


def token_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


def neuer_code(firmenname):
    basis = re.sub(r"[^A-Z]", "", firmenname.upper())[:6] or "FIRMA"
    return f"{basis}-{secrets.randbelow(9000) + 1000}"


def heute():
    return date.today().isoformat()


class Fehler(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status, self.text = status, text


def text_feld(daten, feld, max_len=200, pflicht=True):
    wert = str(daten.get(feld, "") or "").strip()
    if pflicht and not wert:
        raise Fehler(400, f"Feld '{feld}' fehlt")
    if len(wert) > max_len:
        raise Fehler(400, f"Feld '{feld}' ist zu lang")
    return wert


def datum_feld(daten, feld):
    wert = text_feld(daten, feld, 10)
    try:
        datetime.strptime(wert, "%Y-%m-%d")
    except ValueError:
        raise Fehler(400, "Ungültiges Datum")
    return wert


def email_feld(daten):
    e = text_feld(daten, "email", 120).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise Fehler(400, "Ungültige E-Mail-Adresse")
    return e


def passwort_feld(daten):
    pw = str(daten.get("passwort", "") or "")
    if len(pw) < 8:
        raise Fehler(400, "Passwort muss mindestens 8 Zeichen haben")
    if len(pw) > 200:
        raise Fehler(400, "Passwort zu lang")
    return pw


def login_bremse(ip):
    jetzt = time.time()
    with _lock:
        liste = [t for t in _versuche.get(ip, []) if jetzt - t < 300]
        if len(liste) >= 10:
            raise Fehler(429, "Zu viele Versuche. Bitte in ein paar Minuten erneut probieren.")
        liste.append(jetzt)
        _versuche[ip] = liste


def sitzung_anlegen(con, benutzer_id):
    t = secrets.token_urlsafe(32)
    ablauf = (datetime.utcnow() + timedelta(days=SITZUNG_TAGE)).isoformat()
    con.execute("INSERT INTO sitzungen VALUES (?,?,?)", (token_hash(t), benutzer_id, ablauf))
    return t


def anlage_json(r):
    return {
        "id": f"A-{r['nr']:03d}", "dbid": r["id"], "name": r["name"], "ort": r["ort"], "typ": r["typ"] or "",
        "intervall": r["intervall"], "letzte": r["letzte"], "checkliste": json.loads(r["checkliste"] or "[]"),
    }


# ---------------------------------------------------------------- API-Logik
def benutzer_aus_token(con, auth):
    if not auth or not auth.startswith("Bearer "):
        raise Fehler(401, "Nicht angemeldet")
    r = con.execute(
        "SELECT b.*, f.name AS firma, f.code FROM sitzungen s JOIN benutzer b ON b.id = s.benutzer_id "
        "JOIN firmen f ON f.id = b.firma_id WHERE s.token_hash = ? AND s.ablauf > ?",
        (token_hash(auth[7:].strip()), datetime.utcnow().isoformat()),
    ).fetchone()
    if not r:
        raise Fehler(401, "Sitzung abgelaufen – bitte neu anmelden")
    return r


def ich_json(b):
    d = {"name": b["name"], "email": b["email"], "rolle": b["rolle"], "firma": b["firma"]}
    if b["rolle"] == "chef":
        d["code"] = b["code"]
    return d


def anlage_holen(con, b, anlage_id):
    m = re.fullmatch(r"A-(\d+)", str(anlage_id))
    if not m:
        raise Fehler(400, "Ungültige Anlagen-ID")
    r = con.execute("SELECT * FROM anlagen WHERE firma_id = ? AND nr = ?", (b["firma_id"], int(m.group(1)))).fetchone()
    if not r:
        raise Fehler(404, "Anlage nicht gefunden")
    return r


def nur_chef(b):
    if b["rolle"] != "chef":
        raise Fehler(403, "Nur der Chef darf das")


def api(methode, pfad, daten, auth, ip):
    teile = [p for p in pfad.split("/") if p]
    with db() as con:
        # ---- ohne Anmeldung
        if methode == "POST" and teile == ["registrieren"]:
            login_bremse(ip)
            firma, name = text_feld(daten, "firma", 100), text_feld(daten, "name", 80)
            email, pw = email_feld(daten), passwort_feld(daten)
            if con.execute("SELECT 1 FROM benutzer WHERE email = ?", (email,)).fetchone():
                raise Fehler(409, "Diese E-Mail ist bereits registriert")
            for _ in range(20):
                code = neuer_code(firma)
                if not con.execute("SELECT 1 FROM firmen WHERE code = ?", (code,)).fetchone():
                    break
            fid = con.execute("INSERT INTO firmen (name, code, erstellt) VALUES (?,?,?)", (firma, code, heute())).lastrowid
            bid = con.execute(
                "INSERT INTO benutzer (firma_id, name, email, pw, rolle, erstellt) VALUES (?,?,?,?, 'chef', ?)",
                (fid, name, email, pw_hash(pw), heute()),
            ).lastrowid
            return 201, {"token": sitzung_anlegen(con, bid)}

        if methode == "POST" and teile == ["beitreten"]:
            login_bremse(ip)
            code = text_feld(daten, "code", 20).upper()
            name, email, pw = text_feld(daten, "name", 80), email_feld(daten), passwort_feld(daten)
            f = con.execute("SELECT id FROM firmen WHERE code = ?", (code,)).fetchone()
            if not f:
                raise Fehler(404, "Einladungscode ungültig")
            if con.execute("SELECT 1 FROM benutzer WHERE email = ?", (email,)).fetchone():
                raise Fehler(409, "Diese E-Mail ist bereits registriert")
            bid = con.execute(
                "INSERT INTO benutzer (firma_id, name, email, pw, rolle, erstellt) VALUES (?,?,?,?, 'mitarbeiter', ?)",
                (f["id"], name, email, pw_hash(pw), heute()),
            ).lastrowid
            return 201, {"token": sitzung_anlegen(con, bid)}

        if methode == "POST" and teile == ["login"]:
            login_bremse(ip)
            email = str(daten.get("email", "")).strip().lower()
            r = con.execute("SELECT * FROM benutzer WHERE email = ?", (email,)).fetchone()
            if not r or not pw_pruefen(str(daten.get("passwort", "")), r["pw"]):
                raise Fehler(401, "E-Mail oder Passwort falsch")
            return 200, {"token": sitzung_anlegen(con, r["id"])}

        # ---- ab hier nur mit Anmeldung
        b = benutzer_aus_token(con, auth)
        fid = b["firma_id"]

        if methode == "POST" and teile == ["logout"]:
            con.execute("DELETE FROM sitzungen WHERE token_hash = ?", (token_hash(auth[7:].strip()),))
            return 200, {"ok": True}

        if methode == "GET" and teile == ["ich"]:
            return 200, ich_json(b)

        if methode == "GET" and teile == ["daten"]:
            anlagen = con.execute("SELECT * FROM anlagen WHERE firma_id = ? ORDER BY nr", (fid,)).fetchall()
            nr_von = {a["id"]: f"A-{a['nr']:03d}" for a in anlagen}
            hist = con.execute(
                "SELECT h.* FROM historie h JOIN anlagen a ON a.id = h.anlage_id WHERE a.firma_id = ? ORDER BY h.datum DESC, h.id DESC",
                (fid,),
            ).fetchall()
            stoer = con.execute(
                "SELECT s.* FROM stoerungen s JOIN anlagen a ON a.id = s.anlage_id WHERE a.firma_id = ? AND s.offen = 1 ORDER BY s.datum DESC",
                (fid,),
            ).fetchall()
            return 200, {
                "anlagen": [anlage_json(a) for a in anlagen],
                "historie": [{"anlage": nr_von[h["anlage_id"]], "datum": h["datum"], "art": h["art"], "text": h["text"], "wer": h["wer"]} for h in hist],
                "stoerungen": [{"id": s["id"], "anlage": nr_von[s["anlage_id"]], "datum": s["datum"], "text": s["text"], "prio": s["prio"], "offen": True, "von": s["gemeldet_von"]} for s in stoer],
            }

        if methode == "POST" and teile == ["anlagen"]:
            name, ort, typ = text_feld(daten, "name", 100), text_feld(daten, "ort", 100), text_feld(daten, "typ", 100, False)
            try:
                intervall = int(daten.get("intervall"))
            except (TypeError, ValueError):
                raise Fehler(400, "Intervall muss eine Zahl sein")
            if not 1 <= intervall <= 3650:
                raise Fehler(400, "Intervall muss zwischen 1 und 3650 Tagen liegen")
            letzte = datum_feld(daten, "letzte")
            check = [str(c).strip()[:200] for c in (daten.get("checkliste") or []) if str(c).strip()][:30]
            nr = (con.execute("SELECT COALESCE(MAX(nr), 0) + 1 FROM anlagen WHERE firma_id = ?", (fid,)).fetchone()[0])
            con.execute(
                "INSERT INTO anlagen (firma_id, nr, name, ort, typ, intervall, letzte, checkliste) VALUES (?,?,?,?,?,?,?,?)",
                (fid, nr, name, ort, typ, intervall, letzte, json.dumps(check, ensure_ascii=False)),
            )
            return 201, {"id": f"A-{nr:03d}"}

        if len(teile) == 2 and teile[0] == "anlagen" and methode == "DELETE":
            nur_chef(b)
            a = anlage_holen(con, b, teile[1])
            con.execute("DELETE FROM anlagen WHERE id = ?", (a["id"],))
            return 200, {"ok": True}

        if len(teile) == 3 and teile[0] == "anlagen" and teile[2] == "wartung" and methode == "POST":
            a = anlage_holen(con, b, teile[1])
            text = text_feld(daten, "text", 300, False) or "Wartung nach Checkliste durchgeführt"
            con.execute("UPDATE anlagen SET letzte = ? WHERE id = ?", (heute(), a["id"]))
            con.execute("INSERT INTO historie (anlage_id, datum, art, text, wer) VALUES (?,?,?,?,?)",
                        (a["id"], heute(), "Wartung", text, b["name"]))
            return 200, {"ok": True}

        if methode == "POST" and teile == ["stoerungen"]:
            a = anlage_holen(con, b, daten.get("anlage"))
            text = text_feld(daten, "text", 500)
            prio = daten.get("prio") if daten.get("prio") in ("niedrig", "mittel", "hoch") else "mittel"
            con.execute("INSERT INTO stoerungen (anlage_id, datum, text, prio, gemeldet_von) VALUES (?,?,?,?,?)",
                        (a["id"], heute(), text, prio, b["name"]))
            return 201, {"ok": True}

        if len(teile) == 3 and teile[0] == "stoerungen" and teile[2] == "behoben" and methode == "POST":
            try:
                sid = int(teile[1])
            except ValueError:
                raise Fehler(400, "Ungültige Störungs-ID")
            s = con.execute(
                "SELECT s.* FROM stoerungen s JOIN anlagen a ON a.id = s.anlage_id WHERE s.id = ? AND a.firma_id = ? AND s.offen = 1",
                (sid, fid),
            ).fetchone()
            if not s:
                raise Fehler(404, "Störung nicht gefunden")
            massnahme = text_feld(daten, "massnahme", 300)
            con.execute("UPDATE stoerungen SET offen = 0 WHERE id = ?", (sid,))
            con.execute("INSERT INTO historie (anlage_id, datum, art, text, wer) VALUES (?,?,?,?,?)",
                        (s["anlage_id"], heute(), "Störung", f"{s['text']} – Behoben: {massnahme}", b["name"]))
            return 200, {"ok": True}

        if methode == "GET" and teile == ["team"]:
            nur_chef(b)
            leute = con.execute("SELECT id, name, email, rolle FROM benutzer WHERE firma_id = ? ORDER BY rolle, name", (fid,)).fetchall()
            return 200, {"code": b["code"], "team": [dict(l) for l in leute]}

        if len(teile) == 2 and teile[0] == "team" and methode == "DELETE":
            nur_chef(b)
            try:
                uid = int(teile[1])
            except ValueError:
                raise Fehler(400, "Ungültige ID")
            if uid == b["id"]:
                raise Fehler(400, "Du kannst dich nicht selbst entfernen")
            con.execute("DELETE FROM benutzer WHERE id = ? AND firma_id = ? AND rolle = 'mitarbeiter'", (uid, fid))
            return 200, {"ok": True}

        if methode == "POST" and teile == ["team", "neuer-code"]:
            nur_chef(b)
            code = neuer_code(b["firma"])
            con.execute("UPDATE firmen SET code = ? WHERE id = ?", (code, fid))
            return 200, {"code": code}

    raise Fehler(404, "Unbekannter Endpunkt")


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Wartungsplaner/1.0"

    def log_message(self, fmt, *args):  # keine Logs mit Zugangsdaten
        pass

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ERLAUBTE_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def antwort(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def bearbeiten(self, methode):
        pfad = urlparse(self.path).path
        if not pfad.startswith(PREFIX):
            return self.antwort(404, {"fehler": "Nicht gefunden"})
        daten = {}
        if methode == "POST":
            laenge = int(self.headers.get("Content-Length") or 0)
            if laenge > 20_000:
                return self.antwort(413, {"fehler": "Anfrage zu groß"})
            try:
                daten = json.loads(self.rfile.read(laenge) or b"{}")
                if not isinstance(daten, dict):
                    raise ValueError
            except ValueError:
                return self.antwort(400, {"fehler": "Ungültiges JSON"})
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        try:
            status, obj = api(methode, pfad[len(PREFIX):], daten, self.headers.get("Authorization", ""), ip)
            self.antwort(status, obj)
        except Fehler as f:
            self.antwort(f.status, {"fehler": f.text})
        except Exception:
            self.antwort(500, {"fehler": "Interner Fehler"})

    def do_GET(self):
        self.bearbeiten("GET")

    def do_POST(self):
        self.bearbeiten("POST")

    def do_DELETE(self):
        self.bearbeiten("DELETE")


if __name__ == "__main__":
    init_db()
    print(f"Wartungsplaner-API läuft auf http://{HOST}:{PORT}{PREFIX}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
