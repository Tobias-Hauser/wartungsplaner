// Wartungsplaner – Anlagen, Wartungsintervalle, Störmeldungen und QR-Codes
// Daten liegen im localStorage des Browsers (keine Server nötig).

const SPEICHER = 'wartungsplaner-v1';
const BALD_TAGE = 14; // ab wie vielen Tagen vor Fälligkeit "bald fällig"

// ---------- Demo-Daten ----------
function tageZurueck(n) {
  const d = new Date(); d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}
function demoDaten() {
  return {
    anlagen: [
      { id: 'A-001', name: 'Förderband 3', ort: 'Halle 2, Linie A', typ: 'Bandförderer', intervall: 90, letzte: tageZurueck(97),
        checkliste: ['Antriebskette schmieren', 'Gurtspannung und Gurtlauf prüfen', 'Rollen auf Verschleiß prüfen', 'Not-Aus-Funktion testen'] },
      { id: 'A-002', name: 'Druckluftkompressor K1', ort: 'Technikraum', typ: 'Schraubenkompressor', intervall: 180, letzte: tageZurueck(170),
        checkliste: ['Ölstand prüfen', 'Ansaugfilter wechseln', 'Kondensatableiter prüfen', 'Sicherheitsventil testen'] },
      { id: 'A-003', name: 'Hebebühne HB-2', ort: 'Werkstatt', typ: 'Säulenhebebühne', intervall: 365, letzte: tageZurueck(40),
        checkliste: ['Tragmuttern auf Verschleiß prüfen', 'Hydraulik auf Dichtheit prüfen', 'Endschalter testen', 'Sachkundigenprüfung dokumentieren'] },
      { id: 'A-004', name: 'Lastenaufzug LA-1', ort: 'Lager, Achse 4', typ: 'Seilaufzug', intervall: 30, letzte: tageZurueck(31),
        checkliste: ['Türkontakte prüfen', 'Notruf testen', 'Bremse und Halt prüfen', 'Schacht und Fahrkorb Sichtprüfung'] },
      { id: 'A-005', name: 'Rolltor RT-5', ort: 'Laderampe 2', typ: 'Schnelllauftor', intervall: 180, letzte: tageZurueck(60),
        checkliste: ['Lichtschranke prüfen', 'Schließkantensicherung testen', 'Federn und Seile Sichtprüfung'] },
    ],
    historie: [
      { anlage: 'A-003', datum: tageZurueck(40), art: 'Wartung', text: 'Jahreswartung durchgeführt, Tragmuttern i. O.', wer: 'T. Hauser' },
      { anlage: 'A-001', datum: tageZurueck(20), art: 'Störung', text: 'Band läuft seitlich ab – Gurt nachgestellt', wer: 'T. Hauser', erledigt: true },
    ],
    stoerungen: [
      { id: 1, anlage: 'A-004', datum: tageZurueck(2), text: 'Tür schließt nur nach mehreren Versuchen', prio: 'hoch', offen: true },
    ],
  };
}

// ---------- Speicher ----------
let daten = laden();
function laden() {
  try { const d = JSON.parse(localStorage.getItem(SPEICHER)); if (d && d.anlagen) return d; } catch (e) {}
  return demoDaten();
}
function speichern() { try { localStorage.setItem(SPEICHER, JSON.stringify(daten)); } catch (e) {} }

// ---------- Berechnungen ----------
function naechsteWartung(a) {
  const d = new Date(a.letzte); d.setDate(d.getDate() + Number(a.intervall)); return d;
}
function restTage(a) {
  const heute = new Date(); heute.setHours(0, 0, 0, 0);
  return Math.round((naechsteWartung(a) - heute) / 86400000);
}
function status(a) {
  const r = restTage(a);
  if (r < 0) return 'faellig';
  if (r <= BALD_TAGE) return 'bald';
  return 'ok';
}
const STATUS_TEXT = { faellig: 'Überfällig', bald: 'Bald fällig', ok: 'In Ordnung' };
const fmt = (d) => new Date(d).toLocaleDateString('de-DE');
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const offeneStoerungen = (id) => daten.stoerungen.filter((s) => s.offen && (!id || s.anlage === id));

// ---------- Ansichten ----------
function zeige(view) {
  document.querySelectorAll('main > section').forEach((s) => (s.hidden = s.id !== 'view-' + view));
  document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('aktiv', b.dataset.view === view));
}

function renderUebersicht() {
  const zaehl = { faellig: 0, bald: 0, ok: 0 };
  daten.anlagen.forEach((a) => zaehl[status(a)]++);
  document.getElementById('kpis').innerHTML = `
    <div class="kpi faellig"><b>${zaehl.faellig}</b><span>Überfällig</span></div>
    <div class="kpi bald"><b>${zaehl.bald}</b><span>Fällig in ≤ ${BALD_TAGE} Tagen</span></div>
    <div class="kpi ok"><b>${zaehl.ok}</b><span>In Ordnung</span></div>
    <div class="kpi stoer"><b>${offeneStoerungen().length}</b><span>Offene Störungen</span></div>`;

  const q = document.getElementById('suche').value.toLowerCase();
  const f = document.getElementById('filterStatus').value;
  const liste = daten.anlagen
    .filter((a) => (a.name + ' ' + a.ort + ' ' + a.typ + ' ' + a.id).toLowerCase().includes(q))
    .filter((a) => !f || status(a) === f)
    .sort((a, b) => restTage(a) - restTage(b));

  document.getElementById('liste').innerHTML = liste.map((a) => {
    const s = status(a), r = restTage(a), st = offeneStoerungen(a.id).length;
    const rText = r < 0 ? `seit ${-r} Tagen überfällig` : r === 0 ? 'heute fällig' : `fällig in ${r} Tagen`;
    return `<div class="karte anlage ${s}" data-id="${a.id}">
      <h3>${esc(a.name)} <span class="meta">· ${a.id}</span></h3>
      <span class="badge ${s}">${STATUS_TEXT[s]}</span>
      <div class="meta">${esc(a.ort)} · ${esc(a.typ || '')}</div>
      ${st ? `<span class="badge stoer">${st} Störung${st > 1 ? 'en' : ''}</span>` : '<span></span>'}
      <div class="meta">Nächste Wartung: ${fmt(naechsteWartung(a))} (${rText})</div>
    </div>`;
  }).join('') || '<p class="meta">Keine Anlagen gefunden.</p>';

  document.querySelectorAll('.anlage').forEach((el) => el.addEventListener('click', () => { location.hash = 'anlage=' + el.dataset.id; }));
}

function renderDetail(id) {
  const a = daten.anlagen.find((x) => x.id === id);
  const el = document.getElementById('view-detail');
  if (!a) { el.innerHTML = '<p>Anlage nicht gefunden.</p>'; return; }
  const s = status(a);
  const hist = daten.historie.filter((h) => h.anlage === id).sort((x, y) => y.datum.localeCompare(x.datum));
  const stoer = offeneStoerungen(id);

  el.innerHTML = `
    <p class="nicht-drucken"><button class="link" id="zurueck">← Zur Übersicht</button></p>
    <div class="karte">
      <div class="detail-kopf">
        <div>
          <h2 style="margin:0">${esc(a.name)}</h2>
          <p class="meta">${a.id} · ${esc(a.ort)} · ${esc(a.typ || '')}</p>
          <p><span class="badge ${s}">${STATUS_TEXT[s]}</span></p>
          <p class="meta">Intervall: alle ${a.intervall} Tage<br>Letzte Wartung: ${fmt(a.letzte)}<br>Nächste Wartung: <b>${fmt(naechsteWartung(a))}</b></p>
        </div>
        <div><div class="qr" id="qr"></div><div class="qr-label">${esc(a.name)} · ${a.id}</div></div>
      </div>
      <div class="aktionen">
        <button class="primaer" id="btnWartung">Wartung erledigt</button>
        <button class="warn" id="btnStoerung">Störung melden</button>
        <button class="sekundaer" id="btnDruck">QR-Etikett drucken</button>
      </div>
      <div id="formBereich"></div>
    </div>
    ${stoer.length ? `<h3>Offene Störungen</h3>${stoer.map(stoerKarte).join('')}` : ''}
    <h3>Wartungsumfang</h3>
    <div class="karte"><ul>${(a.checkliste || []).map((c) => `<li>${esc(c)}</li>`).join('') || '<li class="meta">Keine Checkliste hinterlegt</li>'}</ul></div>
    <h3>Historie</h3>
    <div class="karte"><ul class="historie">${hist.map((h) => `<li><b>${fmt(h.datum)}</b> · ${h.art} · ${esc(h.text)} <span class="meta">(${esc(h.wer)})</span></li>`).join('') || '<li class="meta">Noch keine Einträge</li>'}</ul></div>`;

  // QR-Code zeigt auf genau diese Anlage
  const url = location.href.split('#')[0] + '#anlage=' + a.id;
  new QRCode(document.getElementById('qr'), { text: url, width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M });

  document.getElementById('zurueck').onclick = () => { location.hash = ''; };
  document.getElementById('btnDruck').onclick = () => window.print();
  document.getElementById('btnWartung').onclick = () => formWartung(a);
  document.getElementById('btnStoerung').onclick = () => formStoerung(a);
  bindStoerButtons();
}

function stoerKarte(s) {
  const a = daten.anlagen.find((x) => x.id === s.anlage);
  return `<div class="karte" style="margin-bottom:8px">
    <b>${esc(a ? a.name : s.anlage)}</b> <span class="badge stoer">Prio ${esc(s.prio)}</span>
    <p>${esc(s.text)}</p>
    <p class="meta">Gemeldet am ${fmt(s.datum)}</p>
    <button class="sekundaer" data-stoer="${s.id}">Als behoben markieren</button>
  </div>`;
}

function bindStoerButtons() {
  document.querySelectorAll('[data-stoer]').forEach((b) => b.onclick = () => {
    const s = daten.stoerungen.find((x) => x.id === Number(b.dataset.stoer));
    const box = document.createElement('form');
    box.className = 'behoben';
    box.innerHTML = '<input name="m" required placeholder="Welche Maßnahme wurde durchgeführt?"><button class="primaer" type="submit">Speichern</button>';
    b.replaceWith(box);
    box.querySelector('input').focus();
    box.onsubmit = (e) => {
      e.preventDefault();
      s.offen = false;
      daten.historie.push({ anlage: s.anlage, datum: new Date().toISOString().slice(0, 10), art: 'Störung', text: `${s.text} – Behoben: ${box.m.value}`, wer: 'Techniker' });
      speichern(); route();
    };
  });
}

function formWartung(a) {
  document.getElementById('formBereich').innerHTML = `
    <form id="fw" class="formular">
      <div class="check">${(a.checkliste || []).map((c, i) => `<label><input type="checkbox" required> ${esc(c)}</label>`).join('')}</div>
      <label>Bemerkung<input name="text" placeholder="z. B. Filter gewechselt, alles i. O."></label>
      <label>Durchgeführt von<input name="wer" required placeholder="Name"></label>
      <button class="primaer" type="submit">Wartung speichern</button>
    </form>`;
  document.getElementById('fw').onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target), heute = new Date().toISOString().slice(0, 10);
    a.letzte = heute;
    daten.historie.push({ anlage: a.id, datum: heute, art: 'Wartung', text: f.get('text') || 'Wartung nach Checkliste durchgeführt', wer: f.get('wer') });
    speichern(); route();
  };
}

function formStoerung(a) {
  document.getElementById('formBereich').innerHTML = `
    <form id="fs" class="formular">
      <label>Was ist passiert?<textarea name="text" rows="3" required placeholder="z. B. Motor wird heiß, ungewöhnliches Geräusch"></textarea></label>
      <label>Priorität<select name="prio"><option>niedrig</option><option selected>mittel</option><option>hoch</option></select></label>
      <button class="warn" type="submit">Störung melden</button>
    </form>`;
  document.getElementById('fs').onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    daten.stoerungen.push({ id: Date.now(), anlage: a.id, datum: new Date().toISOString().slice(0, 10), text: f.get('text'), prio: f.get('prio'), offen: true });
    speichern(); route();
  };
}

function renderStoerungen() {
  const s = offeneStoerungen();
  document.getElementById('stoerListe').innerHTML = s.map(stoerKarte).join('') || '<p class="meta">Keine offenen Störungen. 👍</p>';
  bindStoerButtons();
}

// ---------- Neue Anlage ----------
document.getElementById('formNeu').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const nr = String(daten.anlagen.length + 1).padStart(3, '0');
  const id = 'A-' + nr;
  daten.anlagen.push({
    id, name: f.get('name'), ort: f.get('ort'), typ: f.get('typ'), intervall: Number(f.get('intervall')), letzte: f.get('letzte'),
    checkliste: String(f.get('checkliste')).split('\n').map((s) => s.trim()).filter(Boolean),
  });
  speichern(); e.target.reset(); location.hash = 'anlage=' + id;
});

// ---------- Routing ----------
function route() {
  const h = location.hash.slice(1);
  if (h.startsWith('anlage=')) { zeige('detail'); renderDetail(decodeURIComponent(h.slice(7))); }
  else if (h === 'stoerungen') { zeige('stoerungen'); renderStoerungen(); }
  else if (h === 'neu') { zeige('neu'); }
  else { zeige('uebersicht'); renderUebersicht(); }
  window.scrollTo(0, 0);
}
document.querySelectorAll('nav button').forEach((b) => b.addEventListener('click', () => {
  location.hash = b.dataset.view === 'uebersicht' ? '' : b.dataset.view;
}));
document.getElementById('suche').addEventListener('input', renderUebersicht);
document.getElementById('filterStatus').addEventListener('change', renderUebersicht);
document.getElementById('reset').addEventListener('click', () => { daten = demoDaten(); speichern(); location.hash = ''; route(); });
document.querySelector('input[name=letzte]').value = new Date().toISOString().slice(0, 10);
window.addEventListener('hashchange', route);
route();
