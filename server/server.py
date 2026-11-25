#!/usr/bin/env python3
"""
Wartungsplaner – Backend (nur Python-Standardbibliothek)

Stellt eine JSON-API bereit, damit alle Mitarbeiter eines Betriebs
dieselben Anlagen, Wartungen und Störungen sehen.

- Ein Konto für alle Werkzeuge von MechatronikerDev
- Betriebe (Firmen) mit Adresse und Telefon (optional)
- Benutzer mit Rollen (chef / mitarbeiter), Position und einzelnen Rechten
- Mitarbeiter werden per Einladungslink ins Team geholt
- Passwörter gehasht (PBKDF2-SHA256), Sitzungen per Token
- SQLite als Datenbank
"""
import hashlib
import math
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
import urllib.request
import html as htmlmod

HOST = os.environ.get("WP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WP_PORT", "8093"))
DB = os.environ.get("WP_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wartung.db"))
PREFIX = "/wartung-api"
ERLAUBTE_ORIGINS = {
    "https://tobias-hauser.github.io",
    "https://mechatronik-dev.de",
    "https://www.mechatronik-dev.de",
    "http://localhost:8766",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
}
SITZUNG_TAGE = 90
EINLADUNG_TAGE = 14
BESTAETIGEN_STUNDEN = 48
RESET_MINUTEN = 60
# E-Mail-Versand über Resend (Schlüssel kommt aus der Umgebung, nie aus dem Code)
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_DATEI = os.environ.get("WP_MAIL_DATEI", "")  # nur für lokale Tests: Mails in Datei statt verschicken
ABSENDER = "MechatronikerDev <noreply@mechatronik-dev.de>"
ANTWORT_AN = "kontakt@mechatronik-dev.de"
BASIS = os.environ.get("WP_BASIS_URL", "https://mechatronik-dev.de")
# Einzelne Rechte. Der Chef hat immer alle.
RECHTE = ("wartung", "stoerung", "pruefen", "anlagen", "loeschen", "team")
STANDARD_RECHTE = ["wartung", "stoerung", "anlagen"]  # für Mitarbeiter aus der Zeit vor den Rechten
PBKDF2_RUNDEN = 200_000
# Admin-Konten (Benutzer-IDs, durch Komma getrennt) sehen interne Werkzeuge wie den Konsolen-Scanner
ADMINS = {x.strip() for x in os.environ.get("WP_ADMINS", "").split(",") if x.strip()}
SCANNER_DATEI = os.environ.get("WP_SCANNER", os.path.join(os.path.dirname(DB), "scanner.json"))

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
            CREATE TABLE IF NOT EXISTS eingriffe (
                id INTEGER PRIMARY KEY, anlage_id INTEGER NOT NULL REFERENCES anlagen(id) ON DELETE CASCADE,
                start TEXT NOT NULL, grund TEXT NOT NULL, werkzeug TEXT NOT NULL DEFAULT '[]', von TEXT NOT NULL,
                ende TEXT, freigabe_von TEXT, offen INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS einladungen (
                id INTEGER PRIMARY KEY, firma_id INTEGER NOT NULL REFERENCES firmen(id) ON DELETE CASCADE,
                token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, position TEXT NOT NULL DEFAULT '',
                rechte TEXT NOT NULL DEFAULT '[]', erstellt TEXT NOT NULL, ablauf TEXT NOT NULL, von TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS geraete (
                id INTEGER PRIMARY KEY, firma_id INTEGER NOT NULL REFERENCES firmen(id) ON DELETE CASCADE,
                nr INTEGER NOT NULL, bezeichnung TEXT NOT NULL, hersteller TEXT NOT NULL DEFAULT '',
                typ TEXT NOT NULL DEFAULT '', seriennr TEXT NOT NULL DEFAULT '', standort TEXT NOT NULL,
                schutzklasse TEXT NOT NULL CHECK (schutzklasse IN ('I','II','III')),
                laenge REAL NOT NULL DEFAULT 0, heiz_kw REAL NOT NULL DEFAULT 0, intervall INTEGER NOT NULL,
                erstellt TEXT NOT NULL, aktiv INTEGER NOT NULL DEFAULT 1, ausgesondert TEXT NOT NULL DEFAULT '',
                UNIQUE (firma_id, nr));
            CREATE TABLE IF NOT EXISTS pruefungen (
                id INTEGER PRIMARY KEY, geraet_id INTEGER NOT NULL REFERENCES geraete(id) ON DELETE CASCADE,
                datum TEXT NOT NULL, art TEXT NOT NULL, pruefer TEXT NOT NULL, position TEXT NOT NULL DEFAULT '',
                messgeraet TEXT NOT NULL DEFAULT '', sicht TEXT NOT NULL, funktion INTEGER NOT NULL,
                auswertung TEXT NOT NULL, ergebnis TEXT NOT NULL, bemerkung TEXT NOT NULL DEFAULT '',
                naechste TEXT, erstellt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mailtoken (
                token_hash TEXT PRIMARY KEY, benutzer_id INTEGER NOT NULL REFERENCES benutzer(id) ON DELETE CASCADE,
                zweck TEXT NOT NULL CHECK (zweck IN ('bestaetigen','reset')), ablauf TEXT NOT NULL);
            """
        )
        # Neue Spalten für bestehende Datenbanken ergänzen
        spalten = {
            "firmen": [("adresse", "TEXT NOT NULL DEFAULT ''"), ("telefon", "TEXT NOT NULL DEFAULT ''")],
            "benutzer": [("position", "TEXT NOT NULL DEFAULT ''"), ("rechte", "TEXT"),
                         ("bestaetigt", "INTEGER NOT NULL DEFAULT 1")],
        }
        for tabelle, neu in spalten.items():
            vorhanden = {r[1] for r in con.execute(f"PRAGMA table_info({tabelle})")}
            for name, typ in neu:
                if name not in vorhanden:
                    con.execute(f"ALTER TABLE {tabelle} ADD COLUMN {name} {typ}")


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
    def __init__(self, status, text, **extra):
        super().__init__(text)
        self.status, self.text, self.extra = status, text, extra


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


UNBESTAETIGT_TAGE = 7


def aufraeumen(con):
    """Abgelaufene Anmeldungen, Einladungen und Mail-Links löschen, nie bestätigte Konten nach 7 Tagen entfernen."""
    jetzt = datetime.utcnow().isoformat()
    con.execute("DELETE FROM sitzungen WHERE ablauf < ?", (jetzt,))
    con.execute("DELETE FROM einladungen WHERE ablauf < ?", (jetzt,))
    con.execute("DELETE FROM mailtoken WHERE ablauf < ?", (jetzt,))
    grenze = (date.today() - timedelta(days=UNBESTAETIGT_TAGE)).isoformat()
    con.execute("DELETE FROM benutzer WHERE bestaetigt = 0 AND erstellt < ?", (grenze,))
    con.execute("DELETE FROM firmen WHERE id NOT IN (SELECT firma_id FROM benutzer)")


def sitzung_anlegen(con, benutzer_id):
    aufraeumen(con)
    t = secrets.token_urlsafe(32)
    ablauf = (datetime.utcnow() + timedelta(days=SITZUNG_TAGE)).isoformat()
    con.execute("INSERT INTO sitzungen VALUES (?,?,?)", (token_hash(t), benutzer_id, ablauf))
    return t


def rechte_von(b):
    if b["rolle"] == "chef":
        return list(RECHTE)
    if b["rechte"] is None:
        return list(STANDARD_RECHTE)
    try:
        return [r for r in json.loads(b["rechte"]) if r in RECHTE]
    except ValueError:
        return []


def rechte_feld(daten):
    liste = daten.get("rechte") or []
    if not isinstance(liste, list):
        raise Fehler(400, "Ungültige Rechte")
    return [r for r in RECHTE if r in liste]


def darf(b, recht):
    if recht not in rechte_von(b):
        raise Fehler(403, "Dafür fehlt dir die Berechtigung. Frag deinen Chef.")


def passwort_bestaetigt(daten):
    pw = passwort_feld(daten)
    if "passwort2" in daten and str(daten.get("passwort2") or "") != pw:
        raise Fehler(400, "Die Passwörter stimmen nicht überein")
    return pw


def name_aus_email(email):
    return email.split("@")[0][:80]


def zahl_id(teil):
    if not str(teil).isdigit():
        raise Fehler(400, "Ungültige ID")
    return int(teil)


def einladung_holen(con, token):
    e = con.execute(
        "SELECT e.*, f.name AS firma FROM einladungen e JOIN firmen f ON f.id = e.firma_id "
        "WHERE e.token_hash = ? AND e.ablauf > ?", (token_hash(token), datetime.utcnow().isoformat()),
    ).fetchone()
    if not e:
        raise Fehler(404, "Diese Einladung ist ungültig oder abgelaufen. Bitte frag nach einem neuen Link.")
    return e


def einladung_json(e):
    return {"id": e["id"], "name": e["name"], "position": e["position"], "rechte": json.loads(e["rechte"]),
            "erstellt": e["erstellt"], "ablauf": e["ablauf"][:10], "von": e["von"]}


# ---------------------------------------------------------------- E-Mail
def mail_vorlage(titel, absaetze, knopf, link, hinweis):
    """Schlichte HTML-Mail im Stil von MechatronikerDev (Tabellen + Inline-Styles für alle Mailprogramme)."""
    e = htmlmod.escape
    text_html = "".join(
        f'<p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:#2b3036;">{a}</p>' for a in absaetze)
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{e(titel)}</title></head>
<body style="margin:0;padding:0;background:#e9ebed;font-family:Segoe UI,Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#e9ebed;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #c8cdd2;">
  <tr><td style="background:#16191d;padding:18px 28px;border-bottom:3px solid #e35a0c;">
    <span style="font-family:Arial Narrow,Arial,sans-serif;font-weight:bold;font-size:18px;letter-spacing:2px;color:#ffffff;">MECHATRONIKER<span style="color:#e35a0c;">DEV</span></span>
  </td></tr>
  <tr><td style="padding:34px 28px 10px;">
    <p style="margin:0 0 6px;font-family:Consolas,monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#e35a0c;">Konto</p>
    <h1 style="margin:0 0 22px;font-family:Arial Narrow,Arial,sans-serif;font-size:28px;line-height:1.15;text-transform:uppercase;color:#16191d;">{e(titel)}</h1>
    {text_html}
  </td></tr>
  <tr><td style="padding:6px 28px 30px;">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#e35a0c;">
      <a href="{e(link)}" style="display:inline-block;padding:14px 26px;font-size:16px;font-weight:bold;color:#ffffff;text-decoration:none;">{e(knopf)} &rarr;</a>
    </td></tr></table>
    <p style="margin:22px 0 0;font-size:13px;line-height:1.5;color:#5f6770;">Falls der Knopf nicht funktioniert, kopiere diesen Link in deinen Browser:<br>
    <a href="{e(link)}" style="color:#c44c08;word-break:break-all;">{e(link)}</a></p>
  </td></tr>
  <tr><td style="padding:16px 28px;background:#f4f5f6;border-top:1px solid #dde1e4;font-size:13px;line-height:1.5;color:#5f6770;">{hinweis}</td></tr>
  <tr><td style="padding:16px 28px;background:#16191d;font-size:12px;line-height:1.6;color:#8f979f;">
    MechatronikerDev &middot; Werkzeuge für Mechatronik und Instandhaltung<br>
    <a href="{BASIS}/impressum.html" style="color:#c9ced4;">Impressum</a> &middot; <a href="{BASIS}/datenschutz.html" style="color:#c9ced4;">Datenschutz</a>
  </td></tr>
</table>
</td></tr></table></body></html>"""


def mail_senden(an, betreff, html_text, klartext):
    nachricht = {"from": ABSENDER, "to": [an], "reply_to": ANTWORT_AN, "subject": betreff,
                 "html": html_text, "text": klartext}
    if MAIL_DATEI:
        with open(MAIL_DATEI, "a", encoding="utf-8") as f:
            f.write(json.dumps(nachricht, ensure_ascii=False) + "\n")
        return
    if not RESEND_KEY:
        raise Fehler(503, "Der E-Mail-Versand ist gerade nicht verfügbar. Bitte später erneut versuchen.")
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(nachricht).encode(),
                                 headers={"Authorization": "Bearer " + RESEND_KEY, "Content-Type": "application/json",
                                          "User-Agent": "MechatronikerDev/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 300:
                raise Fehler(502, "Die E-Mail konnte nicht verschickt werden. Bitte später erneut versuchen.")
    except Fehler:
        raise
    except Exception:
        raise Fehler(502, "Die E-Mail konnte nicht verschickt werden. Bitte später erneut versuchen.")


def mailtoken_anlegen(con, benutzer_id, zweck, minuten):
    jetzt = datetime.utcnow()
    con.execute("DELETE FROM mailtoken WHERE ablauf < ? OR (benutzer_id = ? AND zweck = ?)",
                (jetzt.isoformat(), benutzer_id, zweck))
    t = secrets.token_urlsafe(32)
    con.execute("INSERT INTO mailtoken VALUES (?,?,?,?)",
                (token_hash(t), benutzer_id, zweck, (jetzt + timedelta(minutes=minuten)).isoformat()))
    return t


def mailtoken_einloesen(con, token, zweck):
    r = con.execute("SELECT * FROM mailtoken WHERE token_hash = ? AND zweck = ? AND ablauf > ?",
                    (token_hash(str(token)), zweck, datetime.utcnow().isoformat())).fetchone()
    if not r:
        raise Fehler(400, "Dieser Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an.")
    con.execute("DELETE FROM mailtoken WHERE token_hash = ?", (r["token_hash"],))
    return r["benutzer_id"]


def bestaetigung_senden(con, benutzer_id, email, name):
    t = mailtoken_anlegen(con, benutzer_id, "bestaetigen", BESTAETIGEN_STUNDEN * 60)
    link = f"{BASIS}/#/bestaetigen/{t}"
    e = htmlmod.escape
    html_text = mail_vorlage(
        "E-Mail bestätigen",
        [f"Hallo {e(name)},", "schön, dass du dabei bist. Bitte bestätige noch kurz deine E-Mail-Adresse, "
         "dann ist dein Konto bei MechatronikerDev freigeschaltet."],
        "E-Mail bestätigen", link,
        f"Der Link ist {BESTAETIGEN_STUNDEN} Stunden gültig. Wenn du dich nicht bei MechatronikerDev registriert hast, "
        "kannst du diese E-Mail einfach ignorieren.")
    klartext = (f"Hallo {name},\n\nbitte bestätige deine E-Mail-Adresse für MechatronikerDev:\n{link}\n\n"
                f"Der Link ist {BESTAETIGEN_STUNDEN} Stunden gültig. Wenn du dich nicht registriert hast, ignoriere diese E-Mail.\n\n"
                f"MechatronikerDev · {BASIS}")
    mail_senden(email, "Bitte bestätige deine E-Mail-Adresse", html_text, klartext)


def reset_senden(con, benutzer_id, email, name):
    t = mailtoken_anlegen(con, benutzer_id, "reset", RESET_MINUTEN)
    link = f"{BASIS}/#/passwort-neu/{t}"
    e = htmlmod.escape
    html_text = mail_vorlage(
        "Neues Passwort",
        [f"Hallo {e(name)},", "für dein Konto bei MechatronikerDev wurde ein neues Passwort angefordert. "
         "Über den Knopf legst du ein neues fest."],
        "Neues Passwort festlegen", link,
        f"Der Link ist {RESET_MINUTEN} Minuten gültig und funktioniert nur einmal. Wenn du kein neues Passwort "
        "angefordert hast, ignoriere diese E-Mail. Dein bisheriges Passwort bleibt dann gültig.")
    klartext = (f"Hallo {name},\n\nüber diesen Link legst du ein neues Passwort für MechatronikerDev fest:\n{link}\n\n"
                f"Der Link ist {RESET_MINUTEN} Minuten gültig. Wenn du kein neues Passwort angefordert hast, ignoriere diese E-Mail.\n\n"
                f"MechatronikerDev · {BASIS}")
    mail_senden(email, "Neues Passwort für MechatronikerDev", html_text, klartext)


def anlage_json(r):
    return {
        "id": f"A-{r['nr']:03d}", "dbid": r["id"], "name": r["name"], "ort": r["ort"], "typ": r["typ"] or "",
        "intervall": r["intervall"], "letzte": r["letzte"], "checkliste": json.loads(r["checkliste"] or "[]"),
    }


# ---------------------------------------------------------------- Prüfprotokoll (DGUV V3)
# Grenzwerte für ortsveränderliche Geräte nach DIN EN 50699 (VDE 0702) und DIN EN 50678 (VDE 0701),
# gleiche Werte wie in der früheren DIN VDE 0701-0702. Die Bewertung rechnet immer der Server.
SICHTPRUEFUNG = (
    ("gehaeuse", "Gehäuse unbeschädigt, keine Brand- oder Schmorspuren"),
    ("leitung", "Anschlussleitung unbeschädigt, keine Knicke, Quetschungen oder Flickstellen"),
    ("stecker", "Stecker unbeschädigt, Kontakte und Schutzkontakt in Ordnung"),
    ("zugentlastung", "Zugentlastung und Knickschutz wirksam"),
    ("schalter", "Schalter, Bedienteile und Sicherungshalter in Ordnung"),
    ("abdeckungen", "Schutzabdeckungen vollständig, keine unzulässigen Eingriffe oder Umbauten"),
    ("aufschriften", "Typschild und Sicherheitsaufschriften vorhanden und lesbar"),
    ("zustand", "Keine Verschmutzung oder Feuchtigkeit, Lüftungsöffnungen frei"),
)
PRUEFARTEN = ("Wiederholungsprüfung", "Prüfung nach Instandsetzung", "Erstprüfung")
MESSUNGEN = {
    "rpe": ("Schutzleiterwiderstand", "Ω"),
    "riso": ("Isolationswiderstand", "MΩ"),
    "ipe": ("Schutzleiterstrom", "mA"),
    "ib": ("Berührungsstrom", "mA"),
}


def grenzwerte(sk, laenge=0.0, heiz_kw=0.0):
    """Grenzwerte je Schutzklasse. max = höchstens, min = mindestens."""
    if sk == "I":
        rpe = 0.3 if laenge <= 5 else min(1.0, round(0.3 + 0.1 * math.ceil((laenge - 5) / 7.5 - 1e-9), 2))
        return {"rpe": {"max": rpe}, "riso": {"min": 0.3 if heiz_kw > 0 else 1.0},
                "ipe": {"max": min(10.0, round(heiz_kw, 2)) if heiz_kw > 3.5 else 3.5}}
    if sk == "II":
        return {"riso": {"min": 2.0}, "ib": {"max": 0.5}}
    return {"riso": {"min": 0.25}}


def pflicht_messungen(sk):
    """Liste von Gruppen: aus jeder Gruppe muss mindestens ein Wert gemessen sein."""
    if sk == "I":
        return [["rpe"], ["riso", "ipe"]]
    if sk == "II":
        return [["riso", "ib"]]
    return [["riso"]]


def pruefung_bewerten(sk, laenge, heiz_kw, messwerte, sicht, funktion):
    gw = grenzwerte(sk, laenge, heiz_kw)
    werte = {}
    for k, v in (messwerte or {}).items():
        if k not in gw or v in (None, ""):
            continue
        try:
            z = float(str(v).replace(",", "."))
        except ValueError:
            raise Fehler(400, f"Messwert für {MESSUNGEN[k][0]} ist keine Zahl")
        if z != z or not (0 <= z < 1e6):
            raise Fehler(400, f"Messwert für {MESSUNGEN[k][0]} ist ungültig")
        werte[k] = z
    for gruppe in pflicht_messungen(sk):
        if not any(k in werte for k in gruppe):
            namen = " oder ".join(MESSUNGEN[k][0] for k in gruppe)
            raise Fehler(400, f"Für Schutzklasse {sk} fehlt die Messung: {namen}")
    auswertung = []
    for k, g in gw.items():
        if k not in werte:
            continue
        w = werte[k]
        ok = w <= g["max"] if "max" in g else w >= g["min"]
        auswertung.append({"messung": k, "name": MESSUNGEN[k][0], "einheit": MESSUNGEN[k][1], "wert": w,
                           "grenze": g.get("max", g.get("min")), "art": "max" if "max" in g else "min", "ok": ok})
    maengel = [k for k, _ in SICHTPRUEFUNG if sicht.get(k) is not True]
    bestanden = not maengel and funktion and all(a["ok"] for a in auswertung)
    return auswertung, maengel, bestanden


def monate_dazu(iso, monate):
    d = date.fromisoformat(iso)
    m = d.month - 1 + monate
    j, m = d.year + m // 12, m % 12 + 1
    tage = [31, 29 if (j % 4 == 0 and (j % 100 != 0 or j % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(j, m, min(d.day, tage)).isoformat()


def geraet_json(r):
    return {"id": f"G-{r['nr']:03d}", "bezeichnung": r["bezeichnung"], "hersteller": r["hersteller"], "typ": r["typ"],
            "seriennr": r["seriennr"], "standort": r["standort"], "schutzklasse": r["schutzklasse"],
            "laenge": r["laenge"], "heiz_kw": r["heiz_kw"], "intervall": r["intervall"], "erstellt": r["erstellt"],
            "aktiv": bool(r["aktiv"]), "ausgesondert": r["ausgesondert"]}


def geraet_holen(con, b, gid):
    m = re.fullmatch(r"G-(\d+)", str(gid))
    if not m:
        raise Fehler(400, "Ungültige Geräte-ID")
    r = con.execute("SELECT * FROM geraete WHERE firma_id = ? AND nr = ?", (b["firma_id"], int(m.group(1)))).fetchone()
    if not r:
        raise Fehler(404, "Gerät nicht gefunden")
    return r


def zahl_feld(daten, feld, minimum, maximum, standard=None):
    v = daten.get(feld, standard)
    if v in (None, ""):
        v = standard
    try:
        z = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise Fehler(400, f"Feld '{feld}' muss eine Zahl sein")
    if not minimum <= z <= maximum:
        raise Fehler(400, f"Feld '{feld}' liegt außerhalb des erlaubten Bereichs")
    return z


def geraet_felder(daten):
    sk = str(daten.get("schutzklasse", ""))
    if sk not in ("I", "II", "III"):
        raise Fehler(400, "Bitte die Schutzklasse wählen")
    intervall = int(zahl_feld(daten, "intervall", 1, 48))
    return (text_feld(daten, "bezeichnung", 100), text_feld(daten, "hersteller", 80, False),
            text_feld(daten, "typ", 80, False), text_feld(daten, "seriennr", 60, False),
            text_feld(daten, "standort", 100), sk,
            zahl_feld(daten, "laenge", 0, 100, 0) if sk == "I" else 0.0,
            zahl_feld(daten, "heiz_kw", 0, 50, 0) if sk == "I" else 0.0, intervall)


def pruef_api(con, b, methode, teile, daten):
    """Endpunkte unter /pruef. Gibt None zurück, wenn der Pfad nicht passt."""
    fid = b["firma_id"]
    if methode == "GET" and teile == ["daten"]:
        geraete = con.execute("SELECT * FROM geraete WHERE firma_id = ? ORDER BY nr", (fid,)).fetchall()
        nr_von = {g["id"]: f"G-{g['nr']:03d}" for g in geraete}
        pr = con.execute("SELECT p.* FROM pruefungen p JOIN geraete g ON g.id = p.geraet_id WHERE g.firma_id = ? "
                         "ORDER BY p.datum DESC, p.id DESC", (fid,)).fetchall()
        return 200, {
            "geraete": [geraet_json(g) for g in geraete],
            "pruefungen": [{"id": p["id"], "geraet": nr_von[p["geraet_id"]], "datum": p["datum"], "art": p["art"],
                            "pruefer": p["pruefer"], "position": p["position"], "messgeraet": p["messgeraet"],
                            "sicht": json.loads(p["sicht"]), "funktion": bool(p["funktion"]),
                            "auswertung": json.loads(p["auswertung"]), "ergebnis": p["ergebnis"],
                            "bemerkung": p["bemerkung"], "naechste": p["naechste"]} for p in pr],
            "betrieb": {"name": b["firma"] or "", "adresse": b["adresse"] or ""},
        }

    if methode == "POST" and teile == ["geraete"]:
        darf(b, "anlagen")
        f = geraet_felder(daten)
        if con.execute("SELECT COUNT(*) FROM geraete WHERE firma_id = ?", (fid,)).fetchone()[0] >= 2000:
            raise Fehler(400, "Zu viele Geräte")
        nr = con.execute("SELECT COALESCE(MAX(nr), 0) + 1 FROM geraete WHERE firma_id = ?", (fid,)).fetchone()[0]
        con.execute("INSERT INTO geraete (firma_id, nr, bezeichnung, hersteller, typ, seriennr, standort, schutzklasse, "
                    "laenge, heiz_kw, intervall, erstellt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (fid, nr) + f + (heute(),))
        return 201, {"id": f"G-{nr:03d}"}

    if len(teile) == 2 and teile[0] == "geraete" and methode == "POST":
        darf(b, "anlagen")
        g = geraet_holen(con, b, teile[1])
        f = geraet_felder(daten)
        con.execute("UPDATE geraete SET bezeichnung=?, hersteller=?, typ=?, seriennr=?, standort=?, schutzklasse=?, "
                    "laenge=?, heiz_kw=?, intervall=? WHERE id = ?", f + (g["id"],))
        return 200, {"ok": True}

    if len(teile) == 3 and teile[0] == "geraete" and teile[2] == "aussondern" and methode == "POST":
        darf(b, "loeschen")
        g = geraet_holen(con, b, teile[1])
        grund = text_feld(daten, "grund", 200)
        con.execute("UPDATE geraete SET aktiv = 0, ausgesondert = ? WHERE id = ?",
                    (f"{heute()}: {grund} ({b['name']})", g["id"]))
        return 200, {"ok": True}

    if len(teile) == 3 and teile[0] == "geraete" and teile[2] == "pruefung" and methode == "POST":
        darf(b, "pruefen")
        g = geraet_holen(con, b, teile[1])
        if not g["aktiv"]:
            raise Fehler(400, "Dieses Gerät ist ausgesondert")
        art = daten.get("art") if daten.get("art") in PRUEFARTEN else PRUEFARTEN[0]
        datum = datum_feld(daten, "datum")
        if datum > heute() or datum < (date.today() - timedelta(days=366)).isoformat():
            raise Fehler(400, "Das Prüfdatum darf nicht in der Zukunft und höchstens ein Jahr zurück liegen")
        sicht_roh = daten.get("sicht") or {}
        if not isinstance(sicht_roh, dict) or any(sicht_roh.get(k) not in (True, False) for k, _ in SICHTPRUEFUNG):
            raise Fehler(400, "Bitte jeden Punkt der Sichtprüfung bewerten")
        sicht = {k: sicht_roh[k] for k, _ in SICHTPRUEFUNG}
        if daten.get("funktion") not in (True, False):
            raise Fehler(400, "Bitte die Funktionsprüfung bewerten")
        funktion = daten["funktion"]
        messwerte = daten.get("messwerte") or {}
        if not isinstance(messwerte, dict):
            raise Fehler(400, "Ungültige Messwerte")
        auswertung, maengel, bestanden = pruefung_bewerten(g["schutzklasse"], g["laenge"], g["heiz_kw"], messwerte, sicht, funktion)
        bemerkung = text_feld(daten, "bemerkung", 500, False)
        if not bestanden and not bemerkung:
            raise Fehler(400, "Bei nicht bestandener Prüfung bitte den Mangel und die Maßnahme beschreiben")
        ergebnis = "bestanden" if bestanden else "nicht bestanden"
        naechste = monate_dazu(datum, g["intervall"]) if bestanden else None
        pid = con.execute(
            "INSERT INTO pruefungen (geraet_id, datum, art, pruefer, position, messgeraet, sicht, funktion, auswertung, "
            "ergebnis, bemerkung, naechste, erstellt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g["id"], datum, art, b["name"], b["position"] or "", text_feld(daten, "messgeraet", 120, False),
             json.dumps(sicht), 1 if funktion else 0, json.dumps(auswertung, ensure_ascii=False), ergebnis, bemerkung,
             naechste, datetime.utcnow().isoformat())).lastrowid
        return 201, {"id": pid, "ergebnis": ergebnis, "naechste": naechste, "maengel": maengel, "auswertung": auswertung}
    return None


# ---------------------------------------------------------------- API-Logik
def benutzer_aus_token(con, auth):
    if not auth or not auth.startswith("Bearer "):
        raise Fehler(401, "Nicht angemeldet")
    r = con.execute(
        "SELECT b.*, f.name AS firma, f.code, f.adresse, f.telefon FROM sitzungen s JOIN benutzer b ON b.id = s.benutzer_id "
        "JOIN firmen f ON f.id = b.firma_id WHERE s.token_hash = ? AND s.ablauf > ?",
        (token_hash(auth[7:].strip()), datetime.utcnow().isoformat()),
    ).fetchone()
    if not r:
        raise Fehler(401, "Sitzung abgelaufen. Bitte neu anmelden.")
    return r


def ist_admin(b):
    return str(b["id"]) in ADMINS


def ich_json(b):
    d = {"name": b["name"], "email": b["email"], "rolle": b["rolle"], "position": b["position"] or "",
         "rechte": rechte_von(b), "firma": b["firma"],
         "firmendaten": {"name": b["firma"], "adresse": b["adresse"] or "", "telefon": b["telefon"] or ""}}
    if ist_admin(b):
        d["admin"] = True
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


def mitglied_holen(con, b, teil):
    m = con.execute("SELECT * FROM benutzer WHERE id = ? AND firma_id = ?", (zahl_id(teil), b["firma_id"])).fetchone()
    if not m:
        raise Fehler(404, "Mitarbeiter nicht gefunden")
    if m["rolle"] == "chef":
        raise Fehler(400, "Der Chef kann hier nicht geändert werden")
    if m["id"] == b["id"]:
        raise Fehler(400, "Deine eigenen Rechte kannst du nicht ändern")
    return m


def api(methode, pfad, daten, auth, ip):
    teile = [p for p in pfad.split("/") if p]
    with db() as con:
        # ---- ohne Anmeldung
        if methode == "POST" and teile == ["registrieren"]:
            login_bremse(ip)
            email, pw = email_feld(daten), passwort_bestaetigt(daten)
            aufraeumen(con)
            name = text_feld(daten, "name", 80, False) or name_aus_email(email)
            firma = text_feld(daten, "firma", 100, False)
            adresse = text_feld(daten, "adresse", 200, False)
            telefon = text_feld(daten, "telefon", 40, False)
            if con.execute("SELECT 1 FROM benutzer WHERE email = ?", (email,)).fetchone():
                raise Fehler(409, "Diese E-Mail ist bereits registriert")
            for _ in range(20):
                code = neuer_code(firma or name)
                if not con.execute("SELECT 1 FROM firmen WHERE code = ?", (code,)).fetchone():
                    break
            fid = con.execute("INSERT INTO firmen (name, code, erstellt, adresse, telefon) VALUES (?,?,?,?,?)",
                              (firma, code, heute(), adresse, telefon)).lastrowid
            bid = con.execute(
                "INSERT INTO benutzer (firma_id, name, email, pw, rolle, erstellt, bestaetigt) VALUES (?,?,?,?, 'chef', ?, 0)",
                (fid, name, email, pw_hash(pw), heute()),
            ).lastrowid
            bestaetigung_senden(con, bid, email, name)
            return 201, {"bestaetigen": True, "email": email}

        # ---- E-Mail bestätigen, Bestätigung erneut senden
        if methode == "POST" and teile == ["bestaetigen"]:
            login_bremse(ip)
            bid = mailtoken_einloesen(con, daten.get("token", ""), "bestaetigen")
            con.execute("UPDATE benutzer SET bestaetigt = 1 WHERE id = ?", (bid,))
            return 200, {"token": sitzung_anlegen(con, bid)}

        if methode == "POST" and teile == ["bestaetigung-erneut"]:
            login_bremse(ip)
            email = str(daten.get("email", "")).strip().lower()
            r = con.execute("SELECT * FROM benutzer WHERE email = ? AND bestaetigt = 0", (email,)).fetchone()
            if r:
                bestaetigung_senden(con, r["id"], r["email"], r["name"])
            return 200, {"ok": True}

        # ---- Passwort vergessen (immer gleiche Antwort, damit niemand Adressen ausprobieren kann)
        if methode == "POST" and teile == ["passwort-vergessen"]:
            login_bremse(ip)
            email = str(daten.get("email", "")).strip().lower()
            r = con.execute("SELECT * FROM benutzer WHERE email = ?", (email,)).fetchone()
            if r:
                reset_senden(con, r["id"], r["email"], r["name"])
            return 200, {"ok": True}

        if methode == "POST" and teile == ["passwort-neu"]:
            login_bremse(ip)
            pw = passwort_bestaetigt(daten)
            bid = mailtoken_einloesen(con, daten.get("token", ""), "reset")
            con.execute("UPDATE benutzer SET pw = ?, bestaetigt = 1 WHERE id = ?", (pw_hash(pw), bid))
            con.execute("DELETE FROM sitzungen WHERE benutzer_id = ?", (bid,))
            return 200, {"token": sitzung_anlegen(con, bid)}

        # ---- Einladungslink ansehen und annehmen (ohne Anmeldung)
        if len(teile) == 2 and teile[0] == "einladung" and methode == "GET":
            e = einladung_holen(con, teile[1])
            return 200, {"firma": e["firma"], "name": e["name"], "position": e["position"],
                         "rechte": json.loads(e["rechte"]), "von": e["von"]}

        if len(teile) == 2 and teile[0] == "einladung" and methode == "POST":
            login_bremse(ip)
            e = einladung_holen(con, teile[1])
            email, pw = email_feld(daten), passwort_bestaetigt(daten)
            name = text_feld(daten, "name", 80, False) or e["name"]
            if con.execute("SELECT 1 FROM benutzer WHERE email = ?", (email,)).fetchone():
                raise Fehler(409, "Diese E-Mail ist bereits registriert")
            bid = con.execute(
                "INSERT INTO benutzer (firma_id, name, email, pw, rolle, erstellt, position, rechte, bestaetigt) "
                "VALUES (?,?,?,?, 'mitarbeiter', ?, ?, ?, 0)",
                (e["firma_id"], name, email, pw_hash(pw), heute(), e["position"], e["rechte"]),
            ).lastrowid
            con.execute("DELETE FROM einladungen WHERE id = ?", (e["id"],))
            bestaetigung_senden(con, bid, email, name)
            return 201, {"bestaetigen": True, "email": email}

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
            if not r["bestaetigt"]:
                raise Fehler(403, "Bitte bestätige zuerst deine E-Mail-Adresse. Den Link haben wir dir per E-Mail geschickt.",
                             unbestaetigt=True)
            return 200, {"token": sitzung_anlegen(con, r["id"])}

        # ---- ab hier nur mit Anmeldung
        b = benutzer_aus_token(con, auth)
        fid = b["firma_id"]

        if methode == "POST" and teile == ["logout"]:
            con.execute("DELETE FROM sitzungen WHERE token_hash = ?", (token_hash(auth[7:].strip()),))
            return 200, {"ok": True}

        if methode == "GET" and teile == ["ich"]:
            return 200, ich_json(b)

        # ---- Konsolen-Scanner: nur für Admin-Konten (WP_ADMINS = Benutzer-IDs)
        if methode == "GET" and teile == ["scanner"]:
            if not ist_admin(b):
                raise Fehler(404, "Unbekannter Endpunkt")
            try:
                with open(SCANNER_DATEI, encoding="utf-8") as f:
                    return 200, json.load(f)
            except (OSError, ValueError):
                return 200, {"ok": False, "fehler": "Noch kein Scan gelaufen", "angebote": [], "modelle": {}}

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
            eingr = {
                e["anlage_id"]: {"id": e["id"], "start": e["start"], "grund": e["grund"], "werkzeug": json.loads(e["werkzeug"]), "von": e["von"]}
                for e in con.execute(
                    "SELECT e.* FROM eingriffe e JOIN anlagen a ON a.id = e.anlage_id WHERE a.firma_id = ? AND e.offen = 1", (fid,)
                ).fetchall()
            }
            anl = []
            for a in anlagen:
                j = anlage_json(a)
                if a["id"] in eingr:
                    j["eingriff"] = eingr[a["id"]]
                anl.append(j)
            return 200, {
                "anlagen": anl,
                "historie": [{"anlage": nr_von[h["anlage_id"]], "datum": h["datum"], "art": h["art"], "text": h["text"], "wer": h["wer"]} for h in hist],
                "stoerungen": [{"id": s["id"], "anlage": nr_von[s["anlage_id"]], "datum": s["datum"], "text": s["text"], "prio": s["prio"], "offen": True, "von": s["gemeldet_von"]} for s in stoer],
            }

        if methode == "POST" and teile == ["anlagen"]:
            darf(b, "anlagen")
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
            darf(b, "loeschen")
            a = anlage_holen(con, b, teile[1])
            con.execute("DELETE FROM anlagen WHERE id = ?", (a["id"],))
            return 200, {"ok": True}

        if len(teile) == 3 and teile[0] == "anlagen" and teile[2] == "wartung" and methode == "POST":
            darf(b, "wartung")
            a = anlage_holen(con, b, teile[1])
            text = text_feld(daten, "text", 300, False) or "Wartung nach Checkliste durchgeführt"
            con.execute("UPDATE anlagen SET letzte = ? WHERE id = ?", (heute(), a["id"]))
            con.execute("INSERT INTO historie (anlage_id, datum, art, text, wer) VALUES (?,?,?,?,?)",
                        (a["id"], heute(), "Wartung", text, b["name"]))
            return 200, {"ok": True}

        # ---- Eingriff starten (Anlage geht in Instandhaltung, Werkzeug wird erfasst)
        if len(teile) == 3 and teile[0] == "anlagen" and teile[2] == "eingriff" and methode == "POST":
            darf(b, "wartung")
            a = anlage_holen(con, b, teile[1])
            if con.execute("SELECT 1 FROM eingriffe WHERE anlage_id = ? AND offen = 1", (a["id"],)).fetchone():
                raise Fehler(409, "An dieser Anlage läuft bereits ein Eingriff")
            grund = text_feld(daten, "grund", 200)
            werkzeug = [str(w).strip()[:80] for w in (daten.get("werkzeug") or []) if str(w).strip()][:40]
            if not werkzeug:
                raise Fehler(400, "Bitte mindestens ein Werkzeug oder Teil erfassen")
            jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")
            con.execute("INSERT INTO eingriffe (anlage_id, start, grund, werkzeug, von) VALUES (?,?,?,?,?)",
                        (a["id"], jetzt, grund, json.dumps(werkzeug, ensure_ascii=False), b["name"]))
            return 201, {"ok": True}

        # ---- Eingriff abschließen: Werkzeugkontrolle + Freigabe durch Produktion
        if len(teile) == 4 and teile[0] == "anlagen" and teile[2] == "eingriff" and teile[3] == "abschluss" and methode == "POST":
            darf(b, "wartung")
            a = anlage_holen(con, b, teile[1])
            e = con.execute("SELECT * FROM eingriffe WHERE anlage_id = ? AND offen = 1", (a["id"],)).fetchone()
            if not e:
                raise Fehler(404, "Kein offener Eingriff an dieser Anlage")
            werkzeug = json.loads(e["werkzeug"])
            fehlend = [w for w in (daten.get("fehlend") or []) if w in werkzeug]
            jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")
            if fehlend:
                text = "Nach Instandhaltung fehlt: " + ", ".join(fehlend) + ". Anlage NICHT freigeben, Suche einleiten (Fremdkörpergefahr)."
                con.execute("INSERT INTO stoerungen (anlage_id, datum, text, prio, gemeldet_von) VALUES (?,?,?,?,?)",
                            (a["id"], heute(), text[:500], "hoch", b["name"]))
                return 200, {"freigegeben": False, "fehlend": fehlend}
            checks = daten.get("checks") or {}
            for k in ("werkzeug_vollstaendig", "keine_losen_teile", "schutz_montiert", "gereinigt"):
                if checks.get(k) is not True:
                    raise Fehler(400, "Bitte alle Prüfpunkte bestätigen")
            freigabe = text_feld(daten, "freigabe_von", 80)
            bem = text_feld(daten, "bemerkung", 300, False)
            con.execute("UPDATE eingriffe SET offen = 0, ende = ?, freigabe_von = ? WHERE id = ?", (jetzt, freigabe, e["id"]))
            protokoll = (f"{e['grund']}. Eingriff {e['start']} bis {jetzt[11:]} Uhr. Werkzeug vollständig zurück "
                         f"({len(werkzeug)} Positionen), keine losen Teile, Schutz montiert, gereinigt. "
                         f"Freigegeben durch {freigabe}." + (f" Bemerkung: {bem}" if bem else ""))
            con.execute("INSERT INTO historie (anlage_id, datum, art, text, wer) VALUES (?,?,?,?,?)",
                        (a["id"], heute(), "Freigabe", protokoll, b["name"]))
            return 200, {"freigegeben": True}

        if methode == "POST" and teile == ["stoerungen"]:
            darf(b, "stoerung")
            a = anlage_holen(con, b, daten.get("anlage"))
            text = text_feld(daten, "text", 500)
            prio = daten.get("prio") if daten.get("prio") in ("niedrig", "mittel", "hoch") else "mittel"
            con.execute("INSERT INTO stoerungen (anlage_id, datum, text, prio, gemeldet_von) VALUES (?,?,?,?,?)",
                        (a["id"], heute(), text, prio, b["name"]))
            return 201, {"ok": True}

        if len(teile) == 3 and teile[0] == "stoerungen" and teile[2] == "behoben" and methode == "POST":
            darf(b, "stoerung")
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
                        (s["anlage_id"], heute(), "Störung", f"{s['text']}. Behoben: {massnahme}", b["name"]))
            return 200, {"ok": True}

        # ---- Prüfprotokoll DGUV V3
        if teile and teile[0] == "pruef":
            r = pruef_api(con, b, methode, teile[1:], daten)
            if r:
                return r
            raise Fehler(404, "Unbekannter Endpunkt")

        # ---- Firmendaten (nur Chef)
        if methode == "POST" and teile == ["firma"]:
            nur_chef(b)
            con.execute("UPDATE firmen SET name = ?, adresse = ?, telefon = ? WHERE id = ?",
                        (text_feld(daten, "firma", 100, False), text_feld(daten, "adresse", 200, False),
                         text_feld(daten, "telefon", 40, False), fid))
            return 200, {"ok": True}

        # ---- Eigener Name
        if methode == "POST" and teile == ["profil"]:
            name = text_feld(daten, "name", 80, False) or name_aus_email(b["email"])
            con.execute("UPDATE benutzer SET name = ? WHERE id = ?", (name, b["id"]))
            return 200, {"ok": True}

        # ---- Team: Mitglieder und offene Einladungen
        if methode == "GET" and teile == ["team"]:
            darf(b, "team")
            leute = con.execute("SELECT * FROM benutzer WHERE firma_id = ? ORDER BY rolle, name", (fid,)).fetchall()
            offen = con.execute("SELECT * FROM einladungen WHERE firma_id = ? AND ablauf > ? ORDER BY id DESC",
                                (fid, datetime.utcnow().isoformat())).fetchall()
            return 200, {
                "mitglieder": [{"id": l["id"], "name": l["name"], "email": l["email"], "rolle": l["rolle"],
                                "position": l["position"] or "", "rechte": rechte_von(l), "seit": l["erstellt"],
                                "ich": l["id"] == b["id"]} for l in leute],
                "einladungen": [einladung_json(x) for x in offen],
            }

        if methode == "POST" and teile == ["einladungen"]:
            darf(b, "team")
            name = text_feld(daten, "name", 80)
            position = text_feld(daten, "position", 80, False)
            rechte = rechte_feld(daten)
            if "team" in rechte and b["rolle"] != "chef":
                raise Fehler(403, "Nur der Chef darf das Recht zur Mitarbeiterverwaltung vergeben")
            if con.execute("SELECT COUNT(*) FROM einladungen WHERE firma_id = ?", (fid,)).fetchone()[0] >= 50:
                raise Fehler(400, "Zu viele offene Einladungen")
            t = secrets.token_urlsafe(24)
            ablauf = (datetime.utcnow() + timedelta(days=EINLADUNG_TAGE)).isoformat()
            eid = con.execute(
                "INSERT INTO einladungen (firma_id, token_hash, name, position, rechte, erstellt, ablauf, von) VALUES (?,?,?,?,?,?,?,?)",
                (fid, token_hash(t), name, position, json.dumps(rechte), heute(), ablauf, b["name"]),
            ).lastrowid
            return 201, {"id": eid, "token": t, "ablauf": ablauf[:10]}

        if len(teile) == 3 and teile[0] == "einladungen" and teile[2] == "link" and methode == "POST":
            darf(b, "team")
            t = secrets.token_urlsafe(24)
            ablauf = (datetime.utcnow() + timedelta(days=EINLADUNG_TAGE)).isoformat()
            n = con.execute("UPDATE einladungen SET token_hash = ?, ablauf = ? WHERE id = ? AND firma_id = ?",
                            (token_hash(t), ablauf, zahl_id(teile[1]), fid)).rowcount
            if not n:
                raise Fehler(404, "Einladung nicht gefunden")
            return 200, {"token": t, "ablauf": ablauf[:10]}

        if len(teile) == 2 and teile[0] == "einladungen" and methode == "DELETE":
            darf(b, "team")
            con.execute("DELETE FROM einladungen WHERE id = ? AND firma_id = ?", (zahl_id(teile[1]), fid))
            return 200, {"ok": True}

        if len(teile) == 2 and teile[0] == "team" and methode == "POST":
            darf(b, "team")
            m = mitglied_holen(con, b, teile[1])
            rechte = rechte_feld(daten)
            if b["rolle"] != "chef" and ("team" in rechte) != ("team" in rechte_von(m)):
                raise Fehler(403, "Nur der Chef darf das Recht zur Mitarbeiterverwaltung ändern")
            con.execute("UPDATE benutzer SET position = ?, rechte = ? WHERE id = ?",
                        (text_feld(daten, "position", 80, False), json.dumps(rechte), m["id"]))
            return 200, {"ok": True}

        if len(teile) == 2 and teile[0] == "team" and methode == "DELETE":
            darf(b, "team")
            m = mitglied_holen(con, b, teile[1])
            con.execute("DELETE FROM benutzer WHERE id = ?", (m["id"],))
            return 200, {"ok": True}

        if methode == "POST" and teile == ["team", "neuer-code"]:
            nur_chef(b)
            code = neuer_code(b["firma"] or b["name"])
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
            self.antwort(f.status, dict({"fehler": f.text}, **f.extra))
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
