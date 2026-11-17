// Wartungsplaner – Anlagen, Wartungsintervalle, Störmeldungen und QR-Codes
// Zwei Modi:
//   • Server: Login, Daten liegen zentral – alle im Team sehen dasselbe
//   • Demo:   ohne Anmeldung, Daten nur im eigenen Browser

const API = ['localhost', '127.0.0.1'].includes(location.hostname)
  ? 'http://localhost:8093/wartung-api'
  : 'https://off-location.cloud/wartung-api';
const TOKEN_KEY = 'wartungsplaner-token';
const DEMO_KEY = 'wartungsplaner-v2';
const DEMO_AKTIV = 'wartungsplaner-demo';
const BALD_TAGE = 14; // ab wie vielen Tagen vor Fälligkeit "bald fällig"

let modus = null;           // 'server' | 'demo' | null (nicht angemeldet)
let ich = null;             // angemeldeter Benutzer (Server-Modus)
let daten = { anlagen: [], historie: [], stoerungen: [] };

// ---------- kleine Helfer ----------
const $ = (id) => document.getElementById(id);
const heuteISO = () => new Date().toISOString().slice(0, 10);
const fmt = (d) => new Date(d).toLocaleDateString('de-DE');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
const lsDel = (k) => { try { localStorage.removeItem(k); } catch (e) {} };
const darfLoeschen = () => modus === 'demo' || (ich && ich.rolle === 'chef');

// ---------- Server-Anbindung ----------
async function api(methode, pfad, body) {
  const t = lsGet(TOKEN_KEY);
  const r = await fetch(API + pfad, {
    method: methode,
    headers: { 'Content-Type': 'application/json', ...(t ? { Authorization: 'Bearer ' + t } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && t && !pfad.startsWith('/login')) { abmeldenLokal(); throw new Error(j.fehler || 'Bitte neu anmelden'); }
  if (!r.ok) throw new Error(j.fehler || 'Fehler ' + r.status);
  return j;
}

// ---------- Demo-Daten ----------
function tageZurueck(n) { const d = new Date(); d.setDate(d.getDate() - n); return d.toISOString().slice(0, 10); }
function demoDaten() {
  return {
    anlagen: [
      { id: 'A-001', name: 'Motor 1', ort: 'Halle 1', typ: 'Drehstrommotor', intervall: 90, letzte: tageZurueck(97),
        checkliste: ['Lager auf Geräusche prüfen', 'Temperatur im Betrieb messen', 'Anschlussklemmen nachziehen', 'Isolationswiderstand messen'] },
      { id: 'A-002', name: 'Frequenzumrichter 2', ort: 'Schaltschrank 2', typ: 'Frequenzumrichter', intervall: 180, letzte: tageZurueck(172),
        checkliste: ['Lüfter und Kühlkörper reinigen', 'Fehlerspeicher auslesen', 'Anschlüsse auf festen Sitz prüfen'] },
      { id: 'A-003', name: 'Pumpe 1', ort: 'Halle 1', typ: 'Kreiselpumpe', intervall: 90, letzte: tageZurueck(30),
        checkliste: ['Dichtung auf Leckage prüfen', 'Druck und Durchfluss kontrollieren', 'Lagergeräusche prüfen'] },
      { id: 'A-004', name: 'Kompressor 1', ort: 'Technikraum', typ: 'Druckluftkompressor', intervall: 180, letzte: tageZurueck(60),
        checkliste: ['Ölstand prüfen', 'Luftfilter wechseln', 'Kondensat ablassen', 'Sicherheitsventil testen'] },
      { id: 'A-005', name: 'Förderband 1', ort: 'Halle 2', typ: 'Förderband', intervall: 30, letzte: tageZurueck(31),
        checkliste: ['Antriebskette schmieren', 'Bandspannung prüfen', 'Not-Aus testen'] },
    ],
    historie: [
      { anlage: 'A-003', datum: tageZurueck(30), art: 'Wartung', text: 'Wartung nach Checkliste, alles i. O.', wer: 'T. Hauser' },
      { anlage: 'A-001', datum: tageZurueck(20), art: 'Störung', text: 'Motor sehr laut, Lager getauscht', wer: 'T. Hauser' },
    ],
    stoerungen: [
      { id: 1, anlage: 'A-002', datum: tageZurueck(2), text: 'Fehlermeldung Überstrom, Antrieb schaltet ab', prio: 'hoch', offen: true, von: 'Demo' },
    ],
  };
}
function demoLaden() {
  try { const d = JSON.parse(lsGet(DEMO_KEY)); if (d && d.anlagen) return d; } catch (e) {}
  return demoDaten();
}
const demoSpeichern = () => lsSet(DEMO_KEY, JSON.stringify(daten));

// ---------- Datenzugriff (gleiche Funktionen für Demo und Server) ----------
const store = {
  async laden() {
    daten = modus === 'server' ? await api('GET', '/daten') : demoLaden();
  },
  async neueAnlage(a) {
    if (modus === 'server') return (await api('POST', '/anlagen', a)).id;
    const max = daten.anlagen.reduce((m, x) => Math.max(m, parseInt(x.id.slice(2), 10) || 0), 0);
    const id = 'A-' + String(max + 1).padStart(3, '0');
    daten.anlagen.push({ id, ...a }); demoSpeichern(); return id;
  },
  async loescheAnlage(id) {
    if (modus === 'server') return api('DELETE', '/anlagen/' + id);
    daten.anlagen = daten.anlagen.filter((x) => x.id !== id);
    daten.historie = daten.historie.filter((h) => h.anlage !== id);
    daten.stoerungen = daten.stoerungen.filter((s) => s.anlage !== id);
    demoSpeichern();
  },
  async wartung(id, text, wer) {
    if (modus === 'server') return api('POST', `/anlagen/${id}/wartung`, { text });
    const a = daten.anlagen.find((x) => x.id === id); a.letzte = heuteISO();
    daten.historie.push({ anlage: id, datum: heuteISO(), art: 'Wartung', text: text || 'Wartung nach Checkliste durchgeführt', wer: wer || 'Demo' });
    demoSpeichern();
  },
  async stoerung(anlage, text, prio) {
    if (modus === 'server') return api('POST', '/stoerungen', { anlage, text, prio });
    daten.stoerungen.push({ id: Date.now(), anlage, datum: heuteISO(), text, prio, offen: true, von: 'Demo' }); demoSpeichern();
  },
  async eingriffStart(id, grund, werkzeug) {
    if (modus === 'server') return api('POST', `/anlagen/${id}/eingriff`, { grund, werkzeug });
    const a = daten.anlagen.find((x) => x.id === id);
    const jetzt = new Date();
    a.eingriff = { start: jetzt.toISOString().slice(0, 10) + ' ' + jetzt.toTimeString().slice(0, 5), grund, werkzeug, von: 'Demo' };
    demoSpeichern();
  },
  async eingriffAbschluss(id, body) {
    if (modus === 'server') return api('POST', `/anlagen/${id}/eingriff/abschluss`, body);
    const a = daten.anlagen.find((x) => x.id === id), e = a.eingriff;
    if (body.fehlend.length) {
      daten.stoerungen.push({ id: Date.now(), anlage: id, datum: heuteISO(), prio: 'hoch', offen: true, von: 'Demo',
        text: 'Nach Instandhaltung fehlt: ' + body.fehlend.join(', ') + '. Anlage NICHT freigeben, Suche einleiten (Fremdkörpergefahr).' });
      demoSpeichern(); return { freigegeben: false };
    }
    const ende = new Date().toTimeString().slice(0, 5);
    daten.historie.push({ anlage: id, datum: heuteISO(), art: 'Freigabe', wer: 'Demo',
      text: `${e.grund}. Eingriff ${e.start} bis ${ende} Uhr. Werkzeug vollständig zurück (${e.werkzeug.length} Positionen), keine losen Teile, Schutz montiert, gereinigt. Freigegeben durch ${body.freigabe_von}.` + (body.bemerkung ? ' Bemerkung: ' + body.bemerkung : '') });
    delete a.eingriff; demoSpeichern(); return { freigegeben: true };
  },
  async behoben(sid, massnahme) {
    if (modus === 'server') return api('POST', `/stoerungen/${sid}/behoben`, { massnahme });
    const s = daten.stoerungen.find((x) => x.id === sid); s.offen = false;
    daten.historie.push({ anlage: s.anlage, datum: heuteISO(), art: 'Störung', text: `${s.text}. Behoben: ${massnahme}`, wer: 'Demo' });
    demoSpeichern();
  },
};

// Aktion ausführen, Daten neu laden, Ansicht aktualisieren – mit Fehleranzeige
async function ausfuehren(fn, naechsterHash) {
  try {
    await fn();
    await store.laden();
    if (naechsterHash !== undefined && location.hash !== naechsterHash) { location.hash = naechsterHash; } else { route(); }
  } catch (e) { alarm(e.message); }
}
function alarm(text) {
  let el = $('toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = text; el.classList.add('zeigen');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('zeigen'), 4000);
}

// ---------- Berechnungen ----------
function naechsteWartung(a) { const d = new Date(a.letzte); d.setDate(d.getDate() + Number(a.intervall)); return d; }
function restTage(a) { const h = new Date(); h.setHours(0, 0, 0, 0); return Math.round((naechsteWartung(a) - h) / 86400000); }
function status(a) { const r = restTage(a); return r < 0 ? 'faellig' : r <= BALD_TAGE ? 'bald' : 'ok'; }
const STATUS_TEXT = { faellig: 'Überfällig', bald: 'Bald fällig', ok: 'In Ordnung' };
const offeneStoerungen = (id) => daten.stoerungen.filter((s) => s.offen && (!id || s.anlage === id));

// ---------- Ansichten ----------
function zeige(view) {
  document.querySelectorAll('main > section').forEach((s) => (s.hidden = s.id !== 'view-' + view));
  document.querySelectorAll('nav button[data-view]').forEach((b) => b.classList.toggle('aktiv', b.dataset.view === view));
}

function kopfAktualisieren() {
  const drin = !!modus;
  $('nav').hidden = !drin;
  $('navTeam').hidden = !(ich && ich.rolle === 'chef');
  $('demoHinweis').hidden = modus !== 'demo';
  $('abmelden').textContent = modus === 'demo' ? 'Demo beenden' : 'Abmelden';
  $('untertitel').textContent = modus === 'server' ? `${ich.firma} · angemeldet als ${ich.name}` : 'Anlagen · Wartungsintervalle · Störmeldungen · QR-Codes';
  $('fussText').innerHTML = modus === 'demo'
    ? 'Demo-Daten werden lokal im Browser gespeichert. <button id="reset" class="link">Demo zurücksetzen</button>'
    : 'Wartungsplaner · Quellcode auf <a href="https://github.com/Tobias-Hauser/wartungsplaner">GitHub</a>';
  const r = $('reset');
  if (r) r.onclick = () => { lsDel(DEMO_KEY); ausfuehren(async () => {}, ''); };
}

function renderUebersicht() {
  const z = { faellig: 0, bald: 0, ok: 0 };
  daten.anlagen.forEach((a) => z[status(a)]++);
  $('kpis').innerHTML = `
    <div class="kpi faellig"><b>${z.faellig}</b><span>Überfällig</span></div>
    <div class="kpi bald"><b>${z.bald}</b><span>Fällig in ≤ ${BALD_TAGE} Tagen</span></div>
    <div class="kpi ok"><b>${z.ok}</b><span>In Ordnung</span></div>
    <div class="kpi stoer"><b>${offeneStoerungen().length}</b><span>Offene Störungen</span></div>`;

  const q = $('suche').value.toLowerCase(), f = $('filterStatus').value;
  const liste = daten.anlagen
    .filter((a) => (a.name + ' ' + a.ort + ' ' + a.typ + ' ' + a.id).toLowerCase().includes(q))
    .filter((a) => !f || status(a) === f)
    .sort((a, b) => restTage(a) - restTage(b));

  $('liste').innerHTML = liste.map((a) => {
    const s = status(a), r = restTage(a), st = offeneStoerungen(a.id).length;
    const rText = r < 0 ? `seit ${-r} Tagen überfällig` : r === 0 ? 'heute fällig' : `fällig in ${r} Tagen`;
    return `<div class="karte anlage ${s}" data-id="${esc(a.id)}">
      <h3>${esc(a.name)} <span class="meta">· ${esc(a.id)}</span></h3>
      <span class="badge ${s}">${STATUS_TEXT[s]}</span>
      <div class="meta">${esc(a.ort)} · ${esc(a.typ)}</div>
      ${a.eingriff ? '<span class="badge eingriff">In Instandhaltung</span>' : st ? `<span class="badge stoer">${st} Störung${st > 1 ? 'en' : ''}</span>` : '<span></span>'}
      <div class="meta">Nächste Wartung: ${fmt(naechsteWartung(a))} (${rText})</div>
    </div>`;
  }).join('') || `<div class="karte"><p>Noch keine Anlagen angelegt.</p><button class="primaer" onclick="location.hash='neu'">Erste Anlage anlegen</button></div>`;

  document.querySelectorAll('.anlage').forEach((el) => el.addEventListener('click', () => { location.hash = 'anlage=' + el.dataset.id; }));
}

function renderDetail(id) {
  const a = daten.anlagen.find((x) => x.id === id);
  const el = $('view-detail');
  if (!a) { el.innerHTML = '<p>Anlage nicht gefunden.</p><button class="link" onclick="location.hash=\'\'">← Zur Übersicht</button>'; return; }
  const s = status(a);
  const hist = daten.historie.filter((h) => h.anlage === id).sort((x, y) => y.datum.localeCompare(x.datum));
  const stoer = offeneStoerungen(id);

  el.innerHTML = `
    <p class="nicht-drucken"><button class="link" id="zurueck">← Zur Übersicht</button></p>
    <div class="karte">
      <div class="detail-kopf">
        <div>
          <h2 style="margin:0">${esc(a.name)}</h2>
          <p class="meta">${esc(a.id)} · ${esc(a.ort)} · ${esc(a.typ)}</p>
          <p><span class="badge ${s}">${STATUS_TEXT[s]}</span></p>
          <p class="meta">Intervall: alle ${Number(a.intervall)} Tage<br>Letzte Wartung: ${fmt(a.letzte)}<br>Nächste Wartung: <b>${fmt(naechsteWartung(a))}</b></p>
        </div>
        <div><div class="qr" id="qr"></div><div class="qr-label">${esc(a.name)} · ${esc(a.id)}</div></div>
      </div>
      ${a.eingriff ? `<div class="eingriff-box">
        <b>🔧 In Instandhaltung</b> seit ${esc(a.eingriff.start)} Uhr (${esc(a.eingriff.von)})<br>
        Grund: ${esc(a.eingriff.grund)}<br>
        Erfasstes Werkzeug/Material: ${a.eingriff.werkzeug.map(esc).join(', ')}
        <div class="aktionen"><button class="primaer" id="btnAbschluss">Eingriff abschließen &amp; freigeben</button></div>
      </div>` : ''}
      <div class="aktionen">
        ${a.eingriff ? '' : '<button class="primaer" id="btnEingriff">Eingriff starten</button>'}
        <button class="${a.eingriff ? 'primaer' : 'sekundaer'}" id="btnWartung">Wartung erledigt</button>
        <button class="warn" id="btnStoerung">Störung melden</button>
        <button class="sekundaer" id="btnDruck">QR-Etikett drucken</button>
        ${darfLoeschen() ? '<button class="sekundaer" id="btnLoeschen">Anlage löschen</button>' : ''}
      </div>
      <div id="formBereich"></div>
    </div>
    ${stoer.length ? `<h3>Offene Störungen</h3>${stoer.map(stoerKarte).join('')}` : ''}
    <h3>Wartungsumfang</h3>
    <div class="karte"><ul>${(a.checkliste || []).map((c) => `<li>${esc(c)}</li>`).join('') || '<li class="meta">Keine Checkliste hinterlegt</li>'}</ul></div>
    <h3>Historie</h3>
    <div class="karte"><ul class="historie">${hist.map((h) => `<li><b>${fmt(h.datum)}</b> · ${esc(h.art)} · ${esc(h.text)} <span class="wer">(${esc(h.wer)})</span></li>`).join('') || '<li class="meta">Noch keine Einträge</li>'}</ul></div>`;

  // QR-Code zeigt auf genau diese Anlage
  const url = location.href.split('#')[0] + '#anlage=' + a.id;
  new QRCode($('qr'), { text: url, width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M });

  $('zurueck').onclick = () => { location.hash = ''; };
  $('btnDruck').onclick = () => window.print();
  $('btnWartung').onclick = () => formWartung(a);
  $('btnStoerung').onclick = () => formStoerung(a);
  if ($('btnLoeschen')) $('btnLoeschen').onclick = () => formLoeschen(a);
  if ($('btnEingriff')) $('btnEingriff').onclick = () => formEingriffStart(a);
  if ($('btnAbschluss')) $('btnAbschluss').onclick = () => formEingriffAbschluss(a);
  bindStoerButtons();
}

const WERKZEUG_VORSCHLAG = ['Schraubendreher-Set', 'Ratschenkasten', 'Maulschlüssel-Satz', 'Multimeter', 'Taschenlampe', 'Kabelbinder', 'Ersatzteil (Schrauben/Kleinteile)', 'Putztuch'];

function formEingriffStart(a) {
  $('formBereich').innerHTML = `
    <form id="fe" class="formular">
      <p class="meta">Vor dem Eingriff alles erfassen, was an die Anlage mitgenommen wird. Nach dem Eingriff muss jedes Teil wieder abgehakt werden, damit nichts in der Linie zurückbleibt.</p>
      <label>Grund des Eingriffs<input name="grund" required maxlength="200" placeholder="z. B. Lager am Antrieb tauschen"></label>
      <div class="chips">${WERKZEUG_VORSCHLAG.map((w) => `<button type="button" class="chip" data-w="${esc(w)}">+ ${esc(w)}</button>`).join('')}</div>
      <label>Werkzeug und Material (eine Zeile pro Teil)<textarea name="werkzeug" rows="5" required placeholder="Ratschenkasten&#10;Multimeter&#10;2 Schrauben M8"></textarea></label>
      <button class="primaer" type="submit">Eingriff starten und Anlage sperren</button>
    </form>`;
  const ta = document.querySelector('#fe textarea');
  document.querySelectorAll('.chip').forEach((c) => c.onclick = () => {
    const liste = ta.value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!liste.includes(c.dataset.w)) liste.push(c.dataset.w);
    ta.value = liste.join('\n'); c.classList.add('gewaehlt');
  });
  $('fe').onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const werkzeug = String(f.get('werkzeug')).split('\n').map((s) => s.trim()).filter(Boolean);
    ausfuehren(() => store.eingriffStart(a.id, f.get('grund'), werkzeug));
  };
}

function formEingriffAbschluss(a) {
  const e = a.eingriff;
  $('formBereich').innerHTML = `
    <form id="fa" class="formular">
      <h3 style="margin:0">1. Werkzeugkontrolle</h3>
      <p class="meta">Jedes Teil abhaken, das wieder vollständig zurück ist. Fehlt etwas, wird die Anlage nicht freigegeben.</p>
      <div class="check werkzeug-check">${e.werkzeug.map((w, i) => `<label><input type="checkbox" name="w" value="${i}"> ${esc(w)}</label>`).join('')}</div>
      <h3 style="margin:8px 0 0">2. Zustand der Anlage</h3>
      <div class="check">
        <label><input type="checkbox" name="keine_losen_teile"> Keine losen Teile, Schrauben oder Reste in der Anlage</label>
        <label><input type="checkbox" name="schutz_montiert"> Alle Schutzhauben und Abdeckungen wieder montiert</label>
        <label><input type="checkbox" name="gereinigt"> Arbeitsbereich und Anlage gereinigt</label>
      </div>
      <h3 style="margin:8px 0 0">3. Freigabe durch die Produktion</h3>
      <label>Name der Person, die freigibt<input name="freigabe_von" maxlength="80" placeholder="z. B. Schichtleitung"></label>
      <label>Bemerkung (optional)<input name="bemerkung" maxlength="300" placeholder="z. B. Lager getauscht, Probelauf ok"></label>
      <p class="fehler" id="abschlussHinweis" hidden></p>
      <button class="primaer" type="submit" id="btnFreigabe">Anlage freigeben</button>
    </form>`;
  const form = $('fa');
  const fehlend = () => e.werkzeug.filter((w, i) => !form.querySelector(`input[name=w][value="${i}"]`).checked);
  const aktualisieren = () => {
    const f = fehlend();
    const hinweis = $('abschlussHinweis');
    hinweis.hidden = !f.length;
    hinweis.textContent = f.length ? `Fehlt noch: ${f.join(', ')}. Ohne vollständiges Werkzeug keine Freigabe.` : '';
    $('btnFreigabe').textContent = f.length ? 'Fehlendes Teil melden (keine Freigabe)' : 'Anlage freigeben';
    $('btnFreigabe').className = f.length ? 'warn' : 'primaer';
  };
  form.addEventListener('change', aktualisieren);
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const f = fehlend();
    if (!f.length) {
      for (const k of ['keine_losen_teile', 'schutz_montiert', 'gereinigt']) {
        if (!form[k].checked) { alarm('Bitte alle Punkte unter „Zustand der Anlage“ bestätigen'); return; }
      }
      if (!form.freigabe_von.value.trim()) { alarm('Bitte den Namen der freigebenden Person eintragen'); return; }
    }
    const body = {
      fehlend: f,
      checks: { werkzeug_vollstaendig: !f.length, keine_losen_teile: form.keine_losen_teile.checked, schutz_montiert: form.schutz_montiert.checked, gereinigt: form.gereinigt.checked },
      freigabe_von: form.freigabe_von.value.trim(), bemerkung: form.bemerkung.value.trim(),
    };
    ausfuehren(async () => {
      const r = await store.eingriffAbschluss(a.id, body);
      setTimeout(() => alarm(r && r.freigegeben ? 'Anlage freigegeben, Protokoll in der Historie gespeichert' : 'Störung angelegt: Teil fehlt, Anlage bleibt gesperrt'), 50);
    });
  };
}

function stoerKarte(s) {
  const a = daten.anlagen.find((x) => x.id === s.anlage);
  return `<div class="karte" style="margin-bottom:8px">
    <b>${esc(a ? a.name : s.anlage)}</b> <span class="badge stoer">Prio ${esc(s.prio)}</span>
    <p>${esc(s.text)}</p>
    <p class="meta">Gemeldet am ${fmt(s.datum)}${s.von ? ' von ' + esc(s.von) : ''}</p>
    <button class="sekundaer" data-stoer="${Number(s.id)}">Als behoben markieren</button>
  </div>`;
}

function bindStoerButtons() {
  document.querySelectorAll('[data-stoer]').forEach((b) => b.onclick = () => {
    const sid = Number(b.dataset.stoer);
    const box = document.createElement('form');
    box.className = 'behoben';
    box.innerHTML = '<input name="m" required maxlength="300" placeholder="Welche Maßnahme wurde durchgeführt?"><button class="primaer" type="submit">Speichern</button>';
    b.replaceWith(box);
    box.querySelector('input').focus();
    box.onsubmit = (e) => { e.preventDefault(); ausfuehren(() => store.behoben(sid, box.m.value)); };
  });
}

function formWartung(a) {
  $('formBereich').innerHTML = `
    <form id="fw" class="formular">
      <div class="check">${(a.checkliste || []).map((c) => `<label><input type="checkbox" required> ${esc(c)}</label>`).join('')}</div>
      <label>Bemerkung<input name="text" maxlength="300" placeholder="z. B. Filter gewechselt, alles i. O."></label>
      ${modus === 'demo' ? '<label>Durchgeführt von<input name="wer" required maxlength="80" placeholder="Name"></label>' : ''}
      <button class="primaer" type="submit">Wartung speichern</button>
    </form>`;
  $('fw').onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    ausfuehren(() => store.wartung(a.id, f.get('text'), f.get('wer')));
  };
}

function formStoerung(a) {
  $('formBereich').innerHTML = `
    <form id="fs" class="formular">
      <label>Was ist passiert?<textarea name="text" rows="3" required maxlength="500" placeholder="z. B. Motor wird heiß, ungewöhnliches Geräusch"></textarea></label>
      <label>Priorität<select name="prio"><option>niedrig</option><option selected>mittel</option><option>hoch</option></select></label>
      <button class="warn" type="submit">Störung melden</button>
    </form>`;
  $('fs').onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    ausfuehren(() => store.stoerung(a.id, f.get('text'), f.get('prio')));
  };
}

function formLoeschen(a) {
  $('formBereich').innerHTML = `
    <div class="karte loeschen">
      <p><b>${esc(a.name)}</b> wirklich löschen? Wartungshistorie und Störungen dieser Anlage werden ebenfalls entfernt.</p>
      <div class="aktionen">
        <button class="warn" id="jaLoeschen">Ja, löschen</button>
        <button class="sekundaer" id="neinLoeschen">Abbrechen</button>
      </div>
    </div>`;
  $('neinLoeschen').onclick = () => { $('formBereich').innerHTML = ''; };
  $('jaLoeschen').onclick = () => ausfuehren(() => store.loescheAnlage(a.id), '');
}

function renderStoerungen() {
  $('stoerListe').innerHTML = offeneStoerungen().map(stoerKarte).join('') || '<p class="meta">Keine offenen Störungen. 👍</p>';
  bindStoerButtons();
}

async function renderTeam() {
  try {
    const t = await api('GET', '/team');
    $('teamCode').textContent = t.code;
    $('teamListe').innerHTML = t.team.map((p) => `<li><b>${esc(p.name)}</b> · ${esc(p.email)} <span class="wer">(${p.rolle === 'chef' ? 'Chef' : 'Mitarbeiter'})</span>
      ${p.rolle === 'mitarbeiter' ? `<button class="link" data-entfernen="${Number(p.id)}" style="float:right">entfernen</button>` : ''}</li>`).join('');
    document.querySelectorAll('[data-entfernen]').forEach((b) => b.onclick = async () => {
      b.outerHTML = `<span style="float:right"><button class="link" data-ja="${b.dataset.entfernen}">wirklich entfernen?</button></span>`;
      document.querySelector(`[data-ja="${b.dataset.entfernen}"]`).onclick = async () => {
        try { await api('DELETE', '/team/' + b.dataset.entfernen); renderTeam(); } catch (e) { alarm(e.message); }
      };
    });
  } catch (e) { alarm(e.message); }
}

// ---------- Neue Anlage ----------
$('formNeu').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const a = {
    name: f.get('name'), ort: f.get('ort'), typ: f.get('typ'), intervall: Number(f.get('intervall')), letzte: f.get('letzte'),
    checkliste: String(f.get('checkliste')).split('\n').map((s) => s.trim()).filter(Boolean),
  };
  ausfuehren(async () => {
    const id = await store.neueAnlage(a);
    e.target.reset(); e.target.letzte.value = heuteISO();
    a._neu = id;
  }).then(() => { if (a._neu) location.hash = 'anlage=' + a._neu; });
});

// ---------- Anmelden / Registrieren / Beitreten ----------
document.querySelectorAll('.tabs button').forEach((b) => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('aktiv', x === b));
  document.querySelectorAll('[data-form]').forEach((f) => (f.hidden = f.dataset.form !== b.dataset.tab));
  $('authFehler').hidden = true;
});
document.querySelectorAll('[data-form]').forEach((form) => form.onsubmit = async (e) => {
  e.preventDefault();
  const knopf = form.querySelector('button[type=submit]');
  knopf.disabled = true; $('authFehler').hidden = true;
  try {
    const body = Object.fromEntries(new FormData(form));
    const pfad = { login: '/login', registrieren: '/registrieren', beitreten: '/beitreten' }[form.dataset.form];
    const r = await api('POST', pfad, body);
    lsSet(TOKEN_KEY, r.token); lsDel(DEMO_AKTIV);
    form.reset();
    await start();
  } catch (err) {
    $('authFehler').textContent = err.message === 'Failed to fetch' ? 'Server nicht erreichbar. Bitte später erneut versuchen.' : err.message;
    $('authFehler').hidden = false;
  } finally { knopf.disabled = false; }
});

$('demoStart').onclick = () => { lsSet(DEMO_AKTIV, '1'); start(); };
$('demoEnde').onclick = () => { lsDel(DEMO_AKTIV); modus = null; kopfAktualisieren(); route(); };
$('abmelden').onclick = async () => {
  if (modus === 'demo') { $('demoEnde').onclick(); return; }
  try { await api('POST', '/logout'); } catch (e) {}
  abmeldenLokal();
};
function abmeldenLokal() { lsDel(TOKEN_KEY); modus = null; ich = null; kopfAktualisieren(); route(); }

$('codeKopieren').onclick = () => { navigator.clipboard?.writeText($('teamCode').textContent); alarm('Code kopiert'); };
$('codeNeu').onclick = async () => {
  try { const r = await api('POST', '/team/neuer-code'); $('teamCode').textContent = r.code; alarm('Neuer Code erzeugt. Der alte gilt nicht mehr.'); } catch (e) { alarm(e.message); }
};

// ---------- Routing ----------
function route() {
  if (!modus) { zeige('start'); return; }
  const h = location.hash.slice(1);
  if (h.startsWith('anlage=')) { zeige('detail'); renderDetail(decodeURIComponent(h.slice(7))); }
  else if (h === 'stoerungen') { zeige('stoerungen'); renderStoerungen(); }
  else if (h === 'neu') { zeige('neu'); }
  else if (h === 'team' && ich && ich.rolle === 'chef') { zeige('team'); renderTeam(); }
  else { zeige('uebersicht'); renderUebersicht(); }
  window.scrollTo(0, 0);
}
document.querySelectorAll('nav button[data-view]').forEach((b) => b.addEventListener('click', () => {
  location.hash = b.dataset.view === 'uebersicht' ? '' : b.dataset.view;
}));
$('suche').addEventListener('input', renderUebersicht);
$('filterStatus').addEventListener('change', renderUebersicht);
document.querySelector('input[name=letzte]').value = heuteISO();
window.addEventListener('hashchange', route);

// ---------- Start ----------
async function start() {
  modus = null; ich = null;
  if (lsGet(TOKEN_KEY)) {
    try { ich = await api('GET', '/ich'); modus = 'server'; } catch (e) { /* Token ungültig */ }
  }
  if (!modus && lsGet(DEMO_AKTIV)) modus = 'demo';
  if (modus) { try { await store.laden(); } catch (e) { alarm(e.message); } }
  kopfAktualisieren();
  route();
}
start();
