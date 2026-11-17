# 🔧 Wartungsplaner mit QR-Codes

Ein digitaler Wartungsplaner für Betriebe und Werkstätten. Läuft im Browser und auf dem Handy, **für das ganze Team**.

**👉 Live:** https://tobias-hauser.github.io/wartungsplaner/
(„Demo ansehen“ funktioniert ohne Anmeldung)

![Übersicht](bilder/uebersicht.png)

## Das Problem

In vielen Betrieben läuft die Wartungsplanung noch über Excel-Listen, Papier oder das Gedächtnis einzelner Kollegen. Die Folgen kennt jeder aus der Instandhaltung:

- Wartungen werden vergessen oder zu spät gemacht
- Bei einer Störung weiß niemand, was an der Anlage zuletzt gemacht wurde
- Störmeldungen gehen per Zuruf verloren

## Die Lösung

Jede Anlage bekommt einen **QR-Aufkleber**. Wer ihn mit dem Handy scannt, sieht sofort:

- ✅ **Status**: in Ordnung / bald fällig / überfällig (Ampelsystem)
- 📋 **Wartungsumfang** als Checkliste
- 🕓 **Historie**: alle Wartungen, Störungen und Freigaben mit Datum und Name
- ⚠️ **Störung melden** direkt an der Anlage, mit Priorität
- 🔒 **Freigabe nach Instandhaltung** mit Werkzeugkontrolle

Chef und Mitarbeiter sehen **dieselben Daten**, egal auf welchem Gerät.

![Anlagen-Detail mit QR-Code](bilder/anlage.png)

## Funktionen

| Funktion | Beschreibung |
|---|---|
| Betrieb & Team | Chef registriert den Betrieb und lädt Mitarbeiter per Einladungscode ein |
| Rollen | Chef: alles inkl. Anlagen löschen und Team verwalten · Mitarbeiter: Wartungen und Störungen |
| Übersicht mit Kennzahlen | Überfällige, bald fällige und offene Störungen auf einen Blick |
| Wartungsintervalle | Nächster Termin wird automatisch aus letzter Wartung + Intervall berechnet |
| Checkliste | Wartung kann erst gespeichert werden, wenn alle Punkte abgehakt sind |
| Störmeldungen | Melden, priorisieren und mit durchgeführter Maßnahme abschließen |
| QR-Etiketten | Pro Anlage ein QR-Code zum Ausdrucken und Aufkleben, der Scan führt direkt zur Anlage |
| **Freigabe nach Instandhaltung** | Vor dem Eingriff wird jedes Werkzeug und Teil erfasst, die Anlage ist gesperrt. Danach muss alles wieder abgehakt werden. Fehlt etwas, gibt es keine Freigabe und automatisch eine Störung mit hoher Priorität. Erst wenn alles vollständig ist, gibt die Produktion mit Namen frei. Schutz vor Fremdkörpern, wichtig für Lebensmittelbetriebe (HACCP / IFS Food) |
| Anlagen verwalten | Anlegen und löschen (mit Sicherheitsabfrage) |
| Demo-Modus | Ausprobieren ohne Anmeldung, Daten nur im eigenen Browser |

## Aufbau

```
Handy / PC ──► Frontend (GitHub Pages)  ──►  API (eigener Linux-Server, nginx + Python)  ──►  SQLite
               HTML · CSS · JavaScript        Login · Rollen · Daten pro Betrieb
```

**Frontend** (`index.html`, `app.js`, `style.css`)
- Reines HTML, CSS und JavaScript, keine Frameworks, keine Installation
- QR-Codes mit [qrcodejs](https://github.com/davidshimjs/qrcodejs)

**Backend** (`server/`)
- Python-Standardbibliothek (kein Framework), SQLite-Datenbank
- Passwörter mit PBKDF2-SHA256 gehasht, Anmeldung per Sitzungs-Token
- Jeder Betrieb sieht nur seine eigenen Daten
- Schutz gegen zu viele Login-Versuche, Eingaben werden geprüft
- Läuft als eigener systemd-Dienst mit eingeschränkten Rechten hinter nginx (HTTPS)

## Ausprobieren

1. Live-Link öffnen → **„Demo ansehen“**
2. Eine Anlage anklicken → QR-Code mit dem Handy scannen
3. „Störung melden“ oder „Wartung erledigt“ ausprobieren
4. Für echten Einsatz: **„Betrieb registrieren“** und Kollegen mit dem Einladungscode einladen

## Ideen für die Weiterentwicklung

- Erinnerung per E-Mail, wenn eine Wartung fällig wird
- Auswertung: welche Anlage fällt am häufigsten aus (MTBF / MTTR)
- Fotos zur Störmeldung anhängen
- Export der Historie als PDF für Prüfungen

---

Entwickelt von **Tobias Hauser**, Mechatroniker (IHK) aus Freiburg, mit KI-Unterstützung.
Idee aus der Praxis: Wartung und Instandhaltung technischer Anlagen.
