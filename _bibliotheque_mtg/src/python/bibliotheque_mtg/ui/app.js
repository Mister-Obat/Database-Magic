"use strict";

const $ = (id) => document.getElementById(id);
const state = { view: "info", searchPage: 0, editionPage: 0, deckPage: 0, archivePage: 0, searchTotal: 0, searchItems: [], editionCardsTotal: 0, deckTotal: 0, deckExecuted: false, deckAppliedFilters: null, archiveTotal: 0, editions: [], activeSet: "", selectedDeck: null, openDeckId: null, deckDetailRequest: 0, deckListKey: null, deckListScroll: 0, deckListOpener: null, deckTaskId: null, deckPollTimer: null, deckFacetsLoaded: false, deckColorAttempted: false, deckColorPollTimer: null, deckRequest: 0, deckController: null, searchRequest: 0, searchController: null, editionRequest: 0, cardRequest: 0, cardController: null, cardGallery: null, maintenancePollTimer: null };
const viewScrollPositions = new Map();
const SEARCH_DRAFT_KEY = "database-magic-search-draft-v1";
const RECENT_SEARCHES_KEY = "database-magic-recent-searches-v1";
const RECENT_DECK_SEARCHES_KEY = "database-magic-recent-deck-searches-v1";
let initialRouteRestored = false;
let searchDraftTouched = false;
const PAGE_SIZE = 50;
const DECK_PAGE_SIZE = 200;
const imageTaskHandlers = new Map();
const notificationButton = document.getElementById("notification-button");
const REVIEW_SEEN_KEY = "database-magic-seen-reviews-v1";
let currentReviewTaskIds = new Set();
let seenReviewTaskIds = new Set();
let notificationAudioContext = null;
try {
  const saved = JSON.parse(localStorage.getItem(REVIEW_SEEN_KEY) || "[]");
  if (Array.isArray(saved)) seenReviewTaskIds = new Set(saved);
} catch { /* Storage may be unavailable; the badge still works for this page. */ }
function primeNotificationAudio() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return null;
  try {
    notificationAudioContext ||= new AudioContextClass();
    if (notificationAudioContext.state === "suspended") void notificationAudioContext.resume().catch(() => {});
    return notificationAudioContext;
  } catch { return null; }
}
document.addEventListener("pointerdown", primeNotificationAudio, { once: true });
document.addEventListener("keydown", primeNotificationAudio, { once: true });
function playNotificationSound() {
  const audio = primeNotificationAudio();
  if (!audio || audio.state !== "running") return;
  try {
    const start = audio.currentTime;
    const tone = audio.createOscillator();
    const volume = audio.createGain();
    tone.type = "sine";
    tone.frequency.setValueAtTime(880, start);
    tone.frequency.exponentialRampToValueAtTime(660, start + .22);
    volume.gain.setValueAtTime(.0001, start);
    volume.gain.exponentialRampToValueAtTime(.018, start + .025);
    volume.gain.exponentialRampToValueAtTime(.0001, start + .22);
    tone.connect(volume);
    volume.connect(audio.destination);
    tone.onended = () => { tone.disconnect(); volume.disconnect(); };
    tone.start(start);
    tone.stop(start + .23);
  } catch { /* Audio is optional when the browser blocks playback. */ }
}
function updateReviewBadge() {
  const visible = [...currentReviewTaskIds].some(id => !seenReviewTaskIds.has(id));
  const wasVisible = notificationButton.classList.contains("has-review");
  notificationButton.classList.toggle("has-review", visible);
  if (visible && !wasVisible) playNotificationSound();
}
const closeApplicationButton = document.getElementById("close-application");
const shutdownDialog = document.getElementById("shutdown-dialog");
closeApplicationButton.addEventListener("click", () => shutdownDialog.showModal());
document.getElementById("shutdown-cancel").addEventListener("click", () => shutdownDialog.close());
document.getElementById("shutdown-confirm").addEventListener("click", async () => {
  shutdownDialog.close();
  closeApplicationButton.disabled = true;
  closeApplicationButton.setAttribute("aria-label", "Fermeture…");
  try {
    await postJSON("/api/v2/app/shutdown", {});
    window.close();
    window.setTimeout(() => { window.location.replace("about:blank"); }, 100);
  } catch (error) {
    closeApplicationButton.disabled = false;
    closeApplicationButton.setAttribute("aria-label", "Fermer l’application");
    window.alert(`Fermeture impossible : ${error.message}`);
  }
});
notificationButton.addEventListener("click", () => {
  const panel = document.getElementById("notification-panel");
  panel.hidden = !panel.hidden;
  notificationButton.setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) {
    seenReviewTaskIds = new Set(currentReviewTaskIds);
    updateReviewBadge();
    try { localStorage.setItem(REVIEW_SEEN_KEY, JSON.stringify([...seenReviewTaskIds])); } catch { /* Keep the in-memory state. */ }
  }
});
document.addEventListener("pointerdown", event => {
  if (event.target.closest(".notification-center")) return;
  const panel = document.getElementById("notification-panel");
  panel.hidden = true;
  notificationButton.setAttribute("aria-expanded", "false");
});
let notificationRequest = 0;
async function refreshNotifications() {
  const request = ++notificationRequest;
  try {
    const response = await getJSON("/api/v2/notifications");
    if (request !== notificationRequest) return;
    const items = (response.items || []).filter(item => !["complete", "cancelled", "ready"].includes(item.status));
    const count = items.filter(item => ["queued", "running", "needs_review", "error", "failed"].includes(item.status)).length;
    const badge = document.getElementById("notification-count");
    badge.textContent = String(count);
    badge.hidden = count === 0;
    currentReviewTaskIds = new Set(items.filter(item => item.status === "needs_review").map(item => item.task_id));
    updateReviewBadge();
    const list = document.getElementById("notification-list"); list.replaceChildren();
    if (!items.length) { list.append(node("p", "muted", "Aucun traitement en cours ou à vérifier.")); return; }
    for (const item of items) {
      const row = node("div", "notification-item");
      const open = node("button", "notification-open", ""); open.type = "button";
      open.append(node("strong", "", item.label || (item.kind === "ai_generation" ? "Génération IA" : item.kind === "image_preview" ? "Recherche d’image" : item.kind || "Traitement")), node("span", "notification-status", notificationStatusLabel(item.status)), node("span", "notification-detail", `${item.completed != null && item.total ? `${item.completed}/${item.total} · ` : ""}${item.detail || ""}`));
      open.addEventListener("click", () => {
        const handler = imageTaskHandlers.get(item.task_id);
        if (handler) handler.open();
        else if (item.kind === "image_preview") {
          void openImagePreviewTask(item.task_id);
        } else if (item.kind === "ai_generation") {
          void openAIReviewTask(item.task_id, item);
        } else if (item.kind === "batch_download") {
          void openBatchDownloadTask(item);
        } else if (item.kind === "deck_download" && item.deck_id) {
          state.deckTaskId = item.task_id;
          setView("decks");
          void openDeck(item.deck_id);
        } else if (item.kind === "maintenance") {
          setView("info");
        }
      });
      row.append(open);
      if (item.kind === "batch_download" && ["queued", "running"].includes(item.status)) {
        const cancel = node("button", "secondary-button", "Annuler"); cancel.addEventListener("click", async event => { event.stopPropagation(); await postJSON("/api/v2/batch-download/cancel", {task_id:item.task_id}); void refreshNotifications(); }); row.append(cancel);
      } else if (item.kind === "deck_download" && ["queued", "running"].includes(item.status)) {
        const cancel = node("button", "secondary-button", "Annuler"); cancel.addEventListener("click", async event => { event.stopPropagation(); await postJSON("/api/v2/deck/cancel", {task_id:item.task_id}); void refreshNotifications(); }); row.append(cancel);
      } else if (item.kind === "maintenance" && ["queued", "running"].includes(item.status)) {
        const cancel = node("button", "secondary-button", "Annuler"); cancel.addEventListener("click", async event => { event.stopPropagation(); await postJSON("/api/v2/maintenance/update/cancel", {task_id:item.task_id}); void refreshNotifications(); }); row.append(cancel);
      } else if ((["ai_generation", "image_preview"].includes(item.kind) && ["queued", "running"].includes(item.status))
          || (item.kind === "ai_generation" && item.status === "error")) {
        const cancel = node("button", "secondary-button", "Annuler");
        cancel.type = "button";
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try { await cancelImageTask(item.task_id); }
          catch (error) {
            setText(open.querySelector(".notification-detail"), `Annulation impossible : ${error.message}`);
            cancel.disabled = false;
          }
        });
        row.append(cancel);
      }
      list.append(row);
    }
  } catch {
    if (request === notificationRequest) {
      const badge = document.getElementById("notification-count");
      badge.textContent = "!";
      badge.hidden = false;
    }
  }
}
async function cancelImageTask(taskId) {
  const task = await postJSON("/api/v2/image/task/cancel", { task_id: taskId });
  if (task.status === "cancelled") imageTaskHandlers.delete(taskId);
  void refreshNotifications();
  return task;
}
function notificationStatusLabel(status) { return ({ queued: "En attente", running: "En cours", cancelling: "Annulation…", needs_review: "À vérifier", ready: "Terminé", error: "Erreur", failed: "Échec", cancelled: "Annulé" })[status] || status || "En cours"; }
setInterval(() => { void refreshNotifications(); }, 2500);
void refreshNotifications();
const EDITION_PAGE_SIZE = 300;
const SEARCH_PAGE_SIZE = 200;
const PROXY_PAGE_SIZE = SEARCH_PAGE_SIZE;
const PROXY_SET_CODE = "proxy";
const proxyImageRevisions = new Map();
const ROUTE_VIEWS = new Set(["info", "recherche", "editions", "decks", "perso", "proxy", "archives"]);
let imageDemoMode = location.pathname === "/1" ? "existing" : location.pathname === "/2" ? "ai" : null;
let imageDemoCandidate = null;
let imageDemoCardId = null;
const COLOR_NAMES = { W: "blanc", U: "bleu", B: "noir", R: "rouge", G: "vert", C: "incolore" };
const PRODUCT_TYPES_FR = {
  "Advanced Deck": "Deck avancé", "Archenemy Deck": "Deck Archenemy", "Arena Promotional Deck": "Deck promotionnel Arena",
  "Arena Starter Deck": "Deck de démarrage Arena", "Arena Starter Kit": "Kit de démarrage Arena", "Brawl Deck": "Deck Brawl",
  "Challenge Deck": "Deck défi", "Challenger Deck": "Deck Challenger", "Clash Pack": "Pack Duel",
  "Commander Deck": "Deck Commander", "Dandan Deck": "Deck Dandân", "Demo Deck": "Deck de démonstration",
  "Duel Deck": "Deck Duel", "Duel Of The Planeswalkers Deck": "Deck Duels of the Planeswalkers", "Enemy Deck": "Deck adverse",
  "Enhanced Deck": "Deck amélioré", "Event Deck": "Deck événement", "Game Night Deck": "Deck Game Night",
  "Guild Kit": "Kit de guilde", "Halfdeck": "Demi-deck", "Historic Brawl Precon Deck": "Deck Brawl historique préconstruit",
  "Intro Pack": "Pack d'introduction", "Jumpstart": "Deck Jumpstart", "Modern Event Deck": "Deck événement Modern",
  "MTGO Commander Deck": "Deck Commander MTGO", "MTGO Duel Deck": "Deck Duel MTGO", "MTGO Theme Deck": "Deck à thème MTGO",
  "Pioneer Challenger Deck": "Deck Challenger Pioneer", "Planechase Deck": "Deck Planechase", "Planeswalker Deck": "Deck Planeswalker",
  "Premium Deck": "Deck Premium", "Pro Tour Deck": "Deck Pro Tour", "Sample Deck": "Deck d'initiation",
  "Shandalar Enemy Deck": "Deck adverse de Shandalar", "Spellslinger Starter Kit": "Kit de démarrage Spellslinger",
  "Starter Deck": "Deck de démarrage", "Starter Kit": "Kit de démarrage", "Theme Deck": "Deck à thème",
  "Welcome Deck": "Deck de bienvenue", "World Championship Deck": "Deck de championnat du monde",
};
function productTypeLabel(type) { return PRODUCT_TYPES_FR[type] || type || "Type non renseigné"; }
function deckNameLabel(deck) {
  const french = (deck.name_fr || "").trim(), english = (deck.name || "").trim();
  return french && french.toLocaleLowerCase("fr") !== english.toLocaleLowerCase("fr") ? `${french} / ${english}` : french || english || "Deck sans nom";
}
function deckTitleContent(eyebrow, name, colors, titleId) {
  const heading = node("div", "deck-heading");
  const title = node("h1", "", name);
  if (titleId) title.id = titleId;
  heading.append(title);
  const symbols = colorSymbols(colors);
  if (symbols) heading.append(symbols);
  return [node("p", "eyebrow", eyebrow), heading];
}
function deckSectionCount(cards) {
  return cards.reduce((total, card) => total + Number(card.count || 0), 0);
}
function deckSectionTitle(name, cards) {
  return node("h3", "", `${name} (${deckSectionCount(cards)})`);
}
function editionLabel(deck) {
  const name = deck.set_name_fr || deck.set_name || "";
  return [deck.set_code || "", name].filter(Boolean).join(" · ");
}
function selectedManaColors(container) {
  return Array.from(container.querySelectorAll('.mana-toggle[aria-pressed="true"]'), button => button.dataset.color).join("");
}
const MULTI_FILTER_IDS = ["filter-set", "filter-rarity", "filter-type", "deck-edition", "deck-format", "deck-type"];
function multiValue(id) { return $(id).dataset.multiValue ?? $(id).value; }
function setMultiValue(id, value) {
  const select = $(id);
  select.dataset.multiValue = id === "filter-set" && value && value !== "-" && !value.includes("|") ? groupedEditionCode(value) : value || "";
  renderMultiFilter(id);
  select.dispatchEvent(new Event("change", { bubbles: true }));
}
function renderMultiFilter(id) {
  const select = $(id), details = document.getElementById(`${id}-multi`);
  if (!details) return;
  const options = [...select.options].filter(option => option.value);
  const value = multiValue(id);
  const chosen = value === "" ? new Set(options.map(option => option.value)) : new Set(value === "-" ? [] : value.split("|"));
  const summary = details.querySelector("summary");
  summary.textContent = chosen.size === options.length ? "Toutes sélectionnées" : chosen.size === 0 ? "Aucune sélection" : `${chosen.size} sélection${chosen.size > 1 ? "s" : ""}`;
  summary.setAttribute("aria-label", `${select.dataset.filterLabel} : ${summary.textContent}`);
  const list = details.querySelector(".multi-filter-list");
  list.replaceChildren();
  for (const option of options) {
    const label = document.createElement("label"), checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.value = option.value; checkbox.checked = chosen.has(option.value);
    label.append(checkbox);
    appendEditionOptionLabel(label, option);
    list.append(label);
  }
}
function commitMultiFilter(id) {
  const boxes = [...document.querySelectorAll(`#${id}-multi .multi-filter-list input[type="checkbox"]`)];
  if (!boxes.length) return;
  const selected = boxes.filter(box => box.checked).map(box => box.value);
  const value = selected.length === boxes.length ? "" : selected.length ? selected.join("|") : "-";
  if (value !== multiValue(id)) setMultiValue(id, value);
}
function closeMultiFilter(details) {
  if (!details.open) return;
  commitMultiFilter(details.id.slice(0, -"-multi".length));
  details.open = false;
}
function setupMultiFilters() {
  for (const id of MULTI_FILTER_IDS) {
    const select = $(id), details = document.createElement("details");
    details.id = `${id}-multi`; details.className = `multi-filter${id.includes("edition") || id === "filter-set" ? " multi-filter-editions" : ""}${id === "filter-type" ? " multi-filter-types" : ""}`;
    const summary = document.createElement("summary"); summary.className = "custom-select-button";
    details.append(summary);
    const panel = document.createElement("div"), list = document.createElement("div"), actions = document.createElement("div");
    panel.className = "multi-filter-panel"; list.className = "multi-filter-list"; actions.className = "multi-filter-actions";
    list.setAttribute("role", "group");
    for (const [text, checked] of [["Tout cocher", true], ["Tout décocher", false]]) {
      const button = document.createElement("button"); button.type = "button"; button.className = "secondary-button"; button.textContent = text;
      button.addEventListener("click", () => list.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = checked; }));
      actions.append(button);
    }
    const apply = document.createElement("button"); apply.type = "button"; apply.className = "primary-button"; apply.textContent = "Valider";
    apply.addEventListener("click", () => {
      closeMultiFilter(details); summary.focus();
    });
    actions.append(apply); panel.append(list, actions); details.append(panel);
    details.addEventListener("toggle", () => {
      if (details.open) {
        document.querySelectorAll(".multi-filter[open]").forEach(other => { if (other !== details) closeMultiFilter(other); });
        renderMultiFilter(id);
      } else commitMultiFilter(id);
    });
    const label = select.parentElement, field = document.createElement("div");
    field.className = "multi-filter-field";
    label.before(field); field.append(label, details);
    const labelText = label.querySelector(".search-field-label")?.textContent?.trim() || label.textContent.trim();
    select.dataset.filterLabel = labelText;
    list.setAttribute("aria-label", labelText);
    select.dataset.multiFilter = "true";
    select.hidden = true;
    details.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); closeMultiFilter(details); summary.focus(); }
    });
    renderMultiFilter(id);
  }
  document.addEventListener("pointerdown", event => {
    document.querySelectorAll(".multi-filter[open]").forEach(details => {
      if (!details.contains(event.target)) closeMultiFilter(details);
    });
  });
}
function colorSymbols(colors) {
  if (!Array.isArray(colors)) return null;
  const values = colors.length ? colors : ["C"];
  const group = node("span", "deck-colors");
  group.setAttribute("role", "img");
  group.setAttribute("aria-label", values.length === 1 && values[0] === "C" ? "Deck incolore" : `Couleurs du deck : ${values.map(color => COLOR_NAMES[color]).join(", ")}`);
  for (const color of values) {
    const symbol = node("span", `mana-toggle mana-${color.toLowerCase()}`);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "mana-icon");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#mana-${color.toLowerCase()}`);
    svg.append(use);
    symbol.append(svg);
    symbol.setAttribute("aria-hidden", "true");
    group.append(symbol);
  }
  return group;
}
function setManaPressed(button, pressed) { button.setAttribute("aria-pressed", pressed ? "true" : "false"); }
function toggleMana(button, container) {
  const activate = button.getAttribute("aria-pressed") !== "true";
  setManaPressed(button, activate);
}

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}
function editionIcon(code, url) {
  if (code === PROXY_SET_CODE) {
    const icon = cardActionIcon("copy");
    icon.classList.add("edition-symbol");
    return icon;
  }
  if (!code) return null;
  const fallback = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS(svg.namespaceURI, "use");
    svg.classList.add("search-icon", "edition-symbol");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#search-icon-edition");
    svg.append(use);
    return svg;
  };
  if (!url) return fallback();
  const icon = node("img", "edition-symbol");
  icon.alt = "";
  icon.loading = "lazy";
  icon.addEventListener("error", () => icon.replaceWith(fallback()), { once: true });
  icon.src = url;
  return icon;
}
function appendEditionOptionLabel(container, option) {
  const name = option?.textContent || "Sélectionner…";
  if (!option?.dataset.editionCode) {
    container.append(name);
    return;
  }
  const row = node("span", "edition-option-label");
  const icon = editionIcon(option.dataset.editionCode, option.dataset.editionIcon);
  if (icon) row.append(icon);
  row.append(node("span", "edition-option-name", name));
  if (option.dataset.editionTotal !== undefined) {
    const downloaded = Number(option.dataset.editionDownloaded).toLocaleString("fr");
    const total = Number(option.dataset.editionTotal).toLocaleString("fr");
    row.append(node("span", "edition-option-total", `${downloaded} / ${total}`));
  }
  container.append(row);
}
// Lucide SVG paths (ISC/MIT); see LUCIDE_LICENSE.txt.
function cardActionIcon(name) {
  const shapes = {
    search: [["path", { d: "m21 21-4.34-4.34" }], ["circle", { cx: 11, cy: 11, r: 8 }]],
    minus: [["path", { d: "M5 12h14" }]],
    close: [["path", { d: "m18 6-12 12M6 6l12 12" }]],
    arrowUp: [["path", { d: "M12 19V5m-7 7 7-7 7 7" }]],
    arrowLeft: [["path", { d: "m12 19-7-7 7-7" }], ["path", { d: "M19 12H5" }]],
    arrowRight: [["path", { d: "M5 12h14" }], ["path", { d: "m12 5 7 7-7 7" }]],
    plus: [["path", { d: "M5 12h14" }], ["path", { d: "M12 5v14" }]],
    upload: [["path", { d: "M12 3v12" }], ["path", { d: "m17 8-5-5-5 5" }], ["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }]],
    download: [["path", { d: "M12 15V3" }], ["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }], ["path", { d: "m7 10 5 5 5-5" }]],
    addDeck: [["path", { d: "M12 10v6" }], ["path", { d: "M9 13h6" }], ["path", { d: "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" }]],
    usages: [["rect", { x: 3, y: 3, width: 7, height: 7, rx: 1 }], ["rect", { x: 14, y: 3, width: 7, height: 7, rx: 1 }], ["path", { d: "M6.5 14v7m-3.5-3.5h7M14 15h7m-7 4h7" }]],
    occurrences: [["rect", { x: 8, y: 8, width: 13, height: 13, rx: 2 }], ["path", { d: "M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" }]],
    flip: [["path", { d: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }], ["path", { d: "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" }], ["path", { d: "M8 16H3v5" }]],
    copy: [["rect", { x: 8, y: 8, width: 13, height: 13, rx: 2 }], ["path", { d: "M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" }]]
  };
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(key, value);
  svg.classList.add("card-action-icon");
  for (const [tag, attributes] of shapes[name]) {
    const shape = document.createElementNS(namespace, tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}
function setupCardSizeControls() {
  const scales = [0.7, 0.85, 1, 1.25, 1.5, 1.8, 2.1, 2.4, 2.7, 3, 3.4];
  const key = "database-magic-card-columns";
  const minus = $("card-size-minus"), plus = $("card-size-plus");
  const minimumIndex = scales.length - 9;
  const maximumIndex = scales.length - 3;
  let index = minimumIndex;
  try {
    const columns = Number(localStorage.getItem(key));
    if (Number.isInteger(columns) && columns >= 1 && columns <= scales.length) {
      index = scales.length - columns;
    } else {
      const previousIndex = scales.indexOf(Number(localStorage.getItem("database-magic-card-size")));
      if (previousIndex >= 0) index = previousIndex;
    }
  } catch {}
  index = Math.max(minimumIndex, Math.min(maximumIndex, index));
  function render() {
    document.documentElement.style.setProperty("--card-grid-columns", scales.length - index);
    document.documentElement.style.setProperty("--card-thumbnail-scale", scales[index]);
    minus.disabled = index === minimumIndex;
    plus.disabled = index === maximumIndex;
  }
  for (const [button, icon, direction] of [[minus, "minus", -1], [plus, "plus", 1]]) {
    button.append(cardActionIcon(icon));
    button.addEventListener("click", () => {
      index = Math.max(minimumIndex, Math.min(maximumIndex, index + direction));
      render();
      try { localStorage.setItem(key, String(scales.length - index)); } catch {}
    });
  }
  render();
}
setupCardSizeControls();
function setText(el, value) { el.textContent = value == null ? "" : String(value); }
function formatNumber(value) { return Number(value || 0).toLocaleString("fr-FR"); }
function params(values) { const p = new URLSearchParams(); for (const [k,v] of Object.entries(values)) if (v !== "" && v !== null && v !== undefined) p.set(k, String(v)); return p.toString(); }
function viewRoute(view = state.view) {
  const query = new URLSearchParams();
  const add = (key, value, fallback = "") => { if (value !== "" && value !== fallback && value != null) query.set(key, String(value)); };
  if (view === "recherche") {
    const applied = state.searchBaseFilters || {};
    const refined = state.refineFilters || {};
    if (state.searchExecuted) add("searched", 1);
    add("q", applied.q); add("set", applied.set);
    add("rarity", applied.rarity); add("card_type", applied.card_type);
    add("colors", applied.color); add("color_mode", applied.color_mode, "contains");
    add("oracle_id", applied.oracle_id); add("printing_id", applied.printing_id); add("unique", applied.unique);
    for (const [key, value] of Object.entries(refined)) add(key, value);
    add("page", state.searchPage + 1, 1);
  } else if (view === "editions") {
    add("set", $("edition-select").value); add("q", $("edition-query").value.trim());
    if (state.exactEditionCode && $("edition-select").value === state.exactEditionCode) add("exact", 1);
    add("page", state.editionPage + 1, 1);
  } else if (view === "decks") {
    const applied = state.deckAppliedFilters || {};
    if (state.deckExecuted) add("searched", 1);
    add("q", applied.q); add("format", applied.deck_format);
    add("set", applied.set_code); add("type", applied.deck_type);
    add("colors", applied.colors);
    add("color_mode", applied.color_mode, "contains");
    add("sort", applied.sort, "date"); add("order", applied.order, "desc");
    add("image_filter", applied.image_filter, "all");
    if (state.deckExecuted) add("page", state.deckPage + 1, 1);
    add("deck", state.openDeckId);
  } else if (view === "archives") {
    add("status", $("archive-status").value); add("page", state.archivePage + 1, 1);
  } else if (view === "perso") {
    add("deck", personalState.deck?.id);
  } else if (view === "proxy") {
    add("page", proxyState.page + 1, 1);
  }
  return `#${view}${query.size ? `?${query}` : ""}`;
}
function syncRoute(mode = "replace") {
  const hash = viewRoute();
  if (location.hash !== hash) history[mode === "push" ? "pushState" : "replaceState"](null, "", hash);
}
function restoreManaColors(container, colors) {
  const selected = new Set(String(colors || "").toUpperCase());
  container.querySelectorAll(".mana-toggle").forEach(button => setManaPressed(button, selected.has(button.dataset.color)));
}
function routePage(query) {
  const value = Number(query.get("page"));
  return Number.isSafeInteger(value) && value > 0 && value <= 100000 ? value - 1 : 0;
}
let routeRestoreRequest = 0;
async function restoreRoute() {
  const request = ++routeRestoreRequest;
  const routeView = location.hash.slice(1).split("?", 1)[0];
  if (["recherche", "editions"].includes(routeView)) await ensureEditions();
  if (request !== routeRestoreRequest) return;
  const pendingUserDraft = !initialRouteRestored && searchDraftTouched ? captureSearchDraft() : null;
  const pendingStoredDraft = readSearchDraft();
  const fragment = location.hash.slice(1);
  const separator = fragment.indexOf("?");
  const candidate = separator < 0 ? fragment : fragment.slice(0, separator);
  const query = new URLSearchParams(separator < 0 ? "" : fragment.slice(separator + 1));
  const legacyProxyRoute = candidate === "perso" && query.get("proxies") === "1";
  const view = legacyProxyRoute ? "proxy" : ROUTE_VIEWS.has(candidate) ? candidate : "info";
  if (view === "recherche") {
    state.searchExecuted = query.has("searched") || query.size > 0;
    $("query").value = query.get("q") || ""; setMultiValue("filter-set", query.get("set"));
    setMultiValue("filter-rarity", query.get("rarity"));
    restoreManaColors($("filter-color"), query.get("colors"));
    $("filter-color-mode").value = query.get("color_mode") || "contains";
    const savedCardType = (query.get("card_type") || query.get("type") || "").toLowerCase();
    setMultiValue("filter-type", savedCardType);
    $("search-unique").checked = query.get("unique") === "1";
    state.searchIdentity = { oracle_id: query.get("oracle_id") || "", printing_id: query.get("printing_id") || "" };
    for (const [key, id] of Object.entries(REFINE_IDS)) $(id).value = query.get(key) || (key === "image_filter" ? "all" : key === "result_order" ? "desc" : key === "result_sort" ? "date" : "");
    const legacySort = query.get("result_sort");
    if (legacySort && legacySort.includes("_")) {
      const [sort, order] = legacySort.split("_"); $("refine-sort").value = sort; $("refine-order").value = order;
    } else if (legacySort) $("refine-sort").value = legacySort;
    state.searchBaseFilters = mainSearchFilters();
    state.refineFilters = refineSearchFilters();
    state.searchPage = routePage(query);
    if (!query.has("auto") && pendingUserDraft) applySearchDraft(pendingUserDraft);
    else if (!query.has("auto") && pendingStoredDraft?.route === location.hash) applySearchDraft(pendingStoredDraft);
  } else if (view === "editions") {
    $("edition-query").value = query.get("q") || "";
    state.exactEditionCode = query.has("exact") ? query.get("set") : null;
    if (state.exactEditionCode) selectExactEditionCode(state.exactEditionCode);
    else $("edition-select").value = groupedEditionCode(query.get("set"));
    editionSuggestions.hide();
    state.editionPage = routePage(query);
    pendingEditionFocus = query.get("focus");
    if (pendingEditionFocus) editionPageKey = null;
  } else if (view === "decks") {
    await loadDeckFacets();
    if (request !== routeRestoreRequest) return;
    $("deck-query").value = query.get("q") || "";
    setMultiValue("deck-format", query.get("format")); setMultiValue("deck-edition", query.get("set"));
    setMultiValue("deck-type", query.get("type"));
    restoreManaColors(document.querySelector(".deck-color-filter"), query.get("colors"));
    $("deck-color-mode").value = query.get("color_mode") || "contains";
    $("deck-sort").value = query.get("sort") || "date"; $("deck-order").value = query.get("order") || "desc";
    $("deck-image-filter").value = query.get("image_filter") || "all";
    state.deckPage = routePage(query);
    state.deckExecuted = query.has("searched") || [...query.keys()].some(key => key !== "deck");
    state.deckAppliedFilters = state.deckExecuted ? deckResultFilters() : null;
  } else if (view === "archives") {
    $("archive-status").value = query.get("status") || ""; state.archivePage = routePage(query);
  } else if (view === "proxy") {
    proxyState.page = routePage(query);
  }
  initialRouteRestored = true;
  const restoreDeckScroll = view === "decks" && state.deckExecuted && !query.has("deck") && state.openDeckId
    && state.deckListKey === params(deckPageQuery());
  if (view === "decks") {
    const id = query.get("deck");
    if (!id) closeDeck(null);
    else if (id !== state.openDeckId) void openDeck(id, null);
  }
  setView(view, null);
  if (view === "recherche") {
    if (state.searchExecuted) {
      if (state.searchResultKey !== params(searchFilters())) {
        if (state.searchSnapshot && state.searchSnapshotFilters === searchFilterKey()) {
          await loadSearchPage(state.searchSnapshot);
        } else await runSearch(false);
      }
      if (request !== routeRestoreRequest || state.view !== view) return;
      restoreViewScroll(view);
    } else {
      state.searchController?.abort();
      state.searchController = null;
      ++state.searchRequest;
      state.searchResultKey = null;
      state.searchSnapshot = null;
      state.searchItems = [];
      state.searchTotal = 0;
      $("results").replaceChildren();
      updateSearchPager();
      updateBatchButtons();
    }
  }
  if (view === "perso" && query.get("deck") && personalState.deck?.id !== query.get("deck"))
    void openPersonalDeck(query.get("deck"));
  if (restoreDeckScroll) scrollToDeckList();
  if (view === "recherche" && !state.searchExecuted) {
    setText($("search-status"), "Cliquez sur Rechercher pour lancer la recherche.");
  }
  if (legacyProxyRoute || !ROUTE_VIEWS.has(candidate)) syncRoute();
}
async function getJSON(path, query = {}, options = {}) {
  const response = await fetch(path + (Object.keys(query).length ? `?${params(query)}` : ""), { headers: { Accept: "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}
async function postJSON(path, body, options = {}) {
  const response = await fetch(path, { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify(body), ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}
function activateView(view) {
  hoverPreview.hidden = true;
  state.view = view;
  document.querySelectorAll(".view").forEach((x) => x.classList.toggle("active", x.id === `view-${view}`));
  document.querySelectorAll(".nav-button").forEach((x) => x.classList.toggle("active", x.dataset.view === view));
}
function setView(view, urlMode = "push") {
  if (!ROUTE_VIEWS.has(view)) return;
  if (urlMode) routeRestoreRequest++;
  const previousView = state.view;
  if (view !== previousView) cancelPagePrefetch();
  const navigation = routeRestoreRequest;
  let clampedScroll = null;
  const restoreAfterLoad = () => {
    if (clampedScroll !== null && navigation === routeRestoreRequest
        && state.view === view && window.scrollY === clampedScroll) restoreViewScroll(view);
  };
  if (view !== previousView) viewScrollPositions.set(previousView, window.scrollY);
  activateView(view);
  if (urlMode) syncRoute(urlMode);
  if (view === "editions") {
    if (previousView !== view && $("edition-select").value === PROXY_SET_CODE) editionPageKey = null;
    void ensureEditions(previousView !== view).then(ensureEditionPage).then(restoreAfterLoad);
  }
  if (view === "recherche" && previousView !== view) void ensureEditions(true);
  if (view === "decks" && !state.openDeckId) {
    void loadDeckFacets().then(ensureDeckPage).then(restoreAfterLoad);
  }
  if (view === "perso" && !$("personal-content").hasChildNodes() && !new URLSearchParams(location.hash.split("?")[1] || "").has("deck")) void showPersonalDecks();
  if (view === "proxy") void renderProxies({ quiet: $("proxy-content").hasChildNodes() }).then(restoreAfterLoad);
  if (view === "archives") void Promise.resolve(ensureArchivePage()).then(restoreAfterLoad);
  if (view === "info") {
    void loadHome();
    void loadMaintenanceStatus();
  }
  if (view !== previousView) {
    restoreViewScroll(view);
    if (window.scrollY < (viewScrollPositions.get(view) ?? 0)) clampedScroll = window.scrollY;
    const path = { recherche: "/api/v2/search/page", editions: "/api/v2/edition", decks: "/api/v2/decks",
      archives: "/api/v2/archive" }[view];
    cataloguePageCaches.get(path)?.resume?.();
  }
}
function restoreViewScroll(view) {
  window.scrollTo({ top: viewScrollPositions.get(view) ?? 0, behavior: "instant" });
}
function scrollTopFooter(targetId) {
  const footer = node("div", "personal-deck-footer");
  const button = personalButton("", () => {
    const title = $(targetId);
    title?.setAttribute("tabindex", "-1");
    title?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  });
  button.classList.add("personal-scroll-top");
  button.title = "Remonter en haut";
  button.setAttribute("aria-label", button.title);
  button.append(cardActionIcon("arrowUp"));
  footer.append(button);
  return footer;
}
function attachScrollTop(pager, targetId, always = false) {
  const footer = scrollTopFooter(targetId);
  footer.classList.add("catalogue-page-footer");
  if (!always) footer.classList.add("hide-with-pager");
  pager.before(footer);
  footer.prepend(pager);
}
function pageLoading(label) {
  const waiting = node("p", "muted search-loading-state page-loading-state");
  waiting.setAttribute("role", "status");
  waiting.append(node("span", "card-loading-spinner search-loading-spinner"), node("span", "", label));
  return waiting;
}
const pageControls = new Map();
function makePager(pager, previous, label, next, changePage) {
  const input = node("input", "page-number");
  input.type = "text";
  input.inputMode = "numeric";
  input.setAttribute("aria-label", "Numéro de page");
  const range = node("span");
  label.replaceChildren("Page ", input, range);
  let page = 0, pages = 1;
  const go = target => {
    input.value = String(page + 1);
    if (!Number.isSafeInteger(target) || target < 0 || target >= pages || target === page) return;
    changePage(target);
  };
  input.addEventListener("keydown", event => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    const value = input.value.trim();
    go(/^\d+$/.test(value) ? Number(value) - 1 : NaN);
  });
  input.addEventListener("blur", () => { input.value = String(page + 1); });
  previous.onclick = () => go(page - 1);
  next.onclick = () => go(page + 1);
  return (index, count, details = "") => {
    page = index;
    pages = count;
    pager.hidden = pages <= 1;
    previous.disabled = page <= 0;
    next.disabled = page >= pages - 1;
    input.value = String(page + 1);
    input.size = String(pages).length + 1;
    setText(range, ` sur ${pages}${details ? ` · ${details}` : ""}`);
  };
}
function registerPager(id, previousId, labelId, nextId, gridId, changePage, duplicate = true) {
  const footer = $(id);
  const controls = [makePager(footer, $(previousId), $(labelId), $(nextId), changePage)];
  if (duplicate) {
    const top = node("div", "pager catalogue-top-pager");
    top.id = `${id}-top`;
    top.hidden = true;
    const previous = node("button", "secondary-button", $(previousId).textContent);
    const next = node("button", "secondary-button", $(nextId).textContent);
    previous.type = next.type = "button";
    const label = node("span", "muted");
    top.append(previous, label, next);
    $(gridId).before(top);
    controls.push(makePager(top, previous, label, next, changePage));
  }
  pageControls.set(id, (...values) => controls.forEach(update => update(...values)));
}
function jumpToCataloguePage(changePage) {
  return page => {
    window.scrollTo({ top: 0, behavior: "instant" });
    void changePage(page);
  };
}

const cataloguePageCaches = new Map();
function cancelPagePrefetch() {
  for (const cache of cataloguePageCaches.values()) {
    cache.generation++;
    cache.prefetch?.abort();
    cache.prefetch = null;
  }
}
function invalidatePageCache(path) {
  cancelPagePrefetch();
  if (path) {
    cataloguePageCaches.delete(path);
    if (path === "/api/v2/search") cataloguePageCaches.delete("proxy-association");
  } else cataloguePageCaches.clear();
}
function pageCache(path, query) {
  const scope = query.official_only ? "proxy-association" : path;
  const key = params({ ...query, offset: null, focus: null });
  if (cataloguePageCaches.get(scope)?.key !== key) {
    cataloguePageCaches.set(scope, { key, generation: 0, pages: new Map(), prefetch: null });
  }
  return cataloguePageCaches.get(scope);
}
async function fetchCataloguePage(path, query, options = {}) {
  const cache = pageCache(path, query);
  const offset = query.offset || 0;
  if (!query.focus && cache.pages.has(offset)) return cache.pages.get(offset);
  const data = await getJSON(path, query, options);
  if (!options.signal?.aborted) cache.pages.set(data.offset ?? offset, data);
  return data;
}
function prefetchNeighbourPages(path, query, total, grid, isCurrent) {
  const cache = pageCache(path, query);
  const generation = cache.generation;
  const current = () => isCurrent() && generation === cache.generation;
  cache.resume = () => {
    if (isCurrent()) prefetchNeighbourPages(path, query, total, grid, isCurrent);
  };
  const offsets = [query.offset + query.limit, query.offset - query.limit]
    .filter(offset => offset >= 0 && offset < total);
  for (const offset of cache.pages.keys()) {
    if (offset !== query.offset && !offsets.includes(offset)) cache.pages.delete(offset);
  }
  if (offsets.every(offset => cache.pages.has(offset))) return;
  requestAnimationFrame(() => requestAnimationFrame(async () => {
    if (!current()) return;
    const visibleImages = [...grid.querySelectorAll("img")].filter(image => {
      const box = image.getBoundingClientRect();
      return box.bottom > 0 && box.top < window.innerHeight;
    });
    await Promise.allSettled(visibleImages.map(image => image.decode()));
    const warm = async () => {
      if (!current()) return;
      const controller = new AbortController();
      cache.prefetch = controller;
      try {
        for (const offset of offsets) {
          if (!current()) break;
          await fetchCataloguePage(path, { ...query, offset }, { signal: controller.signal, priority: "low" });
        }
      } catch { /* Foreground navigation retries missing pages. */ }
      finally { if (cache.prefetch === controller) cache.prefetch = null; }
    };
    if (window.requestIdleCallback) window.requestIdleCallback(warm);
    else setTimeout(warm, 0);
  }));
}
function showError(container, message) { container.replaceChildren(node("p", "empty-state error-state", `Erreur : ${message}`)); }
function imageNode(slot, className = "card-preview") {
  if (!slot || slot.status !== "ready" || !slot.relative_path) return node("div", "missing-image", slot?.status === "pending_review" ? "À vérifier" : "Image absente");
  const img = node("img", className);
  img.alt = `Image ${slot.selected_language === "fr" ? "française" : "anglaise"} de la carte`;
  img.loading = "lazy";
  img.src = `/assets?${params({ path: slot.relative_path })}`;
  img.onerror = () => { const missing = node("div", "missing-image", "Image indisponible"); img.replaceWith(missing); };
  return img;
}
function currentCardImageUrl(card) {
  const revision = proxyImageRevisions.get(card?.provider_card_id);
  if (!card?.image_url || !revision) return card?.image_url;
  const url = new URL(card.image_url, location.href);
  url.searchParams.set("revision", revision);
  return url.href;
}
function cardTile(item) {
  const card = node("article", "card-card");
  card.dataset.providerId = item.provider_card_id || "";
  card.dataset.printingId = item.printing_id || "";
  card.dataset.lang = item.lang || "";
  const main = node("div");
  const title = node("h3", "", item.printed_name || item.name || "Nom indisponible");
  const languageLabel = ({ fr: "Texte FR", en: "Texte EN", ja: "Texte JA" })[item.lang] || `Texte ${String(item.lang || "?").toUpperCase()}`;
  const meta = node("p", "card-meta", `${item.set_code || "—"} · ${item.collector_number || "—"} · ${languageLabel}`);
  const type = node("p", "card-type", item.printed_type_line || item.type_line || "Type non renseigné");
  const cost = node("p", "card-meta", `Coût : ${item.mana_cost || "—"} · Valeur : ${item.mana_value ?? "—"} · Rareté : ${item.rarity || "—"}`);
  const opener = node("button", "open-card", "Ouvrir la fiche");
  opener.type = "button"; opener.addEventListener("click", () => openCard(item.provider_card_id));
  main.append(title, meta, type, cost, opener);
  const slot = (item.image_slots || []).find((x) => x.face_index === 0 && (x.selected_language === item.lang || x.status === "ready")) || (item.image_slots || [])[0];
  const preview = imageNode(slot);
  if (item.provider_card_id) {
    preview.tabIndex = 0; preview.setAttribute("role", "button"); preview.setAttribute("aria-label", `Ouvrir la fiche ${item.printed_name || item.name || "carte"}`);
    preview.addEventListener("click", () => openCard(item.provider_card_id));
    preview.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openCard(item.provider_card_id); } });
  }
  card.append(main, preview);
  return card;
}
function renderItems(container, items, emptyText = "Aucun résultat.") {
  container.replaceChildren();
  if (!items.length) { container.append(node("p", "empty-state", emptyText)); return; }
  for (const item of items) container.append(cardTile(item));
}
function editionTile(item) {
    const tile = node("button", "deck-card-tile deck-card-open edition-card-tile");
    tile.type = "button";
    tile.dataset.providerId = item.provider_card_id;
    tile.dataset.printingId = item.printing_id;
    tile.dataset.lang = item.lang;
    tile.setAttribute("aria-label", `Ouvrir la carte ${item.printed_name || item.name}`);
    tile.addEventListener("click", () => openCard(item.provider_card_id));
    const slot = (item.image_slots || []).find(face => face.face_index === 0 && face.status === "ready");
    const artUrl = currentCardImageUrl(item) || (slot?.relative_path ? `/assets?${params({ path: slot.relative_path })}` : "");
    const art = artUrl ? node("img", "deck-card-image") : node("div", "deck-card-image deck-card-image-empty", "Image absente");
    if (artUrl) {
      art.src = artUrl;
      art.alt = "";
      art.loading = "lazy";
      art.onerror = () => art.replaceWith(node("div", "deck-card-image deck-card-image-empty", "Image indisponible"));
    }
    tile.append(art, node("span", "deck-card-name", item.printed_name || item.name));
    return tile;
}
function renderEditionItems(container, items, emptyText = "Aucune carte dans cette édition.") {
  container.replaceChildren();
  if (!items.length) { container.append(node("p", "empty-state", emptyText)); return; }
  for (const item of items) container.append(editionTile(item));
}

const HOME_STATS_KEY = "database-magic-home-stats-v1";
const HOME_COUNTER_IDS = ["stat-cards", "stat-editions", "stat-decks"];
let homeStatsRequest, homeActivityRequest, homeActivitySnapshot, homeDeckCounter;
function renderHomeCounters(counters) {
  if (!Array.isArray(counters) || counters.length !== HOME_COUNTER_IDS.length ||
      !counters.every((pair, index) => Array.isArray(pair) && pair.length === 2 &&
        (Number.isFinite(pair[0]) || (index === 2 && pair[0] === null)) &&
        Number.isFinite(pair[1]))) return false;
  if (counters[2][0] !== null) homeDeckCounter = counters[2];
  counters.forEach(([present, total], index) => {
    const counter = $(HOME_COUNTER_IDS[index]);
    const text = `${present === null ? "—" : formatNumber(present)} / ${formatNumber(total)}`;
    if (counter.textContent !== text) setText(counter, text);
  });
  return true;
}
function restoreHomeCounters() {
  try { renderHomeCounters(JSON.parse(localStorage.getItem(HOME_STATS_KEY))); }
  catch { /* Storage may be unavailable. */ }
}
async function refreshHomeStats() {
  const grid = $("stats");
  grid.setAttribute("aria-busy", "true");
  try {
    const stats = await getJSON("/api/v2/stats", { background_decks: 1 });
    const counters = [
      [stats.complete_cards, stats.printings],
      [stats.complete_editions, stats.editions?.eligible],
      (stats.complete_decks === null && homeDeckCounter) || [stats.complete_decks, stats.decks_total],
    ];
    if (!renderHomeCounters(counters)) {
      throw new Error("Le serveur local est une ancienne version. Fermez-le puis relancez la Bibliothèque Magic.");
    }
    try { localStorage.setItem(HOME_STATS_KEY, JSON.stringify(counters)); }
    catch { /* Storage may be unavailable. */ }
    setText($("stats-error"), "");
  } catch (error) {
    setText($("stats-error"), error.message);
  } finally {
    grid.setAttribute("aria-busy", "false");
  }
}
async function refreshHomeActivity() {
  const errorBox = $("home-activity-error");
  const grid = $("home-recent-decks");
  if (homeActivitySnapshot === undefined) showDeckGridSkeleton(grid, 7);
  try {
    const { personal_decks: decks } = await getJSON("/api/v2/recent-activity");
    const snapshot = JSON.stringify(decks);
    if (snapshot !== homeActivitySnapshot) {
      const recentDecks = decks.map(deck =>
        deckCoverTile(deck.name, deck.composition_colors ?? deck.colors ?? [], deck.cover_card, () => {
          location.hash = `#perso?deck=${encodeURIComponent(deck.id)}`;
        }, null, deck.box_design));
      grid.replaceChildren(...(recentDecks.length ? recentDecks :
        [node("p", "muted", "Aucun deck personnel.")]));
      homeActivitySnapshot = snapshot;
    }
    errorBox.hidden = true;
  } catch (error) {
    if (homeActivitySnapshot === undefined) {
      grid.replaceChildren();
    }
    setText(errorBox, `Activité récente indisponible : ${error.message}`);
    errorBox.hidden = false;
  } finally {
    grid.removeAttribute("aria-busy");
  }
}
function loadHome() {
  homeStatsRequest ||= refreshHomeStats().finally(() => { homeStatsRequest = null; });
  homeActivityRequest ||= refreshHomeActivity().finally(() => { homeActivityRequest = null; });
  return Promise.all([homeStatsRequest, homeActivityRequest]);
}
function refreshVisibleHome() {
  if (state.view === "info" && !document.hidden) void loadHome();
}
function maintenanceElapsed(task) {
  const measured = Number(task.seconds);
  if (Number.isFinite(measured) && measured > 0) return Math.round(measured);
  const started = Date.parse(task.started_at || "");
  return Number.isFinite(started) ? Math.max(0, Math.round((Date.now() - started) / 1000)) : 0;
}
function renderMaintenanceSteps(steps) {
  const target = $("update-steps");
  target.replaceChildren();
  target.hidden = !Array.isArray(steps) || steps.length === 0;
  if (target.hidden) return;
  const list = node("ol", "maintenance-step-list");
  for (const step of steps) {
    const status = step.status || "pending";
    const percent = Math.max(0, Math.min(100, Number(step.progress) || 0));
    const label = status === "running" ? "En cours" : status === "complete" ? "Terminée"
      : status === "skipped" ? "Sans objet" : status === "failed" ? "Échec"
      : status === "cancelled" ? "Annulée" : "Restante";
    const item = node("li", `maintenance-step maintenance-step-${status}`);
    const heading = node("div", "maintenance-step-heading");
    heading.append(node("span", "", step.label || step.key || "Étape"),
      node("span", "", `${label} · ${Math.round(percent)} %`));
    const bar = node("progress");
    bar.max = 100;
    bar.value = percent;
    bar.setAttribute("aria-label", step.label || step.key || "Étape");
    item.append(heading, bar);
    if (step.detail && status !== "pending") item.append(node("p", "muted", step.detail));
    list.append(item);
  }
  target.append(list);
}
function renderMaintenanceTask(task) {
  const button = $("update-library"), status = $("update-status");
  renderMaintenanceSteps(task.steps);
  const running = ["queued", "running", "cancelling"].includes(task.status);
  button.disabled = task.status === "cancelling";
  $("update-library-label").textContent = task.status === "cancelling" ? "Annulation en cours…" : running ? "Annuler la mise à jour"
    : task.status === "complete" || task.status === "failed" || task.status === "cancelled" ? "Relancer la mise à jour" : "Lancer la mise à jour";
  if (task.status === "idle") {
    status.className = "muted maintenance-status";
    setText(status, "");
    return;
  }
  if (running) {
    const elapsed = maintenanceElapsed(task);
    status.className = "muted maintenance-status";
    setText(status, `${task.message || "Mise à jour en cours…"}${elapsed ? ` (${formatNumber(elapsed)} s)` : ""}`);
    return;
  }
  const details = [];
  if (task.message) details.push(task.message);
  for (const error of task.errors || []) {
    const label = error.step === "catalogue" ? "catalogue" : error.step === "decks" ? "decks" : "mise à jour";
    details.push(`Échec ${label} : ${error.error}`);
  }
  if (Number(task.seconds) > 0) details.push(`Durée : ${formatNumber(Math.round(task.seconds))} s.`);
  status.className = `maintenance-status ${task.status === "failed" ? "error-state" : "muted"}`;
  setText(status, details.join(" "));
}
async function loadMaintenanceStatus() {
  try {
    const task = await getJSON("/api/v2/maintenance/update");
    renderMaintenanceTask(task);
    if (["queued", "running", "cancelling"].includes(task.status)) pollMaintenanceTask();
  } catch (error) {
    $("update-status").className = "maintenance-status error-state";
    setText($("update-status"), `État de la mise à jour indisponible : ${error.message}`);
  }
}
async function startLibraryUpdate() {
  const button = $("update-library"), status = $("update-status");
  button.disabled = true;
  $("update-library-label").textContent = "Démarrage…";
  status.className = "muted maintenance-status";
  setText(status, "Démarrage de la mise à jour…");
  try {
    const task = await postJSON("/api/v2/maintenance/update", {});
    renderMaintenanceTask(task);
    pollMaintenanceTask();
  } catch (error) {
    button.disabled = false;
    $("update-library-label").textContent = "Lancer la mise à jour";
    status.className = "maintenance-status error-state";
    setText(status, `Mise à jour impossible : ${error.message}`);
  }
}
async function cancelLibraryUpdate(taskId) {
  const button = $("update-library");
  button.disabled = true;
  $("update-library-label").textContent = "Annulation en cours…";
  try {
    renderMaintenanceTask(await postJSON("/api/v2/maintenance/update/cancel", { task_id: taskId }));
    pollMaintenanceTask();
  } catch (error) {
    button.disabled = false;
    setText($("update-status"), `Annulation impossible : ${error.message}`);
  }
}
async function toggleLibraryUpdate() {
  try {
    const task = await getJSON("/api/v2/maintenance/update");
    if (["queued", "running"].includes(task.status)) {
      await cancelLibraryUpdate(task.task_id);
    } else if (task.status !== "cancelling") {
      await startLibraryUpdate();
    }
  } catch (error) {
    setText($("update-status"), `Action indisponible : ${error.message}`);
  }
}
function pollMaintenanceTask() {
  clearTimeout(state.maintenancePollTimer);
  state.maintenancePollTimer = setTimeout(async () => {
    try {
      const task = await getJSON("/api/v2/maintenance/update");
      renderMaintenanceTask(task);
      if (["queued", "running", "cancelling"].includes(task.status)) { pollMaintenanceTask(); return; }
      await refreshAfterMaintenanceUpdate();
    } catch (error) {
      setText($("update-status"), `Suivi indisponible : ${error.message}. Nouvelle tentative…`);
      state.maintenancePollTimer = setTimeout(pollMaintenanceTask, 5000);
    }
  }, 2000);
}
async function refreshAfterMaintenanceUpdate() {
  invalidatePageCache();
  state.deckFacetsLoaded = false;
  editionsPromise = null;
  await Promise.all([loadHome(), ensureEditions()]);
  if (state.view === "editions") await renderEditionPage();
  if (state.view === "decks") { await loadDeckFacets(true); await renderDeckPage(); }
  if (state.view === "recherche" && state.searchItems.length) await refreshSearchImages();
  if (!$('version-history-list').hidden) await loadVersionHistory();
}
function formatVersionSize(bytes) { return `${(Number(bytes || 0) / (1024 ** 3)).toLocaleString("fr-FR", {maximumFractionDigits: 1})} Go`; }
async function loadVersionHistory() {
  const target = $("version-history-list");
  target.replaceChildren(node("p", "muted", "Chargement des versions…"));
  try {
    const {versions} = await getJSON("/api/v2/maintenance/versions");
    target.replaceChildren();
    if (!versions.length) { target.append(node("p", "muted", "Aucune version conservée.")); return; }
    for (const version of versions) {
      const row = node("div", "version-history-row");
      const info = node("div");
      info.append(node("strong", "", version.version_id),
        node("span", "muted", version.active ? "Version active" : version.eligible ? "Version conservée" : "Version indisponible"));
      if (version.modified_at) info.append(node("span", "muted", `Modifiée le ${new Date(version.modified_at * 1000).toLocaleString("fr-FR")} · ${formatVersionSize(version.size_bytes)}`));
      if (version.error) info.append(node("span", "error-state", version.error));
      row.append(info);
      if (version.eligible && !version.active) {
        const button = node("button", "secondary-button", "Restaurer cette version");
        button.type = "button";
        button.addEventListener("click", async () => {
          if (!window.confirm(`Activer la version ${version.version_id} ? La version actuelle restera dans l'historique.`)) return;
          button.disabled = true;
          setText($("update-status"), `Validation de la version ${version.version_id}…`);
          try {
            await postJSON("/api/v2/maintenance/activate", {version_id: version.version_id});
            await refreshAfterMaintenanceUpdate();
            setText($("update-status"), `Version ${version.version_id} restaurée.`);
          } catch (error) { setText($("update-status"), `Restauration impossible : ${error.message}`); button.disabled = false; }
        });
        row.append(button);
      }
      target.append(row);
    }
  } catch (error) { target.replaceChildren(node("p", "error-state", `Historique indisponible : ${error.message}`)); }
}
$("version-history-toggle").addEventListener("click", () => {
  const list = $("version-history-list");
  list.hidden = !list.hidden;
  $("version-history-toggle").setAttribute("aria-expanded", String(!list.hidden));
  if (!list.hidden) void loadVersionHistory();
});
let editionsPromise = null;
let exactEditions = [];
function groupedEditionCode(code) {
  if (!code) return "";
  return state.editions.find(edition => edition.code === code || edition.code.split(",").includes(code))?.code || code;
}
function selectExactEditionCode(code) {
  const select = $("edition-select");
  if (![...select.options].some(option => option.value === code)) {
    const edition = exactEditions.find(edition => edition.code === code);
    select.append(edition ? makeEditionOption(edition) : new Option(`${code.toUpperCase()} · édition précise`, code));
  }
  select.value = code;
}
function groupEditions(editions) {
  const suffix = /^(?: (?:Commander|Tokens|Promos|Art Series|Eternal|Jumpstart Front Cards|Minigames|Southeast Asia Tokens))+(?: .*)?$/i;
  const named = editions.map(edition => ({ ...edition, familyName: edition.name.replace(/^Alchemy: /i, "") }));
  const groups = new Map();
  const rootFor = edition => {
    const parent = named.filter(candidate => candidate !== edition &&
      (edition.name.startsWith("Alchemy: ") && candidate.familyName === edition.familyName && !candidate.name.startsWith("Alchemy: ") ||
      edition.familyName.startsWith(candidate.familyName + " ") &&
      suffix.test(edition.familyName.slice(candidate.familyName.length))))
      .sort((a, b) => b.familyName.length - a.familyName.length)[0];
    return parent ? rootFor(parent) : edition;
  };
  for (const edition of named) {
    const root = rootFor(edition);
    const key = root.code;
    if (!groups.has(key)) groups.set(key, { ...root, members: [], card_total: 0, downloaded: 0 });
    const group = groups.get(key);
    group.members.push(edition.code);
    group.card_total += edition.card_total ?? 0;
    group.downloaded += edition.downloaded ?? 0;
  }
  return [...groups.values()].map(group => ({ ...group, code: [group.code, ...group.members.filter(code => code !== group.code)].join(",") }))
    .sort((a, b) => Number(b.code === PROXY_SET_CODE) - Number(a.code === PROXY_SET_CODE)
      || (b.released_at || "").localeCompare(a.released_at || "") || a.name.localeCompare(b.name));
}
function makeEditionOption(edition) {
  const codes = edition.code.split(",");
  const option = new Option(`${edition.name_fr || edition.name} (${codes[0]})`, edition.code);
  option.dataset.searchTerms = [edition.name_fr || edition.name, edition.name || edition.name_fr, ...codes].map(editionSearchKey).join("|");
  option.dataset.editionCode = codes[0];
  if (edition.edition_icon_url) option.dataset.editionIcon = edition.edition_icon_url;
  if (edition.card_total !== undefined) {
    option.dataset.editionTotal = edition.card_total;
    option.dataset.editionDownloaded = edition.downloaded ?? 0;
  }
  return option;
}
async function loadEditions() {
  const placeholder = $("edition-select").options[0];
  if (!state.editions.length) placeholder.textContent = "Chargement des éditions…";
  const all = []; let offset = 0; let total = Infinity;
  while (offset < total) { const page = await getJSON("/api/v2/editions", { limit: 100, offset }); total = page.total; all.push(...page.items); offset += page.items.length; if (!page.items.length) break; }
  placeholder.textContent = "Sélectionner…";
  exactEditions = all;
  const grouped = groupEditions(all);
  state.editions = grouped.map(e => ({ ...e, search_fr: editionSearchKey(e.name_fr || e.name), search_en: editionSearchKey(e.name || e.name_fr) }));
  for (const id of ["filter-set", "edition-select"]) {
    const select = $(id), selected = select.value;
    const options = document.createDocumentFragment(); options.append(select.options[0]);
    for (const edition of grouped) options.append(makeEditionOption(edition));
    select.replaceChildren(options);
    if (id === "edition-select" && state.exactEditionCode === selected) selectExactEditionCode(selected);
    else select.value = groupedEditionCode(selected);
    if (id === "filter-set") renderMultiFilter(id);
  }
  editionSuggestions.refresh();
  searchEditionSuggestions.refresh();
  setText($("edition-count"), "");
}
let editionReloadPromise = null;
function reloadEditions() {
  if (!editionReloadPromise) {
    editionReloadPromise = Promise.resolve(editionsPromise)
      .then(() => { editionsPromise = null; return ensureEditions(); })
      .finally(() => { editionReloadPromise = null; });
  }
  return editionReloadPromise;
}
async function ensureEditions(refreshProxies = false) {
  if (refreshProxies && editionsPromise) {
    await editionsPromise;
    try {
      const page = await getJSON("/api/v2/editions", { limit: 1 });
      const hasProxies = page.items[0]?.code === PROXY_SET_CODE;
      if (hasProxies !== state.editions.some(item => item.code === PROXY_SET_CODE)) editionsPromise = null;
    } catch { /* Keep the loaded edition list when refresh is unavailable. */ }
  }
  if (!editionsPromise) editionsPromise = loadEditions().catch(error => { editionsPromise = null; $("edition-select").options[0].textContent = "Sélectionner…"; setText($("edition-count"), `Éditions indisponibles : ${error.message}`); });
  return editionsPromise;
}
function editionSearchKey(value) { return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("fr"); }
function matchingEditionOptions(select, term) {
  const options = Array.from(select.options).filter(option => option.value);
  const terms = option => (option.dataset.searchTerms || editionSearchKey(option.textContent)).split("|");
  const starts = options.filter(option => terms(option).some(value => value.startsWith(term)));
  const contains = options.filter(option => !starts.includes(option) && terms(option).some(value => value.includes(term)));
  return [...starts, ...contains].slice(0, 8);
}
function attachEditionSuggestions(inputId, listId, selectId, onSelect, { keepOpen = false, onQuery = () => {} } = {}) {
  const input = $(inputId), list = $(listId), select = $(selectId);
  const field = input.closest(".edition-search");
  const storageKey = `database-magic-${inputId}-recent-v1`;
  let active = -1;
  const hide = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const choose = code => {
    rememberSearchTerm(input.value, storageKey);
    if (!keepOpen) {
      input.value = "";
      hide();
    }
    onSelect(code);
  };
  const updateQuery = () => {
    render();
    onQuery();
  };
  const render = () => {
    if (document.activeElement !== input && !list.contains(document.activeElement)) {
      hide();
      return;
    }
    const term = editionSearchKey(input.value.trim());
    const scrollTop = list.scrollTop;
    list.replaceChildren();
    active = -1;
    input.removeAttribute("aria-activedescendant");
    if (term) {
      const matches = matchingEditionOptions(select, term);
      for (const option of matches) {
        const item = node("div", "edition-suggestion");
        appendEditionOptionLabel(item, option);
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", "false");
        item.addEventListener("pointerdown", event => event.preventDefault());
        item.addEventListener("click", () => choose(option.value));
        list.append(item);
      }
      if (!matches.length) list.append(node("p", "edition-suggestion-empty", "Aucune édition trouvée."));
    }
    renderRecentSearchRows(input, list, storageKey, updateQuery, render);
    list.querySelectorAll('[role="option"]').forEach((option, index) => { option.id = `${listId}-${index}`; });
    list.hidden = !list.childElementCount;
    input.setAttribute("aria-expanded", String(!list.hidden));
    list.scrollTop = scrollTop;
  };
  input.addEventListener("input", updateQuery);
  input.addEventListener("focus", render);
  input.addEventListener("click", render);
  input.addEventListener("blur", event => { if (!list.contains(event.relatedTarget)) hide(); });
  list.addEventListener("focusout", event => { if (!field.contains(event.relatedTarget)) hide(); });
  window.addEventListener("blur", hide);
  document.addEventListener("visibilitychange", () => { if (document.hidden) hide(); });
  document.addEventListener("pointerdown", event => { if (!field.contains(event.target)) hide(); });
  field.addEventListener("keydown", event => {
    if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      input.focus();
      hide();
      return;
    }
    if (event.target !== input || list.hidden || event.isComposing) return;
    const options = [...list.querySelectorAll('[role="option"]')];
    if (["ArrowDown", "ArrowUp"].includes(event.key) && options.length) {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      active = active < 0 ? (direction > 0 ? 0 : options.length - 1) : (active + direction + options.length) % options.length;
      options.forEach((option, index) => option.setAttribute("aria-selected", String(index === active)));
      input.setAttribute("aria-activedescendant", options[active].id);
      options[active].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && options[active]) {
      event.preventDefault();
      options[active].click();
    }
  }, { capture: true });
  return { hide, refresh: () => { if (!list.hidden) render(); } };
}
function addEditionToFilter(id, status, code) {
  const value = multiValue(id);
  const selected = value && value !== "-" ? value.split("|") : [];
  if (selected.includes(code)) {
    setText(status, "Édition déjà sélectionnée.");
    return;
  }
  setMultiValue(id, [...selected, code].join("|"));
  setText(status, "Édition ajoutée. Cliquez sur Rechercher.");
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    $(`${id}-multi`).querySelector("summary").animate(
      [{ backgroundColor: "#59446e", borderColor: "#a287c4" }, { backgroundColor: "", borderColor: "" }],
      { duration: 650, easing: "ease-out" }
    );
  }
}
const REFINE_IDS = { image_filter: "refine-images", result_sort: "refine-sort", result_order: "refine-order", mana_min: "refine-mana-min", mana_max: "refine-mana-max", power_min: "refine-power-min", power_max: "refine-power-max", toughness_min: "refine-toughness-min", toughness_max: "refine-toughness-max" };
function mainSearchFilters() { return { q: $("query").value.trim(), set: multiValue("filter-set"), rarity: multiValue("filter-rarity").toLowerCase(), color: selectedManaColors($("filter-color")), color_mode: $("filter-color-mode").value, card_type: multiValue("filter-type"), oracle_id: state.searchIdentity?.oracle_id || "", printing_id: state.searchIdentity?.printing_id || "", unique: $("search-unique").checked ? "1" : "" }; }
function refineSearchFilters() { return Object.fromEntries(Object.entries(REFINE_IDS).map(([key, id]) => [key, $(id).value])); }
function captureSearchDraft() { return { route: location.hash, main: mainSearchFilters(), refine: refineSearchFilters() }; }
function readSearchDraft() {
  try {
    const value = JSON.parse(localStorage.getItem(SEARCH_DRAFT_KEY) || "null");
    return value && typeof value === "object" && value.main && value.refine ? value : null;
  } catch { return null; }
}
function applySearchDraft(draft) {
  const main = draft.main || {}, refine = draft.refine || {};
  $("query").value = main.q || "";
  setMultiValue("filter-set", main.set);
  setMultiValue("filter-rarity", main.rarity);
  restoreManaColors($("filter-color"), main.color);
  $("filter-color-mode").value = main.color_mode || "contains";
  setMultiValue("filter-type", main.card_type);
  $("search-unique").checked = main.unique === "1";
  state.searchIdentity = { oracle_id: main.oracle_id || "", printing_id: main.printing_id || "" };
  for (const [key, id] of Object.entries(REFINE_IDS)) {
    const fallback = key === "image_filter" ? "all" : key === "result_order" ? "desc" : key === "result_sort" ? "date" : "";
    $(id).value = refine[key] || fallback;
  }
}
function persistSearchDraft() {
  try { localStorage.setItem(SEARCH_DRAFT_KEY, JSON.stringify(captureSearchDraft())); } catch {}
}
function recentSearchTerms(storageKey) {
  try {
    const items = JSON.parse(localStorage.getItem(storageKey) || "[]");
    return Array.isArray(items) ? items.filter(value => typeof value === "string" && value.trim()).slice(0, 10) : [];
  } catch { return []; }
}
function rememberSearchTerm(value, storageKey = RECENT_SEARCHES_KEY) {
  const term = String(value || "").trim();
  if (!term) return;
  const items = recentSearchTerms(storageKey).filter(item => item.toLocaleLowerCase("fr") !== term.toLocaleLowerCase("fr"));
  items.unshift(term);
  try { localStorage.setItem(storageKey, JSON.stringify(items.slice(0, 10))); } catch {}
}
function forgetSearchTerm(term, storageKey) {
  const items = recentSearchTerms(storageKey).filter(item => item.toLocaleLowerCase("fr") !== term.toLocaleLowerCase("fr"));
  try { localStorage.setItem(storageKey, JSON.stringify(items)); } catch {}
}
function renderRecentSearchRows(input, list, storageKey, select, refresh) {
  const key = input.value.trim().toLocaleLowerCase("fr");
  const terms = recentSearchTerms(storageKey).filter(term => {
    const name = term.toLocaleLowerCase("fr");
    return name !== key && (!key || name.includes(key));
  }).slice(0, 8);
  for (const term of terms) {
    const row = node("div", "recent-search-row");
    const option = node("button", "edition-suggestion", term);
    option.type = "button";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.addEventListener("pointerdown", event => event.preventDefault());
    option.addEventListener("click", () => {
      input.value = term;
      input.focus();
      select();
    });
    const remove = node("button", "recent-search-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Supprimer « ${term} » des recherches récentes`);
    remove.addEventListener("pointerdown", event => event.preventDefault());
    remove.addEventListener("click", () => {
      forgetSearchTerm(term, storageKey);
      input.focus();
      refresh();
    });
    row.append(option, remove);
    list.append(row);
  }
}
function bindRecentSearches(inputId, listId, storageKey, onSelect = () => {}) {
  const input = $(inputId), list = $(listId), field = input.closest(".search-main");
  const hide = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
  };
  const render = () => {
    if (!document.hasFocus() || (document.activeElement !== input && !list.contains(document.activeElement))) {
      hide();
      return;
    }
    list.replaceChildren();
    renderRecentSearchRows(input, list, storageKey, () => {
      hide();
      onSelect();
    }, render);
    list.hidden = !list.childElementCount;
    input.setAttribute("aria-expanded", String(!list.hidden));
  };
  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  input.addEventListener("click", render);
  input.addEventListener("blur", event => {
    if (!list.contains(event.relatedTarget)) hide();
  });
  list.addEventListener("focusout", event => {
    if (event.relatedTarget !== input && !list.contains(event.relatedTarget)) hide();
  });
  window.addEventListener("blur", hide);
  document.addEventListener("visibilitychange", () => { if (document.hidden) hide(); });
  document.addEventListener("pointerdown", event => { if (!field.contains(event.target)) hide(); });
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") hide();
    if (event.key === "ArrowDown" && !list.hidden) {
      const first = list.querySelector('[role="option"]');
      if (first) { event.preventDefault(); first.focus(); }
    }
  });
  list.addEventListener("keydown", event => {
    if (!["ArrowDown", "ArrowUp", "Escape"].includes(event.key)) return;
    if (event.key === "Escape") {
      event.preventDefault();
      input.focus();
      hide();
      return;
    }
    const options = [...list.querySelectorAll('[role="option"]')];
    const index = options.indexOf(document.activeElement);
    const next = event.key === "ArrowDown" ? Math.min(options.length - 1, index + 1) : Math.max(0, index - 1);
    if (options[next]) { event.preventDefault(); options[next].focus(); }
  });
  return hide;
}
const hideRecentSearches = bindRecentSearches("query", "search-suggestions", RECENT_SEARCHES_KEY, () => {
  state.searchIdentity = null;
  searchDraftTouched = true;
  persistSearchDraft();
  setText($("search-status"), "Terme repris. Cliquez sur Rechercher.");
});
const hideDeckSuggestions = bindRecentSearches("deck-query", "deck-suggestions", RECENT_DECK_SEARCHES_KEY);
function searchFilters() {
  const refined = state.refineFilters || refineSearchFilters();
  const direction = refined.result_order || "desc";
  const sortField = refined.result_sort || "date";
  const resultSort = sortField === "date" ? (direction === "asc" ? "date_asc" : "date") : `${sortField}_${direction}`;
  const { result_order, ...backendRefined } = refined;
  return { ...(state.searchBaseFilters || mainSearchFilters()), ...backendRefined, result_sort: resultSort, limit: SEARCH_PAGE_SIZE, offset: state.searchPage * SEARCH_PAGE_SIZE };
}
const SEARCH_PAGE_ROUTE = "/api/v2/search/page";
function searchFilterKey() {
  return params({ ...searchFilters(), offset: null });
}
function runSearch(updateDraft = true) {
  state.searchExecuted = true;
  state.searchSnapshot = null;
  state.searchSnapshotFilters = null;
  invalidatePageCache(SEARCH_PAGE_ROUTE);
  return loadSearchPage(null, updateDraft);
}
async function loadSearchPage(snapshot, updateDraft = false) {
  if (state.view === "recherche") syncRoute();
  if (updateDraft) persistSearchDraft();
  hideRecentSearches();
  state.searchController?.abort();
  const controller = new AbortController();
  state.searchController = controller;
  const request = ++state.searchRequest;
  $("search-status").replaceChildren(node("span", "card-loading-spinner search-loading-spinner"),
    node("span", "", snapshot ? "Chargement de la page…" : "Recherche…"));
  const filters = searchFilters();
  const pageQuery = { snapshot, limit: filters.limit, offset: filters.offset };
  cancelPagePrefetch();
  state.searchResultKey = null;
  if (snapshot || updateDraft || !$("results").hasChildNodes()) {
    pageControls.get("search-pager")(0, 1);
    $("results").replaceChildren(pageLoading("Chargement des cartes…"));
  }
  try {
    const options = { signal: controller.signal };
    const fromCache = snapshot && pageCache(SEARCH_PAGE_ROUTE, pageQuery).pages.has(pageQuery.offset);
    const data = snapshot ? await fetchCataloguePage(SEARCH_PAGE_ROUTE, pageQuery, options)
      : await getJSON("/api/v2/search", { ...filters, snapshot: 1 }, options);
    if (request !== state.searchRequest) return;
    state.searchPage = Math.floor(data.offset / SEARCH_PAGE_SIZE);
    state.searchSnapshot = data.snapshot;
    state.searchSnapshotFilters = searchFilterKey();
    state.searchResultKey = params(searchFilters());
    if (state.view === "recherche") syncRoute();
    state.searchTotal = data.total;
    state.searchItems = data.items || [];
    renderEditionItems($("results"), state.searchItems);
    updateSearchPager(); updateBatchButtons();
    setText($("search-status"), `${formatNumber(data.total)} résultat(s)`);
    const activeQuery = { ...pageQuery, snapshot: data.snapshot, offset: data.offset };
    pageCache(SEARCH_PAGE_ROUTE, activeQuery).pages.set(data.offset, data);
    if (fromCache) {
      state.searchController = null;
      await refreshSearchImages();
    }
    if (request === state.searchRequest) prefetchNeighbourPages(SEARCH_PAGE_ROUTE, activeQuery, data.total, $("results"),
      () => state.view === "recherche" && request === state.searchRequest);
  } catch (error) {
    if (request === state.searchRequest) {
      showError($("results"), error.message);
      setText($("search-status"), snapshot ? "Chargement de la page impossible." : "La recherche a échoué.");
    }
  }
  finally {
    if (state.searchController === controller) {
      state.searchController = null;
      updateSearchPager();
    }
  }
}
async function refreshSearchImages() {
  const items = state.searchItems, request = state.searchRequest;
  if (state.searchController) return;
  const printings = [...new Set(items.filter(item => !item.proxy).map(item => item.printing_id).filter(Boolean))];
  if (!printings.length) return;
  const controller = new AbortController();
  state.searchController = controller;
  setText($("search-status"), "Actualisation des images locales…");
  try {
    const data = await postJSON("/api/v2/search/image-slots", {
      printing_ids: printings,
    }, { signal: controller.signal });
    if (request !== state.searchRequest || items !== state.searchItems) return;
    for (const item of items) if (!item.proxy) item.image_slots = data.image_slots?.[item.printing_id] || [];
    renderEditionItems($("results"), items);
    setText($("search-status"), `${formatNumber(state.searchTotal)} résultat(s) · images actualisées`);
  } catch (error) {
    if (request === state.searchRequest && items === state.searchItems) {
      setText($("search-status"), `Actualisation des images impossible : ${error.message}`);
    }
  } finally {
    if (state.searchController === controller) state.searchController = null;
  }
}
function updateSearchPager() {
  const pages = Math.max(1, Math.ceil(state.searchTotal / SEARCH_PAGE_SIZE));
  pageControls.get("search-pager")(state.searchPage, pages, `${formatNumber(state.searchTotal)} résultats`);
}
let batchTaskId = null, batchMode = null, batchPollTimer = null;
const BATCH_BASELINE_SECONDS_PER_IMAGE = 8 * 3600 / 11376;
function batchTimeRemaining(task) {
  if (task.status !== "running" || !task.total || !task.processing_started_at) return "";
  const elapsed = Math.max(0, Date.now() / 1000 - task.processing_started_at);
  const observed = task.completed > 0 ? elapsed / task.completed : BATCH_BASELINE_SECONDS_PER_IMAGE;
  const weight = task.completed / (task.completed + 50);
  const secondsPerImage = BATCH_BASELINE_SECONDS_PER_IMAGE * (1 - weight) + observed * weight;
  const sinceProgress = Math.max(0, Date.now() / 1000 - (task.last_progress_at || task.processing_started_at));
  const seconds = Math.max(0, Math.ceil((task.total - task.completed) * secondsPerImage - sinceProgress));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return ` · temps restant estimé : ${hours ? `${hours} h ` : ""}${minutes} min`;
}
function deckResultFilters() { return { q: $("deck-query").value.trim(), deck_format: multiValue("deck-format"), deck_type: multiValue("deck-type"), set_code: multiValue("deck-edition"), colors: selectedManaColors(document.querySelector(".deck-color-filter")), color_mode: $("deck-color-mode").value, sort: $("deck-sort").value, order: $("deck-order").value, image_filter: $("deck-image-filter").value }; }
function deckHasFilter() { const f = state.deckAppliedFilters || {}; return Boolean(f.q || f.deck_format || f.deck_type || f.set_code || f.colors); }
function updateBatchButtons() {
  $("download-search-results").disabled = Boolean(batchTaskId) || !state.searchExecuted || !state.searchTotal || state.searchBaseFilters?.set === PROXY_SET_CODE;
  const edition = $("edition-select").value;
  $("download-edition-results").disabled = Boolean(batchTaskId) || !edition || edition === PROXY_SET_CODE || !state.editionCardsTotal;
  $("download-deck-results").disabled = Boolean(batchTaskId) || !state.deckExecuted || !deckHasFilter() || !state.deckTotal;
}
function batchStatusElement(mode) { return $({ search: "search-batch-status", edition: "edition-batch-status", decks: "deck-batch-status" }[mode]); }
async function openBatchDownloadTask(task) {
  const mode = task.mode;
  if (!batchStatusElement(mode)) return;
  const filters = task.filters || {};
  batchTaskId = task.task_id;
  batchMode = mode;
  if (mode === "edition") {
    await ensureEditions();
    if (filters.set) {
      $("edition-query").value = "";
      $("edition-select").value = filters.set;
      state.exactEditionCode = null;
      state.editionPage = 0;
    }
    setView("editions");
    void renderEditionPage();
  } else if (mode === "search") {
    applySearchDraft({ main: { q: filters.q, set: filters.set, rarity: filters.rarity,
      color: filters.color, color_mode: filters.color_mode, card_type: filters.card_type },
      refine: { ...filters, result_sort: (filters.result_sort || "date").split("_")[0],
        result_order: (filters.result_sort || "date_desc").split("_")[1] || "desc" } });
    state.searchBaseFilters = mainSearchFilters();
    state.refineFilters = refineSearchFilters();
    state.searchPage = 0;
    setView("recherche");
    void runSearch();
  } else {
    await loadDeckFacets();
    $("deck-query").value = filters.q || "";
    setMultiValue("deck-format", filters.deck_format);
    setMultiValue("deck-type", filters.deck_type);
    setMultiValue("deck-edition", filters.set_code);
    restoreManaColors(document.querySelector(".deck-color-filter"), filters.colors);
    $("deck-color-mode").value = filters.color_mode || "contains";
    $("deck-sort").value = filters.sort || "date";
    $("deck-order").value = filters.order || "desc";
    $("deck-image-filter").value = filters.image_filter || "all";
    state.deckAppliedFilters = deckResultFilters();
    state.deckExecuted = true;
    state.deckPage = 0;
    closeDeck(null);
    setView("decks");
    void renderDeckPage();
  }
  updateBatchButtons();
  void pollBatchDownload();
  $("notification-panel").hidden = true;
  notificationButton.setAttribute("aria-expanded", "false");
}
async function startBatchDownload(mode) {
  const count = mode === "search" ? state.searchTotal : mode === "edition" ? state.editionCardsTotal : state.deckTotal;
  if (!count || mode === "decks" && !deckHasFilter()) return;
  const unit = mode === "decks" ? "decks" : "cartes";
  if (count > 100 || mode === "decks") {
    if (!window.confirm(`Télécharger les images manquantes de ${formatNumber(count)} ${unit} ? Cette opération peut utiliser beaucoup de données.`)) return;
  }
  const filters = mode === "search" ? Object.fromEntries(Object.entries(searchFilters()).filter(([key]) => !["limit", "offset"].includes(key)))
    : mode === "edition" ? { set: $("edition-select").value } : state.deckAppliedFilters;
  const status = batchStatusElement(mode);
  setText(status, "Analyse des cartes et des images déjà présentes…");
  try {
    const task = await postJSON("/api/v2/batch-download", { mode, filters });
    batchTaskId = task.task_id; batchMode = mode;
    updateBatchButtons(); void pollBatchDownload();
  } catch (error) { setText(status, `Téléchargement impossible : ${error.message}`); }
}
async function pollBatchDownload() {
  if (!batchTaskId) return;
  clearTimeout(batchPollTimer);
  try {
    const task = await getJSON("/api/v2/batch-download/task", { task_id: batchTaskId });
    const status = batchStatusElement(batchMode);
    if (["queued", "running"].includes(task.status) && !task.total) {
      const spinner = node("span", "card-loading-spinner batch-preparation-spinner");
      spinner.setAttribute("aria-hidden", "true");
      status.replaceChildren(spinner, node("span", "", "Analyse des cartes, suppression des doublons et vérification des images déjà présentes…"));
    } else status.replaceChildren(node("span", "", `${task.completed}/${task.total} impressions traitées${batchTimeRemaining(task)} · ${task.ready} prêtes · ${task.failed} échecs · ${task.unavailable} indisponibles${task.current ? ` · ${task.current}` : ""}`));
    if (task.errors?.length) {
      const details = node("details", "deck-errors"); details.append(node("summary", "error-state", `${task.errors.length} erreur(s)`));
      const list = node("ul"); for (const error of task.errors) list.append(node("li", "", error)); details.append(list); status.append(details);
    }
    if (["queued", "running"].includes(task.status)) {
      const cancel = node("button", "secondary-button", "Annuler"); cancel.type = "button";
      cancel.addEventListener("click", async () => { cancel.disabled = true; await postJSON("/api/v2/batch-download/cancel", { task_id: batchTaskId }); });
      status.append(cancel); batchPollTimer = setTimeout(pollBatchDownload, 1500); return;
    }
    status.prepend(node("strong", "", task.status === "complete" ? "Terminé · " : task.status === "cancelled" ? "Annulé · " : "Échec · "));
    batchTaskId = null; batchMode = null; updateBatchButtons();
    if (editionsPromise) void reloadEditions();
    if (state.view === "recherche" && state.searchItems.length) void refreshSearchImages();
    if (state.view === "editions") void refreshEditionPage();
    if (state.view === "decks") void renderDeckPage(true);
  } catch (error) { batchPollTimer = setTimeout(pollBatchDownload, 3000); }
}
function openEdition(code) { $("edition-query").value = ""; editionSuggestions.hide(); state.exactEditionCode = null; $("edition-select").value = groupedEditionCode(code); state.activeSet = $("edition-select").value; state.editionPage = 0; setView("editions"); }
let editionPageKey = null;
let pendingEditionFocus = null;
let editionController = null;
let editionRefreshPending = false;
function editionSelectionKey() {
  return JSON.stringify([$("edition-select").value, state.editionPage]);
}
function ensureEditionPage() {
  if (state.view === "editions" && editionPageKey !== editionSelectionKey()) return renderEditionPage();
}
function refreshEditionPage() {
  if (editionController) {
    editionRefreshPending = true;
    return;
  }
  return renderEditionPage(true);
}
async function renderEditionPage(background = false) {
  $("edition-select").dispatchEvent(new Event("input"));
  editionController?.abort();
  editionController = null;
  editionRefreshPending = false;
  editionPageKey = editionSelectionKey();
  if (state.view === "editions" && !pendingEditionFocus) syncRoute();
  const request = ++state.editionRequest;
  const code = $("edition-select").value; state.activeSet = code; const container = $("edition-cards");
  const focus = pendingEditionFocus;
  pendingEditionFocus = null;
  if (background) invalidatePageCache("/api/v2/edition");
  else cancelPagePrefetch();
  $("download-edition-results").disabled = true;
  if (!code) {
    container.replaceChildren();
    state.editionCardsTotal = 0;
    updateEditionPager();
    updateBatchButtons();
    return;
  }
  const controller = new AbortController();
  editionController = controller;
  if (!background) {
    pageControls.get("edition-pager")(0, 1);
    container.replaceChildren(pageLoading("Chargement des cartes…"));
  }
  try {
    const path = "/api/v2/edition";
    const query = { set: code, limit: EDITION_PAGE_SIZE, offset: state.editionPage * EDITION_PAGE_SIZE };
    if (focus) invalidatePageCache(path);
    const data = await fetchCataloguePage(path, focus ? { ...query, focus } : query, { signal: controller.signal });
    if (request !== state.editionRequest) return;
    if (focus) {
      query.offset = data.offset;
      state.editionPage = Math.floor(data.offset / EDITION_PAGE_SIZE);
      editionPageKey = editionSelectionKey();
      if (state.view === "editions") syncRoute();
    }
    state.editionCardsTotal = data.total;
    renderEditionItems(container, data.items);
    updateEditionPager();
    updateBatchButtons();
    const current = () => state.view === "editions" && request === state.editionRequest;
    const prefetch = () => prefetchNeighbourPages(path, query, data.total, container, current);
    if (!focus || !current() || !revealEditionCard(container, focus, current, prefetch)) prefetch();
  }
  catch (error) {
    if (request === state.editionRequest) {
      editionPageKey = null;
      if (!background) showError(container, error.message);
    }
  } finally {
    if (editionController === controller) {
      editionController = null;
      updateEditionPager();
      const refresh = editionRefreshPending;
      editionRefreshPending = false;
      if (refresh && state.view === "editions" && editionPageKey === editionSelectionKey()) void refreshEditionPage();
    }
  }
}
function updateEditionPager() {
  const pages = Math.max(1, Math.ceil(state.editionCardsTotal / EDITION_PAGE_SIZE));
  pageControls.get("edition-pager")(state.editionPage, pages, `${formatNumber(state.editionCardsTotal)} cartes`);
}
function revealEditionCard(container, printing, isCurrent, afterScroll) {
  const tile = container.querySelector(`[data-printing-id="${CSS.escape(printing)}"]`);
  if (!tile) return false;
  requestAnimationFrame(() => {
    if (!isCurrent() || !tile.isConnected) return;
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const highlight = () => {
      clearTimeout(fallback);
      document.removeEventListener("scrollend", highlight);
      if (!isCurrent() || !tile.isConnected) return;
      tile.classList.add("edition-target");
      setTimeout(() => tile.classList.remove("edition-target"), 2600);
      afterScroll();
    };
    const fallback = setTimeout(highlight, 700);
    tile.scrollIntoView({ block: "center", behavior: reducedMotion ? "instant" : "smooth" });
    if (reducedMotion || Math.abs(tile.getBoundingClientRect().top + tile.offsetHeight / 2 - innerHeight / 2) < 2) highlight();
    else document.addEventListener("scrollend", highlight, { once: true });
  });
  return true;
}
function galleryRecords(items) {
  const byPrinting = new Map();
  for (const item of items || []) {
    const key = item.printing_id || item.provider_card_id;
    if (!key) continue;
    const old = byPrinting.get(key);
    if (!old || item.lang === "fr") byPrinting.set(key, { provider_card_id: item.provider_card_id, printing_id: key, lang: item.lang });
  }
  return [...byPrinting.values()];
}
function galleryContextFor(providerId) {
  const id = String(providerId || "");
  const visible = selector => [...document.querySelectorAll(selector)].map(el => ({ provider_card_id: el.dataset.providerId, printing_id: el.dataset.printingId, lang: el.dataset.lang }));
  if (state.view === "info" && document.querySelector(`#list-import-results .edition-card-tile[data-provider-id="${CSS.escape(id)}"]`))
    return { kind: "list", rows: galleryRecords(visible("#list-import-results .edition-card-tile")), current: id };
  if (state.view === "recherche") return { kind: "search", page: state.searchPage, snapshot: state.searchSnapshot, total: state.searchTotal, rows: galleryRecords(visible("#results .edition-card-tile")), current: id };
  if (state.view === "editions" && state.activeSet) return { kind: "edition", page: state.editionPage, set: state.activeSet, total: state.editionCardsTotal, rows: galleryRecords(visible("#edition-cards .edition-card-tile")), current: id };
  if (state.view === "decks" && state.selectedDeck) {
    const rows = [];
    const sections = Object.entries(state.selectedDeck.sections || {});
    const ordered = sections.slice().sort(([a], [b]) => a === "commander" ? -1 : b === "commander" ? 1 : a === "tokens" ? 1 : b === "tokens" ? -1 : 0);
    for (const [, cards] of ordered) for (const card of cards) if (card.provider_card_id) rows.push(card);
    return { kind: "deck", rows: galleryRecords(rows), current: id };
  }
  if (state.view === "proxy")
    return { kind: "proxy", page: proxyState.page, total: proxyState.total, rows: galleryRecords(visible("#personal-proxy-grid .edition-card-tile")), current: id };
  if (state.view === "perso" && personalState.deck) {
    const cards = personalState.deck.sections.flatMap(section => section.cards).filter(card => card.provider_card_id);
    const opened = cards.find(card => String(card.provider_card_id) === id);
    if (opened) {
      const rows = galleryRecords(cards);
      const key = opened.printing_id || opened.provider_card_id;
      const index = rows.findIndex(row => row.printing_id === key);
      rows[index] = { ...rows[index], provider_card_id: opened.provider_card_id };
      return { kind: "deck", rows, current: id };
    }
  }
  return { kind: "single", rows: [{ provider_card_id: id, printing_id: id }], current: id };
}
let galleryNavigation = 0;
const galleryPageSizes = { search: SEARCH_PAGE_SIZE, edition: EDITION_PAGE_SIZE, proxy: PROXY_PAGE_SIZE };
function navigateCardGallery(direction) {
  void galleryNeighbor(direction).catch(error => console.error("Impossible de changer de carte.", error));
}
async function galleryNeighbor(direction) {
  const ctx = state.cardGallery; if (!ctx) return;
  const navigation = ++galleryNavigation;
  const request = state.cardRequest;
  const isCurrent = () => navigation === galleryNavigation && request === state.cardRequest && $("card-dialog").open;
  let rows = ctx.rows || [], index = rows.findIndex(x => String(x.provider_card_id) === ctx.current);
  if (["search", "edition", "proxy"].includes(ctx.kind)) {
    const pageSize = galleryPageSizes[ctx.kind];
    const getPage = async page => {
      const path = { search: SEARCH_PAGE_ROUTE, edition: "/api/v2/edition", proxy: "/api/v2/proxies" }[ctx.kind];
      const values = { ...(ctx.kind === "search" ? { snapshot: ctx.snapshot } : { set: ctx.set }),
        limit: pageSize, offset: page * pageSize };
      if (ctx.kind === "proxy") delete values.set;
      const cache = cataloguePageCaches.get(path);
      const key = params({ ...values, offset: null });
      const cached = cache?.key === key ? cache.pages.get(values.offset) : null;
      const data = cached || await getJSON(path, values);
      return galleryRecords(data.items);
    };
    rows = ctx.rows || await getPage(ctx.page);
    if (!isCurrent()) return;
    index = rows.findIndex(x => ctx.currentPrinting ? x.printing_id === ctx.currentPrinting : String(x.provider_card_id) === ctx.current);
    if (index < 0) return;
    let page = ctx.page, target = index + direction;
    while (true) {
      if (target >= 0 && target < rows.length && rows[target].printing_id !== ctx.currentPrinting) break;
      if (direction < 0) {
        if (target >= 0 && target < rows.length) { target--; continue; }
        if (page <= 0) return;
        page--; rows = await getPage(page); target = rows.length - 1;
      } else {
        if (target >= 0 && target < rows.length) { target++; continue; }
        if ((page + 1) * pageSize >= ctx.total) return;
        page++; rows = await getPage(page); target = 0;
      }
      if (!isCurrent()) return;
    }
    ctx.page = page; ctx.rows = rows; await openCard(rows[target].provider_card_id, ctx);
  } else if (rows[index + direction]) await openCard(rows[index + direction].provider_card_id, ctx);
}
async function openImagePreviewTask(taskId) {
  try {
    const task = await getJSON("/api/v2/image/task", { task_id: taskId });
    if (task.kind !== "image_preview" || task.status !== "needs_review" || task.result?.status !== "candidate") return;
    const choiceDialog = $("image-choice-dialog");
    if (choiceDialog.open) choiceDialog.close();
    await openCard(task.provider_card_id, null, task.face_index, task);
  } catch (error) { console.error("Impossible de rouvrir la recherche d’image.", error); }
}
async function showCardUsages(card) {
  const dialog = node("dialog", "card-usages-dialog");
  const title = node("h2", "", "Éditions et decks utilisant cette carte");
  const tabs = node("div", "card-usages-tabs"); tabs.setAttribute("role", "tablist");
  const content = node("div", "card-usages-content");
  const loading = node("p", "card-usages-loading muted");
  const spinner = node("span", "card-loading-spinner search-loading-spinner"); spinner.setAttribute("aria-hidden", "true");
  loading.append(spinner, node("span", "", "Recherche des occurrences locales…")); content.append(loading);
  const close = node("button", "secondary-button", "Fermer"); close.type = "button";
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("pointerdown", event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
  dialog.append(title, tabs, content, close); document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true }); dialog.showModal();
  const group = (label, entries, route, name, icon) => {
    const section = node("section", "card-usages-group");
    section.append(node("h3", "", label));
    if (!entries?.length) section.append(node("p", "muted", "Aucune occurrence locale connue."));
    for (const entry of entries || []) {
      const link = node("a", "secondary-button");
      link.href = `${location.origin}/${route(entry)}`; link.target = "_blank"; link.rel = "noopener noreferrer";
      const symbol = icon(entry);
      if (symbol) link.append(symbol);
      link.append(node("span", "", name(entry)));
      section.append(link);
    }
    return section;
  };
  try {
    const data = await getJSON("/api/v2/card/usage", { id: card.provider_card_id });
    if (!dialog.open) return;
    const renderScope = scope => {
      const entries = data[scope] || data;
      content.replaceChildren(
        group("Éditions", entries.editions, e => `#editions?${params({ set: e.code, exact: 1, focus: e.printing_id })}`, e => `${e.name_fr || e.name || e.code} (${e.code.toUpperCase()})`, e => {
          if (!e.edition_icon_url) return null;
          const image = node("img", "card-usages-edition-icon");
          image.src = e.edition_icon_url; image.alt = ""; image.loading = "lazy";
          image.addEventListener("error", () => image.remove(), { once: true });
          return image;
        }),
        group("Decks préconstruits chargés", entries.decks, d => `#decks?deck=${encodeURIComponent(d.id)}`, d => d.name_fr || d.name || d.id, d => colorSymbols(d.colors)),
        group("Decks personnels", entries.personal_decks, d => `#perso?deck=${encodeURIComponent(d.id)}`, d => d.name || d.id, d => colorSymbols(d.colors)));
      if (data.unloaded_decks) content.append(node("p", "muted", "Les decks préconstruits non encore chargés ne sont pas examinés."));
      for (const button of tabs.children) button.setAttribute("aria-selected", String(button.dataset.scope === scope));
    };
    for (const [scope, label] of [["exact", "Carte et édition précises"], ["global", "Carte globale"]]) {
      const button = node("button", "secondary-button", label); button.type = "button";
      button.dataset.scope = scope; button.setAttribute("role", "tab");
      button.addEventListener("click", () => renderScope(scope)); tabs.append(button);
    }
    renderScope("exact");
  } catch (error) { if (dialog.open) content.replaceChildren(node("p", "error-state", error.message)); }
}
async function openCard(providerId, gallery = null, preferredFace = null, resumedPreview = null) {
  const dialog = $("card-dialog"), target = $("card-detail");
  const focusedDirection = gallery ? document.activeElement?.dataset.cardGalleryDirection : null;
  state.cardController?.abort();
  const controller = new AbortController();
  state.cardController = controller;
  const request = ++state.cardRequest;
  const id = String(providerId);
  const context = gallery || galleryContextFor(providerId);
  context.current = id;
  context.currentPrinting = null;
  state.cardGallery = context;
  hoverPreview.hidden = true;
  dialog.querySelector(".proxy-association-preview")?.remove();
  let stage = target.dataset.providerId === id ? target.querySelector(".card-image-stage") : null;
  if (!stage) {
    const shell = node("section", "card-viewer");
    shell.append(node("header", "card-viewer-header"), node("div", "card-viewer-image", ""));
    shell.querySelector(".card-viewer-image").append(node("div", "card-image-stage"));
    target.replaceChildren(shell);
    stage = shell.querySelector(".card-image-stage");
  }
  target.dataset.providerId = id;
  stage.replaceChildren(node("div", "card-loading-spinner"));
  target.classList.add("card-detail-loading");
  if (!dialog.open) dialog.showModal();
  try {
    const data = await getJSON("/api/v2/card", { id: providerId }, { signal: controller.signal });
    if (request !== state.cardRequest || !dialog.open) return;
    if (!data.found) { target.replaceChildren(node("p", "empty-state", "Cette fiche n’existe pas dans le catalogue.")); target.classList.remove("card-detail-loading"); return; }
    const card = data.card; context.currentPrinting = card.printing_id || card.provider_card_id;
    const shell = node("section", "card-viewer");
    const header = node("header", "card-viewer-header");
    const rows = context.rows || [];
    const pageSize = galleryPageSizes[context.kind];
    const index = rows.findIndex(row => pageSize ? row.printing_id === context.currentPrinting : String(row.provider_card_id) === context.current);
    const navigationButton = (direction, label, icon) => {
      const button = node("button", "secondary-button card-gallery-arrow");
      button.type = "button";
      button.dataset.cardGalleryDirection = String(direction);
      button.setAttribute("aria-label", label);
      button.title = label;
      button.append(cardActionIcon(icon));
      const hasAdjacentPage = pageSize && (direction < 0 ? context.page > 0 : (context.page + 1) * pageSize < context.total);
      button.disabled = index < 0 || (!rows[index + direction] && !hasAdjacentPage);
      button.addEventListener("click", () => navigateCardGallery(direction));
      return button;
    };
    const previousCard = navigationButton(-1, "Carte précédente", "arrowLeft");
    const nextCard = navigationButton(1, "Carte suivante", "arrowRight");
    const edition = node("p", "eyebrow");
    const icon = editionIcon(card.set_code, card.edition_icon_url);
    if (icon) edition.append(icon);
    edition.append(node("span", "", [card.edition || card.set_code, card.collector_number && `n° ${card.collector_number}`].filter(Boolean).join(" · ")));
    header.append(previousCard, edition, nextCard);
    const imageArea = node("div", `card-viewer-image${card.proxy ? (card.proxy_source_id ? " linked-proxy-image" : " unlinked-proxy-image") : ""}`);
    const slots = (card.image_slots || []).slice().sort((a,b) => a.face_index - b.face_index);
    let selectedFace = preferredFace !== null && slots.some(x => x.face_index === preferredFace)
      ? preferredFace
      : slots.some(x => x.status === "ready") ? slots.find(x => x.status === "ready").face_index : (slots[0]?.face_index ?? 0);
    const imageWrap = node("div", "card-image-stage");
    const controls = node("div", "card-image-actions");
    const details = node("aside", "card-facts");
    const association = card.proxy ? proxyAssociationPanel(card, context) : null;
    let detailLanguage = "fr";
    let copyTimer;
    const copyNotice = node("div", "card-copy-notice", "Copié dans le presse-papiers");
    copyNotice.setAttribute("role", "status");
    const showCopyNotice = message => {
      copyNotice.textContent = message;
      copyNotice.classList.add("visible");
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => copyNotice.classList.remove("visible"), 2200);
    };
    const symbolClass = token => {
      const value = token.toUpperCase();
      if (/^(?:[0-9]|1[0-9]|20|100|1000000|[WUBRGCSXYZEH])$/.test(value)) return value.toLowerCase();
      if (value === "T") return "tap";
      if (value === "Q") return "untap";
      if (/^(?:[WUBRG]\/P|[2C]\/W|[2C]\/U|[2C]\/B|[2C]\/R|[2C]\/G|[WUBRG]\/[WUBRG]|[WUBRG]\/[WUBRG]\/P)$/.test(value)) return value.replaceAll("/", "").toLowerCase();
      return null;
    };
    const symbolText = value => {
      const fragment = document.createDocumentFragment();
      const source = String(value);
      const pattern = /\{([^{}]+)\}/g;
      let cursor = 0;
      for (const match of source.matchAll(pattern)) {
        fragment.append(document.createTextNode(source.slice(cursor, match.index)));
        const kind = symbolClass(match[1]);
        if (kind) {
          const symbol = node("span", `ms ms-cost ms-${kind} card-rule-symbol`);
          symbol.setAttribute("role", "img"); symbol.setAttribute("aria-label", match[1]);
          symbol.title = match[0]; fragment.append(symbol);
        } else fragment.append(document.createTextNode(match[0]));
        cursor = match.index + match[0].length;
      }
      fragment.append(document.createTextNode(source.slice(cursor)));
      return fragment;
    };
    const detailRow = (label, value, copy = false) => {
      if (value === null || value === undefined || value === "") return null;
      const row = node("div", "card-fact");
      row.append(node("span", "card-fact-label", label));
      const content = node("div", "card-fact-content");
      const display = node("span", "card-fact-value");
      display.append(label === "Coût de mana" || label === "Texte de la carte" ? symbolText(value) : document.createTextNode(String(value)));
      content.append(display);
      if (copy) {
        const button = node("button", "card-fact-copy");
        button.type = "button"; button.append(cardActionIcon("copy"));
        button.setAttribute("aria-label", `Copier ${label.toLowerCase()}`);
        button.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(String(value)); showCopyNotice("Copié dans le presse-papiers"); }
          catch { showCopyNotice("Copie impossible"); }
        });
        content.append(button);
      }
      row.append(content); return row;
    };
    const renderDetails = () => {
      if (association && !card.proxy_source_id) { details.replaceChildren(association); return; }
      const french = card.translations?.find(item => item.lang === "fr");
      const english = card.translations?.find(item => item.lang === "en");
      const fallback = english || card;
      const raw = record => { try { return JSON.parse(record?.raw_json || "{}"); } catch { return {}; } };
      const frData = raw(french), enData = raw(fallback);
      const frFace = frData.card_faces?.[selectedFace] || frData;
      const enFace = enData.card_faces?.[selectedFace] || enData;
      const frenchAvailable = Boolean(french && (frFace.printed_name || frFace.printed_type_line || frFace.printed_text || (!frData.card_faces && (french.printed_name || french.printed_type_line || french.printed_text))));
      if (!frenchAvailable) detailLanguage = "en";
      const localized = detailLanguage === "fr" && frenchAvailable;
      const face = localized ? frFace : enFace;
      const source = localized ? french : fallback;
      const name = localized ? (frFace.printed_name || (!frData.card_faces && french.printed_name) || enFace.name || fallback.name) : (enFace.name || fallback.name);
      const type = localized ? (frFace.printed_type_line || (!frData.card_faces && french.printed_type_line) || enFace.type_line || fallback.type_line) : (enFace.type_line || fallback.type_line);
      const rules = localized ? (frFace.printed_text || (!frData.card_faces && french.printed_text) || enFace.oracle_text || fallback.oracle_text) : (enFace.oracle_text || fallback.oracle_text);
      const mana = face.mana_cost || source.mana_cost || enFace.mana_cost || fallback.mana_cost;
      const flavor = face.flavor_text || (localized ? enFace.flavor_text : null);
      const artist = face.artist || enFace.artist || raw(card).artist;
      const title = node("div", "card-facts-heading");
      const languages = node("div", "card-facts-languages");
      for (const language of ["fr", "en"]) {
        const button = node("button", "card-facts-language", language.toUpperCase());
        button.type = "button";
        button.disabled = language === "fr" && !frenchAvailable;
        button.setAttribute("aria-pressed", String(detailLanguage === language));
        button.setAttribute("aria-label", language === "fr" ? "Afficher en français" : "Afficher en anglais");
        button.addEventListener("click", () => { detailLanguage = language; renderDetails(); });
        languages.append(button);
      }
      title.append(node("span", "card-facts-kicker", "FICHE DE CARTE"), languages);
      const rows = [detailRow("Nom", name, true), detailRow("Coût de mana", mana), detailRow("Type", type), detailRow("Texte de la carte", rules || "Aucun texte de règles", Boolean(rules))];
      if (association && (enData.card_faces?.length || frData.card_faces?.length) > 1) {
        const faces = enData.card_faces || frData.card_faces;
        const switcher = node("div", "face-switcher");
        faces.forEach((entry, index) => {
          const button = node("button", "secondary-button", entry.name || `Face ${index + 1}`);
          button.type = "button";
          button.setAttribute("aria-pressed", String(index === selectedFace));
          button.addEventListener("click", () => { selectedFace = index; renderDetails(); });
          switcher.append(button);
        });
        rows.unshift(switcher);
      }
      if (flavor) {
        const flavorRow = detailRow("Texte d’ambiance", flavor);
        flavorRow.classList.add("card-fact-flavor");
        rows.push(flavorRow);
      }
      const power = face.power ?? enFace.power, toughness = face.toughness ?? enFace.toughness;
      rows.push(detailRow("Loyauté", face.loyalty ?? enFace.loyalty), detailRow("Défense", face.defense ?? enFace.defense));
      const footer = node("div", "card-facts-footer");
      if (artist) footer.append(detailRow("Illustration", artist, true));
      if (power != null && toughness != null) {
        const combat = detailRow("Force / endurance", `${power} / ${toughness}`);
        combat.classList.add("card-fact-combat"); footer.append(combat);
      }
      details.replaceChildren(title, ...rows.filter(Boolean), footer, copyNotice);
      if (association) details.prepend(association);
    };
    const buildImageSearch = ready => {
      const action = node("button", "secondary-button card-image-search"); action.type = "button"; action.append(cardActionIcon("search"));
      action.setAttribute("aria-label", ready ? "Rechercher une meilleure image" : "Rechercher une image");
      action.addEventListener("click", async () => {
        const demoThisCard = Boolean(imageDemoMode && imageDemoCandidate && card.provider_card_id === imageDemoCardId);
        if (ready && !demoThisCard && !resumedPreview && !window.confirm("Voulez-vous rechercher une meilleure image ?")) return;
        action.disabled = true; controls.classList.add("image-search-pending");
        const loading = node("div", "image-search-loading");
        loading.append(node("div", "card-loading-spinner"));
        imageWrap.replaceChildren(loading);
        let downloadedOtherFaces = false;
        let previewTaskId = null;
        try {
          let previewTask;
          if (!demoThisCard) {
          if (resumedPreview) {
            previewTask = resumedPreview;
            previewTaskId = resumedPreview.task_id;
          } else {
          const otherMissingFaces = slots.filter(x => x.face_index !== selectedFace && x.status !== "ready");
          if (otherMissingFaces.length) {
            for (const face of otherMissingFaces)
              await postJSON("/api/v2/download", { provider_card_id: card.provider_card_id, face_index: face.face_index });
            downloadedOtherFaces = true;
            refreshCurrentCollection();
          }
          const started = await postJSON("/api/v2/image/preview", { provider_card_id: card.provider_card_id, face_index: selectedFace });
          previewTaskId = started.task_id;
          if (!previewTaskId) throw new Error("La recherche n’a pas retourné de tâche.");
          const cancelPreview = node("button", "secondary-button image-search-cancel", "Annuler");
          cancelPreview.type = "button";
          cancelPreview.addEventListener("click", async () => {
            cancelPreview.disabled = true;
            try { await postJSON("/api/v2/image/task/cancel", { task_id: previewTaskId }); }
            catch (error) { cancelPreview.disabled = false; loading.append(node("p", "error-state", error.message)); }
          });
          loading.append(cancelPreview);
          imageTaskHandlers.set(previewTaskId, { open: () => openImagePreviewTask(previewTaskId) });
          void refreshNotifications();
          do {
            await new Promise(resolve => setTimeout(resolve, 900));
            previewTask = await getJSON("/api/v2/image/task", { task_id: previewTaskId });
          } while (["queued", "running", "cancelling"].includes(previewTask.status));
          }
          } else previewTask = { result: imageDemoCandidate };
          if (previewTask.status === "cancelled") throw new Error("Recherche annulée.");
          if (previewTask.status === "error") throw new Error(previewTask.detail || "Recherche impossible.");
          const candidate = previewTask.result;
          if (candidate.status !== "candidate") throw new Error(candidate.detail || "Aucune candidate trouvée.");
          if (dialog.open && request === state.cardRequest) await renderFace();
          const comparison = node("section", "image-comparison");
          comparison.append(node("h2", "comparison-title", "Comparer les images"));
          const comparePanel = node("div", "comparison-columns comparison-current-panel");
          const currentImageStage = node("div", "comparison-column-stage");
          const proposedImageStage = node("div", "comparison-column-stage");
          const currentSlot = slots.find(item => item.face_index === selectedFace);
          if (currentSlot?.status === "ready" && currentSlot.relative_path) {
            const image = node("img", "comparison-preview"); image.src = `/assets?${params({path: currentSlot.relative_path})}`; image.alt = "Image actuelle"; currentImageStage.append(image);
          } else currentImageStage.append(node("p", "muted", "Aucune image actuelle"));
          for (const [title, stage] of [["Image actuelle", currentImageStage], ["Image proposée", proposedImageStage]]) {
            const column = node("section", "comparison-column"); column.append(node("h3", "", title), stage); comparePanel.append(column);
          }
          const columns = node("div", "comparison-columns");
          const onlineTextPanel = node("section", "ai-french-text-panel"); onlineTextPanel.hidden = true;
          const selectionPane = node("aside", "comparison-selection-pane");
          const previewPane = node("div", "comparison-preview-pane");
          const selectionPreview = node("div", "ai-selection-preview");
          const thumbs = node("div", "comparison-thumbnails");
          const choices = node("div", "comparison-choices");
          let selected = null;
          const options = candidate.candidates;
          const showPreview = (url, alt, host) => { const preview = node("img", "comparison-preview"); preview.src = url; preview.alt = alt; host.replaceChildren(preview); };
          const addThumb = (url, label, index, dimensions = "") => {
            const button = node("button", "comparison-option"); button.type = "button";
            if (wizardStep === "text") {
              const isSelected = String(index) === textSelect.value;
              button.classList.toggle("selected", isSelected);
              button.setAttribute("aria-pressed", String(isSelected));
            }
            const thumb = node("img", "comparison-thumb"); thumb.src = url; thumb.alt = label;
            button.append(thumb, node("span", "comparison-caption", dimensions || label));
            button.addEventListener("click", () => {
              const reference=options.find(value=>value.index===index);
              if (wizardStep === "compare") selected = index;
              thumbs.querySelectorAll(".comparison-option").forEach(x => x.classList.remove("selected"));
              button.classList.add("selected"); if (wizardStep === "compare") chooseNew.disabled = index === null || reference?.visual_only === true;
              if (wizardStep === "compare") showPreview(url, label, proposedImageStage);
              if (wizardStep === "visual" && reference) { visualSelect.value=String(index); alignReferenceOptions(); }
              if (wizardStep === "text" && reference && (reference.language === "fr" || reference.text_reference_only || reference.local_original_reference)) { textMode = "image"; textSelect.value=String(index); alignReferenceOptions(); }
              if (wizardStep === "visual" || wizardStep === "text") showPreview(url, label, selectionPreview);
              if (wizardStep !== "compare") { updateReferenceStages(); nextStep.disabled = !visualSelect.value; }
            });
            thumbs.append(button);
          };
          const renderThumbnails = language => {
            thumbs.replaceChildren();
            if (wizardStep === "text") {
              const noReference = node("button", "comparison-option comparison-no-reference");
              noReference.type = "button";
              noReference.append(node("span", "comparison-caption", "Aucune image de référence"));
              noReference.classList.toggle("selected", !textSelect.value);
              noReference.setAttribute("aria-pressed", String(!textSelect.value));
              noReference.addEventListener("click", () => {
                textSelect.value = "";
                textMode = "online";
                selectionPreview.replaceChildren();
                alignReferenceOptions();
                thumbs.querySelector(".comparison-no-reference").focus();
              });
              thumbs.append(noReference);
            }
            const visible = options.filter(value => (wizardStep !== "compare" || !value.visual_only && !value.text_reference_only)
              && (wizardStep === "text" ? value.language === "fr" || value.text_reference_only || value.local_original_reference : wizardStep === "visual" ? !value.text_reference_only || value.local_original_reference : !language || value.language === language));
            for (const item of visible)
              addThumb(item.preview_url, `Image ${item.language.toUpperCase()}${item.local_original_reference ? " déjà intégrée" : item.text_reference_only ? " même nom" : ""}`, item.index,
                `${item.language.toUpperCase()}${item.local_original_reference ? " · originale intégrée" : item.text_reference_only ? " · même nom · autre édition" : ""} · ${item.width} × ${item.height} px`);
            if (!visible.length && wizardStep !== "text") thumbs.append(node("p", "muted", wizardStep === "compare" ? "Aucune autre image à comparer." : "Aucune image disponible pour ce choix."));
          };
          const chooseOld = node("button", "secondary-button", "Annuler"); chooseOld.type = "button";
          const chooseNew = node("button", "primary-button", "Valider l’image choisie"); chooseNew.type = "button"; chooseNew.disabled = true;
          const choiceDialog = $("image-choice-dialog");
          const translationDialog = $("ai-translation-dialog");
          const translationPrompt = $("ai-translation-prompt");
          const translationCancel = $("ai-translation-cancel");
          const translationSend = $("ai-translation-send");
          const choose = async choice => { if (demoThisCard) { choiceDialog.close(); return; } chooseOld.disabled = chooseNew.disabled = true; try { await postJSON("/api/v2/image/candidate/choose", { candidate_id: candidate.candidate_id, choice, index: selected ?? 0 }); choiceDialog.close(); if (previewTaskId) { imageTaskHandlers.delete(previewTaskId); choiceDialog.removeAttribute("data-preview-task-id"); void refreshNotifications(); } if (choice === "new" || downloadedOtherFaces) { refreshCurrentCollection(); await openCard(card.provider_card_id, context, selectedFace); } } catch (error) { choices.append(node("p", "error-state", error.message)); chooseOld.disabled = false; chooseNew.disabled = selected === null; } };
          chooseOld.addEventListener("click", () => choose("old")); chooseNew.addEventListener("click", () => choose("new")); choices.append(chooseOld, chooseNew);
          const visualStage = node("div", "comparison-column-stage visual-original-stage");
          const textStage = node("div", "comparison-column-stage text-original-stage");
          const aiStage = node("div", "comparison-column-stage ai-result-stage");
          const makeColumn = (title, stage) => { const column = node("section", "comparison-column"); column.append(node("h3", "", title), stage); return column; };
          columns.append(makeColumn("Référence visuelle", visualStage), makeColumn("Référence française", textStage), makeColumn("Version IA", aiStage));
          aiStage.append(node("p", "muted", "L’illustration apparaîtra ici."));
          const createVersionButton = node("button", "primary-button ai-create-version", "Créer une version IA"); createVersionButton.type = "button";
          const stepDescription = node("p", "muted ai-step-description");
          const aiProgress = node("div", "ai-generation-progress");
          const aiProgressMessage = node("p", "muted", "Codex génère la carte française. Cette étape peut prendre quelques minutes.");
          const leaveWaiting = node("button", "secondary-button", "Reprendre la consultation en attendant la notification"); leaveWaiting.type = "button";
          const cancelWaiting = node("button", "secondary-button", "Annuler"); cancelWaiting.type = "button";
          const waitingActions = node("div", "ai-waiting-actions");
          waitingActions.append(cancelWaiting, leaveWaiting);
          aiProgress.append(node("div", "card-loading-spinner"), aiProgressMessage, waitingActions);
          const stepActions = node("div", "comparison-choices ai-step-actions");
          const backStep = node("button", "secondary-button", "Retour"); backStep.type = "button";
          const nextStep = node("button", "primary-button", "Valider et continuer"); nextStep.type = "button";
          const aiControls = node("div", "ai-generation-controls");
          aiControls.hidden=true;
          const visualSelect = node("select", ""); visualSelect.setAttribute("aria-label", "Référence visuelle");
          const textSelect = node("select", ""); textSelect.setAttribute("aria-label", "Référence de texte");
          const fillReferenceSelect = (select, label, language) => { const first = node("option", "", label); first.value = ""; select.append(first); for (const item of options.filter(value => language === "fr" ? value.language === "fr" || value.text_reference_only || value.local_original_reference : !value.text_reference_only || value.local_original_reference)) { const origin = item.local_original_reference ? " · image originale intégrée" : item.text_reference_only ? " · même nom, autre édition" : ""; const option = node("option", "", `${item.language.toUpperCase()}${origin} · ${item.width} × ${item.height} px`); option.value = String(item.index); select.append(option); } };
          fillReferenceSelect(visualSelect, "Choisir une référence visuelle", null); fillReferenceSelect(textSelect, "Choisir une image française", "fr");
          const correction = node("textarea", "ai-correction-prompt"); correction.rows = 2; correction.maxLength = 4000; correction.placeholder = "Consigne supplémentaire avant génération (facultatif)"; correction.setAttribute("aria-label", "Consigne supplémentaire pour la génération IA");
          stepActions.append(backStep, nextStep);
          const aiStatus = node("p", "muted ai-status", "Choisissez une image visuelle et une référence française de la même impression ou d’une carte au même nom.");
          const aiActions = node("div", "comparison-choices ai-actions");
          aiActions.hidden=true;
          const reviewFooter = node("div", "review-footer");
          const reviewCorrection = node("div", "review-correction"); reviewCorrection.hidden = true;
          const retrySubmit = node("button", "primary-button", "Générer avec cette correction"); retrySubmit.type = "button";
          const retryCancel = node("button", "secondary-button", "Annuler la correction"); retryCancel.type = "button";
          reviewCorrection.append(correction, retrySubmit, retryCancel);
          const errorPane = node("div", "ai-generation-error"); errorPane.hidden = true;
          const errorTitle = node("h3", "", "Génération impossible");
          const errorMessage = node("p", "error-state");
          const acknowledgeError = node("button", "primary-button", "Annuler la génération"); acknowledgeError.type = "button";
          errorPane.append(errorTitle, errorMessage, acknowledgeError);
          const acceptAI = node("button", "primary-button", "Valider et remplacer l’image"); acceptAI.type = "button"; acceptAI.disabled = true;
          const cancelAI = node("button", "secondary-button", "Annuler"); cancelAI.type = "button";
          const retryAI = node("button", "secondary-button", "Réessayer"); retryAI.type = "button"; retryAI.disabled = true;
          let aiId = null;
          let aiTaskId = null;
          let textMode = "image";
          const alignReferenceOptions = () => {
            const visual = options.find(item => String(item.index) === visualSelect.value);
            for (const option of textSelect.options) {
              if (!option.value) continue;
              const text = options.find(item => String(item.index) === option.value);
            option.disabled = !visual || !text || !(text.printing_id === visual.printing_id && text.face_index === visual.face_index || text.text_reference_only || text.local_original_reference);
            }
            if (textSelect.selectedOptions[0]?.disabled) textSelect.value = "";
            updateReferenceStages();
            nextStep.disabled = wizardStep === "visual" ? !visualSelect.value : !visual;
            if (wizardStep === "text") renderThumbnails("fr");
          };
          const updateReferenceStages = () => {
            const visual=options.find(item=>String(item.index)===visualSelect.value),text=options.find(item=>String(item.index)===textSelect.value);
            if(visual)showPreview(visual.preview_url,`Référence visuelle ${visual.language.toUpperCase()}`,visualStage);else visualStage.replaceChildren(node("p","muted","Choisir une référence visuelle"));
            if(textMode === "online") textStage.replaceChildren(node("p","muted","Le texte français exact sera recherché et sa source sera affichée pour vérification."));
            else if(textMode === "translate") textStage.replaceChildren(node("p","muted","Aucune traduction officielle disponible : le modèle traduira le texte visible de cette référence."));
            else if(text)showPreview(text.preview_url,text.local_original_reference ? "Image originale intégrée · vérifiez le texte" : text.text_reference_only ? "Image française du même nom" : "Référence textuelle française",textStage);else textStage.replaceChildren(node("p","muted","Choisissez une image française ou utilisez les champs officiels de la base locale."));
          };
          visualSelect.addEventListener("change", alignReferenceOptions);
          textSelect.addEventListener("change",alignReferenceOptions);
          const cancelSetup=node("button","secondary-button","Annuler");cancelSetup.type="button";
          let wizardStep = "compare";
          const setWizardStep = step => {
            wizardStep = step;
            comparison.dataset.step = step;
            const choosing = step === "visual" || step === "text";
            thumbs.hidden = !["compare", "visual", "text"].includes(step);
            choices.hidden = step !== "compare";
            comparePanel.hidden = step !== "compare";
            selectionPreview.hidden = !choosing;
            createVersionButton.hidden = step !== "compare";
            stepDescription.hidden = step !== "visual";
            aiProgress.hidden = step !== "generating";
            stepActions.hidden = !choosing;
            columns.hidden = step !== "review";
            onlineTextPanel.hidden = step !== "review" || textMode !== "online";
            aiControls.hidden = step !== "review";
            aiActions.hidden = step !== "review" || !aiId;
            selectionPane.hidden = step === "review" || step === "generating" || step === "error";
            reviewFooter.hidden = step !== "review";
            errorPane.hidden = step !== "error";
            if (step === "compare") {
              comparison.querySelector(".comparison-title").textContent = "Choisir une image existante";
              renderThumbnails();
            } else if (step === "visual") {
              comparison.querySelector(".comparison-title").textContent = "Étape 1 sur 2 · Référence visuelle";
              stepDescription.textContent = "Choisissez l’image dont vous voulez conserver le graphisme et la composition. Toutes les langues peuvent servir de référence.";
              renderThumbnails();
              const choice = options.find(item => String(item.index) === visualSelect.value);
              if (choice) showPreview(choice.preview_url, `Référence visuelle ${choice.language.toUpperCase()}`, selectionPreview);
              else selectionPreview.replaceChildren(node("p", "muted", "Sélectionnez une carte à gauche."));
            } else if (step === "text") {
              comparison.querySelector(".comparison-title").textContent = "Étape 2 sur 2 · Référence de texte";
              renderThumbnails("fr");
              const choice = options.find(item => String(item.index) === textSelect.value);
              if (choice) showPreview(choice.preview_url, "Référence textuelle française", selectionPreview);
              else selectionPreview.replaceChildren();
            } else if (step === "generating") {
              comparison.querySelector(".comparison-title").textContent = "Étape 3 sur 3 · Génération en cours";
            } else if (step === "error") {
              comparison.querySelector(".comparison-title").textContent = "La génération a échoué";
            } else {
              comparison.querySelector(".comparison-title").textContent = "Comparer la carte générée";
            }
            if (choosing) nextStep.disabled = !visualSelect.value;
            nextStep.textContent = step === "visual" ? "Continuer" : "Générer";
          };
          createVersionButton.addEventListener("click",()=>{ textMode = "image"; setWizardStep("visual"); });
          backStep.addEventListener("click",()=>{ textMode = "image"; setWizardStep(wizardStep === "text" ? "visual" : "compare"); });
          nextStep.addEventListener("click",()=>{
            if (wizardStep === "visual" && visualSelect.value) setWizardStep("text");
            else if (wizardStep === "text") { textMode = textSelect.value ? "image" : "online"; void runAIGeneration(); }
          });
          translationCancel.onclick = () => { translationDialog.close(); textMode = "image"; setWizardStep("text"); if (!choiceDialog.open) choiceDialog.showModal(); };
          translationDialog.oncancel = event => { event.preventDefault(); translationCancel.click(); };
          translationSend.onclick = () => {
            const prompt = translationPrompt.value.trim();
            translationDialog.close(); textMode = "translate"; correction.value = prompt; updateReferenceStages();
            if (!choiceDialog.open) choiceDialog.showModal();
            void runAIGeneration(prompt);
          };
          cancelSetup.addEventListener("click",()=>choiceDialog.close());
          leaveWaiting.addEventListener("click", () => { choiceDialog.close(); dialog.close(); });
          cancelWaiting.addEventListener("click", async () => {
            if (!aiTaskId) return;
            cancelWaiting.disabled = true;
            try { await cancelImageTask(aiTaskId); setText(aiProgressMessage, "Annulation demandée…"); }
            catch (error) { setText(aiProgressMessage, `Annulation impossible : ${error.message}`); cancelWaiting.disabled = false; }
          });
          const runAIGeneration = async (promptOverride = null) => {
            const visual=options.find(item=>String(item.index)===visualSelect.value),text=options.find(item=>String(item.index)===textSelect.value);
            if (!visual || textMode === "image" && (!text || !(text.language === "fr" || text.text_reference_only || text.local_original_reference) || !(visual.printing_id===text.printing_id&&visual.face_index===text.face_index || text.text_reference_only || text.local_original_reference))) { setText(aiStatus, "Choisissez une référence visuelle, puis une image française, l’original intégré ou une image du même nom."); return; }
            aiTaskId = null;
            cancelWaiting.disabled = false;
            setWizardStep("generating"); retryAI.disabled = true; acceptAI.disabled = true; aiStage.replaceChildren(node("div", "card-loading-spinner")); setText(aiStatus, "Génération de la carte… cela peut prendre quelques minutes.");
            if (textMode === "online") textStage.replaceChildren(node("p", "muted", "Recherche du texte français vérifié…"));
            try {
              const requestedCorrection = promptOverride === null ? correction.value.trim() : promptOverride;
              const result = await postJSON("/api/v2/image/ai/generate", { candidate_id: candidate.candidate_id, visual_index: Number(visualSelect.value), ...(textMode === "image" ? { text_index: Number(textSelect.value) } : { text_mode: textMode }), correction_prompt: requestedCorrection, ...(aiId ? { previous_id: aiId } : {}) });
              if (result.status === "translation_unavailable") {
                setWizardStep("text");
                if (choiceDialog.open) choiceDialog.close();
                translationPrompt.value = "";
                translationDialog.showModal();
                return;
              }
              if (!result.task_id) throw new Error("Le serveur n’a pas retourné l’identifiant de la tâche.");
              const taskId = result.task_id;
              aiTaskId = taskId;
              const openTask = async () => {
                if (choiceDialog.open) choiceDialog.close();
                await openCard(card.provider_card_id, context, selectedFace);
                $("image-choice-content").replaceChildren(comparison);
                choiceDialog.showModal();
                document.getElementById("notification-panel").hidden = true;
              };
              imageTaskHandlers.set(aiTaskId, { open: openTask });
              setText(aiProgressMessage, "Génération en cours. Vous pouvez quitter cette page; résultat dans notifications.");
              cancelWaiting.hidden = false; leaveWaiting.hidden = false;
              void refreshNotifications();
              const poll = async () => {
                if (aiTaskId !== taskId) return;
                try {
                  const task = await getJSON("/api/v2/image/task", { task_id: taskId });
                  if (aiTaskId !== taskId) return;
                  if (["queued", "running", "cancelling"].includes(task.status)) { setText(aiProgressMessage, task.status === "cancelling" ? "Annulation demandée…" : task.detail || "Génération en cours. Vous pouvez quitter cette page; résultat dans notifications."); void refreshNotifications(); setTimeout(poll, 1200); return; }
                  if (task.status === "needs_review" || task.status === "ready") {
                    const value = task.result || task;
                    aiId = value.id || value.candidate_id;
                    if (!aiId) throw new Error("La tâche est terminée, mais sa candidate est introuvable.");
                    showPreview(value.preview_url || `/api/v2/image/ai/candidate?id=${encodeURIComponent(aiId)}`, "Illustration générée par IA", aiStage);
                    if (textMode === "online" || textMode === "translate") {
                      columns.classList.add("ai-online-review"); columns.children[0].querySelector("h3").textContent = "Image originale";
                      columns.children[1].hidden = true;
                      if (textMode === "online") { renderVerifiedFrenchText(onlineTextPanel, value.french_text || task.french_text, value.text_provenance || task.text_provenance); }
                    }
                    setText(aiStatus, "Comparez le texte français et le graphisme avant de valider."); acceptAI.disabled = false; retryAI.disabled = false; setWizardStep("review");
                    cancelWaiting.hidden = true; void refreshNotifications(); return;
                  }
                  if (task.status === "cancelled") {
                    imageTaskHandlers.delete(aiTaskId);
                    aiTaskId = null;
                    setText(errorMessage, "La génération a été annulée.");
                    setText(acknowledgeError, "Revenir aux images");
                    setWizardStep("error");
                    cancelWaiting.hidden = true;
                    void refreshNotifications();
                    return;
                  }
                  throw new Error(task.detail || task.error || "La génération n’a pas abouti.");
                } catch (error) {
                  setText(errorMessage, error.message || "La génération n’a pas abouti."); setWizardStep("error"); cancelWaiting.hidden = true; void refreshNotifications();
                }
              };
              void poll();
            } catch (error) { setText(errorMessage, error.message || "La génération n’a pas abouti."); setWizardStep("error"); }
            finally { alignReferenceOptions(); }
          };
          retryAI.addEventListener("click", () => { reviewCorrection.hidden = false; retryAI.disabled = true; correction.focus(); });
          retryCancel.addEventListener("click", () => { reviewCorrection.hidden = true; retryAI.disabled = false; });
          retrySubmit.addEventListener("click", () => { reviewCorrection.hidden = true; void runAIGeneration(); });
          acknowledgeError.addEventListener("click", async () => {
            acknowledgeError.disabled = true;
            try {
              if (aiTaskId) await cancelImageTask(aiTaskId);
              aiTaskId = null;
              aiId = null;
              textMode = "image";
              correction.value = "";
              reviewCorrection.hidden = true;
              setWizardStep("compare");
            } catch (error) {
              setText(errorMessage, `Annulation impossible : ${error.message}`);
            } finally {
              acknowledgeError.disabled = false;
            }
          });
          const finishAI = async choice => { if (demoThisCard) { choiceDialog.close(); return; } acceptAI.disabled = cancelAI.disabled = retryAI.disabled = true; try { await postJSON("/api/v2/image/ai/choose", { id: aiId, choice }); choiceDialog.close(); if (aiTaskId) imageTaskHandlers.delete(aiTaskId); if (choice === "new" && previewTaskId) { imageTaskHandlers.delete(previewTaskId); choiceDialog.removeAttribute("data-preview-task-id"); } void refreshNotifications(); if (choice === "new") { refreshCurrentCollection(); await openCard(card.provider_card_id, context, selectedFace); } } catch (error) { setText(aiStatus, `Choix impossible : ${error.message}`); acceptAI.disabled = !aiId; cancelAI.disabled = false; retryAI.disabled = !aiId; } };
          acceptAI.addEventListener("click", () => { if (aiId) void finishAI("new"); });
          cancelAI.textContent = "Retour à la sélection des images";
          cancelAI.addEventListener("click", async () => { if (aiId && !demoThisCard) { try { await postJSON("/api/v2/image/ai/choose", { id: aiId, choice: "old" }); } catch {} } aiId = null; reviewCorrection.hidden = true; setWizardStep("compare"); });
          aiActions.append(acceptAI, retryAI, cancelAI);
          aiControls.append(visualSelect, textSelect, cancelSetup, aiStatus);
          stepActions.prepend(correction);
          reviewFooter.append(aiActions, reviewCorrection);
          selectionPane.append(thumbs, choices, createVersionButton, stepActions, aiControls);
          previewPane.append(comparePanel, selectionPreview, columns, onlineTextPanel, aiProgress, errorPane);
          comparison.append(stepDescription, selectionPane, previewPane, reviewFooter);
          if (demoThisCard) {
            comparison.prepend(node("p", "eyebrow", "Simulation · aucune image ne sera remplacée"));
            createVersionButton.hidden = true;
          }
          createVersionButton.disabled = options.length === 0;
          setText(aiStatus, "Choisissez une référence visuelle, puis une image française ou les champs officiels de la base locale.");
          alignReferenceOptions(); setWizardStep("compare");
          if (demoThisCard && imageDemoMode === "existing") createVersionButton.hidden = true;
          const firstPublishable = options.find(item => !item.visual_only);
          if (firstPublishable) { selected = firstPublishable.index; thumbs.querySelector(`.comparison-option`)?.classList.add("selected"); showPreview(firstPublishable.preview_url, "Image proposée", proposedImageStage); chooseNew.disabled = false; }
          if (demoThisCard && imageDemoMode === "ai") {
            visualSelect.value = String(options[0].index);
            textSelect.value = String(options[1].index);
            updateReferenceStages();
            showPreview("/api/v2/demo-ai-image", "Exemple local de version IA", aiStage);
            aiId = "simulation";
            acceptAI.disabled = false;
            retryAI.disabled = true;
            setWizardStep("review");
          }
          const openComparison = () => {
            $("image-choice-content").replaceChildren(comparison);
            choiceDialog.dataset.previewTaskId = previewTaskId || "";
            if (!choiceDialog.open) choiceDialog.showModal();
          };
          if (previewTaskId) imageTaskHandlers.set(previewTaskId, { open: openComparison });
          const stillWaitingHere = dialog.open && request === state.cardRequest;
          const otherModalOpen = [...document.querySelectorAll("dialog[open]")].some(item => item !== dialog);
          if (stillWaitingHere && !otherModalOpen) openComparison();
          void refreshNotifications();
        } catch (error) {
          if (dialog.open && request === state.cardRequest) {
            if (downloadedOtherFaces) { refreshCurrentCollection(); await openCard(card.provider_card_id, context, selectedFace); target.querySelector(".card-image-actions")?.append(node("p", "error-state", error.message)); }
            else { imageArea.replaceChildren(imageWrap, controls); await renderFace(); controls.append(node("p", "error-state", error.message)); }
          }
          if (previewTaskId) { imageTaskHandlers.delete(previewTaskId); void refreshNotifications(); }
        } finally { controls.classList.remove("image-search-pending"); loading.querySelector(".image-search-cancel")?.remove(); }
      });
      return action;
    };
    let faceRequest = 0;
    const renderFace = async () => {
      const faceLoad = ++faceRequest;
      const slot = slots.find(x => x.face_index === selectedFace);
      renderDetails();
      imageWrap.replaceChildren(node("div", "card-loading-spinner"));
      let visual;
      const ready = Boolean(card.image_url || slot?.status === "ready");
      if (ready) {
        const img = node("img", "card-viewer-art");
        img.alt = card.printed_name || card.name || "Image de carte";
        img.loading = "eager";
        img.src = currentCardImageUrl(card) || `/assets?${params({ path: slot.relative_path })}`;
        try {
          await img.decode();
          if (img.naturalWidth && img.naturalHeight)
            img.style.setProperty("--viewer-image-ratio", String(img.naturalWidth / img.naturalHeight));
          visual = img;
        }
        catch { visual = node("div", "card-viewer-missing", "Image indisponible"); }
      } else visual = node("div", "card-viewer-missing", "Image absente");
      if (faceLoad !== faceRequest || request !== state.cardRequest || !dialog.open) return;
      imageWrap.replaceChildren(visual);
      controls.replaceChildren();
      if (card.proxy && !card.proxy_source_id) return;
      if (!card.proxy) controls.append(buildImageSearch(ready));
      {
        const fileInput = node("input", "image-import-input");
        fileInput.type = "file"; fileInput.accept = "image/*"; fileInput.hidden = true;
        const importButton = node("button", "secondary-button card-image-icon image-import-button"); importButton.append(cardActionIcon("upload"));
        importButton.type = "button";
        importButton.setAttribute("aria-label", card.proxy ? "Changer l’image du proxy" : "Importer une image locale");
        importButton.addEventListener("click", () => fileInput.click());
        fileInput.addEventListener("change", async () => {
          const file = fileInput.files?.[0]; fileInput.value = "";
          if (!file) return;
          if (file.size > 20 * 1024 * 1024) { controls.append(node("p", "error-state", "Le fichier dépasse la limite de 20 Mio.")); return; }
          importButton.disabled = true;
          try {
            const query = params({ provider_card_id: card.provider_card_id, face_index: card.proxy ? 0 : selectedFace });
            const response = await fetch(`/api/v2/image/import?${query}`, { method: "POST", headers: { Accept: "application/json", "Content-Type": file.type || "application/octet-stream" }, body: file });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
            if (card.proxy) proxyImageRevisions.set(card.provider_card_id, String(Date.now()));
            refreshCurrentCollection(card.proxy);
            if (dialog.open && request === state.cardRequest) await openCard(card.provider_card_id, context, selectedFace);
          } catch (error) { controls.append(node("p", "error-state", `Import impossible : ${error.message}`)); importButton.disabled = false; }
        });
        controls.append(importButton, fileInput);
      }
      {
        const exportButton = node("button", "secondary-button card-image-icon image-export-button"); exportButton.append(cardActionIcon("download"));
        exportButton.type = "button";
        exportButton.setAttribute("aria-label", "Télécharger l’image sur le Bureau");
        exportButton.disabled = !ready;
        exportButton.addEventListener("click", async () => {
          exportButton.disabled = true;
          try {
            const result = await postJSON("/api/v2/image/export", { provider_card_id: card.provider_card_id, face_index: card.proxy ? 0 : selectedFace });
            if (!exportButton.isConnected) return;
            const status = node("p", "card-export-status", `Image enregistrée : ${result.path}`);
            status.setAttribute("role", "status");
            imageWrap.querySelector(".card-export-status")?.remove();
            imageWrap.append(status);
            setTimeout(() => status.remove(), 4000);
          } catch (error) {
            controls.append(node("p", "error-state", `Export impossible : ${error.message}`));
          } finally { exportButton.disabled = false; }
        });
        controls.append(exportButton);
      }
      const addPersonal = node("button", "secondary-button card-image-icon personal-add-button"); addPersonal.append(cardActionIcon("addDeck"));
      addPersonal.type = "button"; addPersonal.setAttribute("aria-label", "Ajouter cette carte à un deck personnel");
      addPersonal.addEventListener("click", () => void choosePersonalDeck(card));
      controls.append(addPersonal);
      const english = card.translations?.find(record => record.lang === "en");
      const typeLine = english?.type_line || card.type_line || "";
      const typeNames = { creature: "Creature", land: "Land", instant: "Instant", sorcery: "Sorcery", artifact: "Artifact", enchantment: "Enchantment", planeswalker: "Planeswalker", battle: "Battle", kindred: "Kindred|Tribal", plane: "Plane", phenomenon: "Phenomenon", scheme: "Scheme", conspiracy: "Conspiracy", vanguard: "Vanguard", dungeon: "Dungeon" };
      const selectedTypes = Object.entries(typeNames).filter(([, name]) => new RegExp(`\\b(?:${name})\\b`, "i").test(typeLine)).map(([key]) => key);
      if (["token", "double_faced_token"].includes(card.layout)) selectedTypes.push("token");
      const occurrencesQuery = new URLSearchParams({ searched: "1", auto: "1", q: english?.name || card.name || "", colors: card.colors_key || "C", color_mode: "exact" });
      if (card.oracle_id) occurrencesQuery.set("oracle_id", card.oracle_id);
      else occurrencesQuery.set("printing_id", card.printing_id);
      if (selectedTypes.length) occurrencesQuery.set("card_type", selectedTypes.join("|"));
      const occurrences = node("a", "secondary-button card-image-icon card-occurrences-button"); occurrences.append(cardActionIcon("occurrences"));
      occurrences.href = `${location.href.split("#")[0]}#recherche?${occurrencesQuery}`;
      occurrences.target = "_blank"; occurrences.rel = "noopener noreferrer";
      occurrences.setAttribute("aria-label", "Voir toutes les occurrences de la carte");
      controls.append(occurrences);
      const usages = node("button", "secondary-button card-image-icon card-usages-button"); usages.append(cardActionIcon("usages"));
      usages.type = "button"; usages.setAttribute("aria-label", "Voir les éditions et decks utilisant cette carte");
      usages.addEventListener("click", () => void showCardUsages(card));
      controls.append(usages);
      if (slots.length > 1) {
        const nextFace = slots[(slots.findIndex(x => x.face_index === selectedFace) + 1) % slots.length];
        const flipButton = node("button", "secondary-button card-image-icon card-face-flip"); flipButton.append(cardActionIcon("flip"));
        flipButton.type = "button";
        flipButton.setAttribute("aria-label", `Afficher ${nextFace.face_index === 0 ? "le recto" : "le verso"}`);
        flipButton.addEventListener("click", () => { selectedFace = nextFace.face_index; void renderFace(); });
        controls.append(flipButton);
      }
    };
    await renderFace();
    if (request !== state.cardRequest || !dialog.open) return;
    imageArea.append(imageWrap, controls);
    const body = node("div", "card-viewer-body"); body.append(imageArea, details);
    shell.append(header, body); target.replaceChildren(shell); target.classList.remove("card-detail-loading");
    if (focusedDirection && document.activeElement === document.body) {
      const button = focusedDirection === "-1" ? previousCard : nextCard;
      (button.disabled ? $("close-dialog") : button).focus({ preventScroll: true });
    }
    if (resumedPreview) controls.querySelector(".card-image-search")?.click();
  } catch (error) { if (request === state.cardRequest) { showError(target,error.message); target.classList.remove("card-detail-loading"); } }
  finally { if (state.cardController === controller) state.cardController = null; }
}
async function openAIReviewTask(taskId, summary = {}) {
  const content = $("image-choice-content"), reviewDialog = $("image-choice-dialog");
  try {
    const task = await getJSON("/api/v2/image/task", { task_id: taskId });
    const result = task.result || {};
    const providerId = task.provider_card_id || summary.provider_card_id;
    const faceIndex = task.face_index ?? summary.face_index ?? 0;
    if (["queued", "running", "cancelling"].includes(task.status)) {
      const waiting = node("section", "image-comparison");
      const waitingMessage = node("p", "muted", task.status === "cancelling" ? "Annulation demandée…" : "Vous pouvez quitter cette page; résultat dans notifications.");
      waiting.append(node("h2", "comparison-title", "Génération en cours"), node("div", "card-loading-spinner"), waitingMessage);
      const actions = node("div", "comparison-choices ai-waiting-actions");
      const home = node("button", "secondary-button", "Reprendre la consultation"); home.type = "button"; home.addEventListener("click", () => { reviewDialog.close(); if ($("card-dialog").open) $("card-dialog").close(); });
      const cancel = node("button", "secondary-button", "Annuler"); cancel.type = "button"; cancel.addEventListener("click", async () => { cancel.disabled = true; try { await cancelImageTask(taskId); } catch (error) { setText(waitingMessage, `Annulation impossible : ${error.message}`); cancel.disabled = false; } });
      actions.append(cancel, home); waiting.append(actions); content.replaceChildren(waiting);
      if (!reviewDialog.open) reviewDialog.showModal(); $("notification-panel").hidden = true;
      const poll = async () => {
        try {
          const current = await getJSON("/api/v2/image/task", { task_id: taskId });
          if (["queued", "running", "cancelling"].includes(current.status)) { setText(waitingMessage, current.status === "cancelling" ? "Annulation demandée…" : "Vous pouvez quitter cette page; résultat dans notifications."); void refreshNotifications(); setTimeout(poll, 1200); return; }
          void refreshNotifications();
          if (reviewDialog.open) void openAIReviewTask(taskId, summary);
        } catch { setTimeout(poll, 2500); }
      };
      setTimeout(poll, 1200); return;
    }
    if (["error", "cancelled"].includes(task.status)) {
      const failed = node("section", "image-comparison");
      const failure = node("p", "error-state", task.detail || "La génération n’a pas abouti.");
      failed.append(node("h2", "comparison-title", task.status === "cancelled" ? "Génération annulée" : "Génération impossible"), failure);
      const close = node("button", "secondary-button", providerId ? "Revenir à la sélection d’images" : "Fermer");
      close.type = "button";
      close.addEventListener("click", async () => {
        close.disabled = true;
        try {
          if (task.status === "error") await cancelImageTask(taskId);
          reviewDialog.close();
          if (providerId) {
            await openCard(providerId, null, faceIndex);
            $("card-detail").querySelector(".card-image-search")?.click();
          }
        } catch (error) {
          setText(failure, `Annulation impossible : ${error.message}`);
          close.disabled = false;
        }
      });
      failed.append(close); content.replaceChildren(failed);
      if (!reviewDialog.open) reviewDialog.showModal(); $("notification-panel").hidden = true; return;
    }
    let currentAiId = result.id || result.candidate_id || summary.candidate_id;
    let currentTaskId = taskId;
    const sourceCandidateId = task.source_candidate_id || result.source_candidate_id;
    const visualIndex = task.visual_index ?? result.visual_index;
    const textIndex = task.text_index ?? result.text_index;
    if (!providerId || !currentAiId) throw new Error("Le contexte de cette candidate n’est plus disponible.");
    if (reviewDialog.open) reviewDialog.close();
    await openCard(providerId, null, faceIndex);
    const comparison = node("section", "image-comparison");
    comparison.dataset.step = "review";
    comparison.classList.add("resumed-ai-review");
    comparison.append(node("h2", "comparison-title", "Comparer la carte générée"));
    const previewPane = node("div", "comparison-preview-pane");
    const columns = node("div", "comparison-columns");
    const frenchTextPanel = node("section", "ai-french-text-panel"); frenchTextPanel.hidden = true;
    const addColumn = (title, imageUrl, alt) => {
      const column = node("section", "comparison-column"), stage = node("div", "comparison-column-stage");
      column.append(node("h3", "", title));
      if (imageUrl) { const image = node("img", "comparison-preview"); image.src = imageUrl; image.alt = alt; stage.append(image); }
      else stage.append(node("p", "muted", "Image absente"));
      column.append(stage); columns.append(column); return stage;
    };
    const visualUrl = result.visual_preview_url || task.visual_preview_url || summary.visual_preview_url || "";
    const textUrl = result.text_preview_url || task.text_preview_url || summary.text_preview_url || "";
    const textMode = result.text_mode || task.text_mode || "image";
    addColumn(textMode === "online" ? "Image originale" : "Référence visuelle", visualUrl, "Référence visuelle sélectionnée");
    if (textMode === "online" || textMode === "translate") {
      columns.classList.add("ai-online-review");
      if (textMode === "online") renderVerifiedFrenchText(frenchTextPanel, result.french_text || task.french_text, result.text_provenance || task.text_provenance);
    } else addColumn("Référence française", textUrl, "Référence textuelle française");
    const aiStage = addColumn("Version IA", result.preview_url || task.preview_url || `/api/v2/image/ai/candidate?id=${encodeURIComponent(currentAiId)}`, "Illustration générée par IA");
    const actions = node("div", "comparison-choices");
    const chooseOld = node("button", "secondary-button", "Conserver l’image actuelle"); chooseOld.type = "button";
    const chooseNew = node("button", "primary-button", "Utiliser l’image générée"); chooseNew.type = "button";
    const status = node("p", "muted review-status"); status.setAttribute("aria-live", "polite");
    const correction = node("textarea", "ai-correction-prompt"); correction.rows = 1; correction.maxLength = 4000; correction.placeholder = "Précision pour la correction (facultatif)"; correction.setAttribute("aria-label", "Précision pour la correction IA");
    correction.value = result.correction_prompt || task.correction_prompt || "";
    const retry = node("button", "secondary-button", "Générer une correction"); retry.type = "button";
    const retryPane = node("div", "review-correction"); retryPane.append(correction, retry);
    const canRetry = Boolean(sourceCandidateId && Number.isInteger(visualIndex) && (textMode !== "image" || Number.isInteger(textIndex)));
    retryPane.hidden = !canRetry;
    const reviewTitle = comparison.querySelector(".comparison-title");
    const loading = node("div", "ai-generation-progress");
    const loadingMessage = node("p", "muted", "Codex génère la carte française. Cette étape peut prendre quelques minutes.");
    const leaveWaiting = node("button", "secondary-button", "Reprendre la consultation en attendant la notification"); leaveWaiting.type = "button";
    const cancelWaiting = node("button", "secondary-button", "Annuler"); cancelWaiting.type = "button";
    const waitingActions = node("div", "ai-waiting-actions"); waitingActions.append(cancelWaiting, leaveWaiting);
    loading.append(node("div", "card-loading-spinner"), loadingMessage, waitingActions);
    leaveWaiting.addEventListener("click", () => { reviewDialog.close(); if ($("card-dialog").open) $("card-dialog").close(); });
    const showRetryLoading = () => {
      comparison.dataset.step = "generating";
      reviewTitle.textContent = "Étape 3 sur 3 · Génération en cours";
      comparison.replaceChildren(reviewTitle, loading);
    };
    const showRetryReview = () => {
      comparison.dataset.step = "review";
      reviewTitle.textContent = "Comparer la carte générée";
      comparison.replaceChildren(reviewTitle, previewPane, reviewFooter);
      retry.disabled = chooseOld.disabled = chooseNew.disabled = false;
    };
    retry.addEventListener("click", async () => {
      retry.disabled = chooseOld.disabled = chooseNew.disabled = true;
      cancelWaiting.disabled = true;
      setText(loadingMessage, "Codex génère la carte française. Cette étape peut prendre quelques minutes.");
      showRetryLoading();
      try {
        const started = await postJSON("/api/v2/image/ai/generate", { candidate_id: sourceCandidateId, visual_index: visualIndex, ...(textMode === "image" ? { text_index: textIndex } : { text_mode: textMode }), correction_prompt: correction.value.trim(), previous_id: currentAiId });
        if (!started.task_id) throw new Error("Le serveur n’a pas retourné l’identifiant de la tâche.");
        imageTaskHandlers.delete(currentTaskId); currentTaskId = started.task_id;
        cancelWaiting.disabled = false;
        const openRetry = async () => { if (reviewDialog.open) reviewDialog.close(); await openCard(providerId, null, faceIndex); content.replaceChildren(comparison); reviewDialog.showModal(); $("notification-panel").hidden = true; };
        imageTaskHandlers.set(currentTaskId, { open: openRetry });
        cancelWaiting.onclick = async () => { cancelWaiting.disabled = true; try { await postJSON("/api/v2/image/task/cancel", { task_id: currentTaskId }); setText(loadingMessage, "Annulation demandée…"); } catch (error) { setText(loadingMessage, `Annulation impossible : ${error.message}`); cancelWaiting.disabled = false; } };
        const pollRetry = async () => {
          try {
            const nextTask = await getJSON("/api/v2/image/task", { task_id: currentTaskId });
            if (["queued", "running", "cancelling"].includes(nextTask.status)) { setText(loadingMessage, nextTask.status === "cancelling" ? "Annulation demandée…" : nextTask.detail || "Génération en cours. Vous pouvez quitter cette page; résultat dans notifications."); void refreshNotifications(); setTimeout(pollRetry, 1200); return; }
            if (nextTask.status === "needs_review") {
              const nextResult = nextTask.result || {};
              currentAiId = nextResult.id || nextResult.candidate_id; if (!currentAiId) throw new Error("La candidate générée est introuvable.");
              const image = node("img", "comparison-preview"); image.src = nextResult.preview_url || `/api/v2/image/ai/candidate?id=${encodeURIComponent(currentAiId)}`; image.alt = "Nouvelle candidate générée par IA"; aiStage.replaceChildren(image);
              if (textMode === "online") renderVerifiedFrenchText(frenchTextPanel, nextResult.french_text || nextTask.french_text, nextResult.text_provenance || nextTask.text_provenance);
              showRetryReview(); setText(status, ""); void refreshNotifications(); return;
            }
            if (["cancelled", "canceled"].includes(nextTask.status)) { showRetryReview(); setText(status, "Génération annulée."); void refreshNotifications(); return; }
            throw new Error(nextTask.detail || "La génération n’a pas abouti.");
          } catch (error) { showRetryReview(); setText(status, `Génération impossible : ${error.message}`); void refreshNotifications(); }
        };
        void refreshNotifications(); void pollRetry();
      } catch (error) { showRetryReview(); setText(status, `Génération impossible : ${error.message}`); }
    });
    const choose = async choice => {
      chooseOld.disabled = chooseNew.disabled = true;
      try {
        await postJSON("/api/v2/image/ai/choose", { id: currentAiId, choice });
        reviewDialog.close();
        if (choice === "new") { refreshCurrentCollection(); await openCard(providerId, state.cardGallery, faceIndex); }
        imageTaskHandlers.delete(currentTaskId); void refreshNotifications();
      } catch (error) { setText(status, `Choix impossible : ${error.message}`); chooseOld.disabled = chooseNew.disabled = false; }
    };
    chooseOld.addEventListener("click", () => void choose("old")); chooseNew.addEventListener("click", () => void choose("new"));
    actions.classList.add("ai-actions");
    actions.append(chooseOld, chooseNew);
    previewPane.append(columns);
    const reviewFooter = node("div", "review-footer");
    reviewFooter.append(retryPane, actions, status);
    comparison.append(previewPane, reviewFooter); content.replaceChildren(comparison);
    if (!reviewDialog.open) reviewDialog.showModal();
    $("notification-panel").hidden = true;
  } catch (error) {
    content.replaceChildren(node("p", "error-state", `Impossible de rouvrir la vérification : ${error.message}`));
    if (!reviewDialog.open) reviewDialog.showModal();
    $("notification-panel").hidden = true;
  }
}
function refreshCurrentCollection(proxyImage = false) {
  invalidatePageCache();
  if (editionsPromise) void reloadEditions();
  if (state.view === "editions") void refreshEditionPage();
  else if (state.view === "recherche") {
    if (proxyImage && state.searchSnapshot) void loadSearchPage(state.searchSnapshot);
    else void refreshSearchImages();
  }
  else if (state.view === "decks") {
    void renderDeckPage();
    if (state.selectedDeck?.id) void openDeck(state.selectedDeck.id);
  } else if (state.view === "info") void loadHome();
  else if (state.view === "proxy" && proxyImage) void renderProxies({ quiet: true });
  else if (state.view === "perso" && proxyImage) {
    if (personalState.deck) renderPersonalEditor();
    else void personalState.refreshList?.(true, true);
  }
}
function renderVerifiedFrenchText(container, frenchText, provenance = null) {
  container.replaceChildren();
  if (!frenchText) { container.append(node("p", "muted", "Le texte français n’a pas été fourni par la recherche.")); return; }
  const block = node("div", "ai-french-text");
  block.append(node("strong", "", provenance?.verified ? "Texte français vérifié" : "Texte français proposé — à vérifier"));
  const labels = { name: "Nom", mana_cost: "Coût de mana", type_line: "Type", oracle_text: "Texte", power: "Force", toughness: "Endurance", loyalty: "Loyauté", defense: "Défense", flavor_text: "Texte d’ambiance" };
  const appendFace = (fields, faceLabel = "") => {
    if (!fields || typeof fields !== "object" || Array.isArray(fields)) return;
    if (faceLabel) block.append(node("h4", "", faceLabel));
    for (const [key, label] of Object.entries(labels)) {
      const value = fields[key];
      if (typeof value !== "string" || !value.trim()) continue;
      const row = node("div", "ai-french-text-field"); row.append(node("span", "", label), node("p", "", value)); block.append(row);
    }
  };
  if (typeof frenchText === "string") block.append(node("p", "", frenchText));
  else if (Array.isArray(frenchText.card_faces)) frenchText.card_faces.forEach((face, index) => appendFace(face, `Face ${index + 1}`));
  else appendFace(frenchText);
  if (block.querySelectorAll(".ai-french-text-field").length === 0 && !block.querySelector("p")) block.append(node("p", "muted", "Aucun champ de texte imprimé n’a été fourni."));
  const sourceLabel = provenance?.source === "scryfall_api" ? "Scryfall · API" : provenance?.source === "catalogue_fr" ? "Catalogue français vérifié" : provenance?.source === "catalogue_oracle_fr" ? "Traduction française d’une autre édition (même carte)" : provenance?.source;
  if (provenance?.source_url) {
    const link = node("a", "", sourceLabel || "Voir la source"); link.href = provenance.source_url; link.target = "_blank"; link.rel = "noopener noreferrer"; block.append(link);
  } else if (sourceLabel) block.append(node("span", "muted", sourceLabel));
  container.append(block);
}
async function refreshDeckResults() {
  if (!state.deckExecuted) return loadDeckFacets(true);
  const status = $("deck-batch-status");
  const filters = state.deckAppliedFilters;
  setText(status, "Vérification des decks de la recherche active…");
  try {
    const result = await postJSON("/api/v2/decks/recalculate", filters);
    if (state.view === "decks" && state.deckAppliedFilters === filters && !state.openDeckId) await renderDeckPage(true);
    setText(status, `${formatNumber(result.total || 0)} decks vérifiés.`);
  } catch (error) {
    setText(status, `Rafraîchissement impossible : ${error.message}`);
  }
}
function deckPageQuery() {
  return { ...state.deckAppliedFilters, limit: DECK_PAGE_SIZE, offset: state.deckPage * DECK_PAGE_SIZE };
}
function ensureDeckPage() {
  if (!state.deckExecuted) return;
  if (state.deckListKey !== params(deckPageQuery())) return renderDeckPage();
}
async function renderDeckPage(background = false) {
  if (state.openDeckId || !state.deckExecuted) return;
  if (state.view === "decks") syncRoute();
  const query = deckPageQuery();
  state.deckListKey = params(query);
  state.deckController?.abort();
  const controller = new AbortController();
  state.deckController = controller;
  const request = ++state.deckRequest;
  if (background) invalidatePageCache("/api/v2/decks");
  else cancelPagePrefetch();
  $("download-deck-results").disabled = true;
  const container = $("deck-list");
  if (!background) {
    pageControls.get("deck-pager")(0, 1);
    showDeckGridSkeleton(container, 9, true);
  }
  try {
    const data = await fetchCataloguePage("/api/v2/decks", query, { signal: controller.signal });
    if (request !== state.deckRequest) return;
    state.deckTotal = data.total;
    updateBatchButtons();
    container.replaceChildren();
    if (!data.items.length) container.append(node("p", "empty-state", "Aucun deck préconstruit trouvé."));
    for (const deck of data.items) {
      const name = deckNameLabel(deck);
      const edition = node("span", "deck-tile-edition");
      if (deck.edition_icon_url) {
        const icon = node("img");
        icon.alt = "";
        icon.draggable = false;
        icon.loading = "lazy";
        icon.addEventListener("error", () => icon.remove(), { once: true });
        icon.src = deck.edition_icon_url;
        edition.append(icon);
      }
      edition.append(deck.set_name_fr || deck.set_name || deck.set_code || "Édition inconnue");
      container.append(deckCoverTile(name, deck.composition_colors ?? deck.colors, { image_path: deck.cover_image_path },
        () => void openDeck(deck.id), edition));
    }
    const pages = Math.max(1, Math.ceil(data.total / DECK_PAGE_SIZE));
    pageControls.get("deck-pager")(state.deckPage, pages, `${formatNumber(data.total)} decks`);
    prefetchNeighbourPages("/api/v2/decks", query, data.total, container,
      () => state.view === "decks" && !state.openDeckId && request === state.deckRequest);
  } catch (error) {
    if (request === state.deckRequest) {
      state.deckListKey = null;
      showError(container, error.message);
    }
  }
  finally {
    if (state.deckController === controller) {
      state.deckController = null;
      container.removeAttribute("aria-busy");
    }
  }
}

async function loadDeckFacets(force = false) {
  if (state.deckFacetsLoaded && !force) return;
  try {
    const data = await getJSON("/api/v2/decks/filters");
    const select = $("deck-type"), current = select.value, first = select.options[0];
    select.replaceChildren(first);
    for (const type of [...(data.types || [])].sort((a, b) => productTypeLabel(a).localeCompare(productTypeLabel(b), "fr"))) {
      const option = document.createElement("option"); option.value = type; option.textContent = productTypeLabel(type); select.append(option);
    }
    select.value = current; renderMultiFilter("deck-type");
    const editionSelect = $("deck-edition"), selectedEdition = editionSelect.value, firstEdition = editionSelect.options[0];
    editionSelect.replaceChildren(firstEdition);
    for (const edition of data.editions || []) {
      const option = document.createElement("option");
      option.value = edition.code;
      option.textContent = `${edition.name_fr || edition.name_en || edition.code} (${edition.code})`;
      option.dataset.searchTerms = [edition.name_fr, edition.name_en, edition.code].map(editionSearchKey).filter(Boolean).join("|");
      editionSelect.append(option);
    }
    editionSelect.value = selectedEdition; renderMultiFilter("deck-edition");
    deckEditionSuggestions.refresh();
    const coverage = Number(data.color_indexed || 0), total = Number(data.total || 0);
    const incomplete = coverage < total;
    document.querySelectorAll(".deck-color-filter .mana-toggle").forEach(button => { button.disabled = incomplete; });
    setText($("deck-color-coverage"), incomplete ? `Préparation du filtre de couleurs : ${formatNumber(coverage)} decks sur ${formatNumber(total)}.` : "");
    state.deckFacetsLoaded = true;
    if (incomplete && !state.deckColorAttempted) {
      state.deckColorAttempted = true;
      startDeckColorIndex();
    }
  } catch (error) { setText($("deck-color-coverage"), `Filtres indisponibles : ${error.message}`); }
}

async function startDeckColorIndex() {
  try {
    await postJSON("/api/v2/decks/colors/index", {});
    pollDeckColorIndex();
  } catch (error) {
    setText($("deck-color-coverage"), `Indexation des couleurs impossible : ${error.message}. Utilisez « Actualiser la liste » pour réessayer.`);
  }
}

async function pollDeckColorIndex() {
  clearTimeout(state.deckColorPollTimer);
  try {
    const task = await getJSON("/api/v2/decks/colors/status");
    if (task.status === "complete") {
      await loadDeckFacets(true);
      if (state.deckExecuted) await renderDeckPage();
      if (Number(task.unmatched || 0) || Number(task.failed || 0)) {
        setText($("deck-color-coverage"), `Couleurs indexées pour ${formatNumber(task.indexed)} decks. ${formatNumber(task.unmatched || 0)} sans correspondance, ${formatNumber(task.failed || 0)} en échec.`);
      }
      return;
    }
    if (task.status === "failed") {
      setText($("deck-color-coverage"), `Indexation des couleurs impossible : ${task.error || "erreur inconnue"}. Utilisez « Actualiser la liste » pour réessayer.`);
      return;
    }
    const progress = `${formatNumber(task.completed || 0)} sur ${formatNumber(task.total || 0)}`;
    const phase = task.phase === "catalog" ? `Correspondance avec le catalogue local : ${progress} lots de cartes…`
      : task.phase === "fallback" ? `Vérification des decks récents : ${progress}…`
      : `Lecture des listes préconstruites : ${progress} decks…`;
    setText($("deck-color-coverage"), phase);
    state.deckColorPollTimer = setTimeout(pollDeckColorIndex, 800);
  } catch (error) {
    setText($("deck-color-coverage"), `Suivi de l’indexation impossible : ${error.message}`);
  }
}

function scrollToDeckList() {
  window.scrollTo({ top: state.deckListScroll, behavior: "instant" });
}
function closeDeck(urlMode = "push") {
  const wasOpen = Boolean(state.openDeckId);
  ++state.deckDetailRequest;
  state.openDeckId = null;
  state.selectedDeck = null;
  $("deck-open-panel").hidden = true;
  $("deck-browser").hidden = false;
  $("deck-detail").replaceChildren();
  $("deck-detail-actions").replaceChildren($("back-to-deck-list"));
  if (urlMode && state.view === "decks") {
    syncRoute(urlMode);
    if (!state.deckListKey) void ensureDeckPage();
    if (wasOpen) {
      state.deckListOpener?.focus({ preventScroll: true });
      scrollToDeckList();
      cataloguePageCaches.get("/api/v2/decks")?.resume?.();
    }
  }
}
async function openDeck(deckId, urlMode = "push") {
  const target = $("deck-detail");
  const request = ++state.deckDetailRequest;
  cancelPagePrefetch();
  const changed = deckId !== state.openDeckId;
  if (!state.openDeckId && !$("deck-browser").hidden && state.view === "decks") {
    state.deckListScroll = window.scrollY;
    const opener = document.activeElement;
    state.deckListOpener = $("deck-browser").contains(opener) ? opener : null;
  }
  state.openDeckId = deckId;
  if (changed) state.selectedDeck = null;
  hoverPreview.hidden = true;
  $("deck-browser").hidden = true;
  $("deck-open-panel").hidden = false;
  if (changed) {
    $("deck-open-heading").hidden = true;
    $("deck-detail-actions").replaceChildren($("back-to-deck-list"));
    target.replaceChildren(node("p", "muted", "Chargement de la composition…"));
    if (state.view === "decks") window.scrollTo({ top: 0, behavior: "instant" });
  }
  if (urlMode && state.view === "decks") syncRoute(urlMode);
  try {
    const data = await getJSON("/api/v2/deck", { id: deckId });
    if (request !== state.deckDetailRequest) return;
    if (!data.found) { target.replaceChildren(node("p", "empty-state", "Deck introuvable.")); return; }
    const deck = data.deck;
    state.selectedDeck = deck;
    if (state.view === "decks") syncRoute();
    const header = $("deck-open-heading");
    const eyebrow = [editionLabel(deck), productTypeLabel(deck.type)].filter(Boolean).join(" · ");
    header.replaceChildren(...deckTitleContent(eyebrow, deckNameLabel(deck), deck.composition_colors ?? deck.colors));
    header.hidden = false;
    target.replaceChildren();
    const sections = Object.entries(deck.sections || {});
    const matched = sections.some(([, cards]) => cards.some(card => card.provider_card_id));
    if (!matched) target.append(node("p", "empty-state", "Aucune impression de ce deck n'est disponible dans le catalogue actuel. Actualisez le catalogue de la Bibliothèque Magic, puis rouvrez ce deck pour télécharger ses images."));
    const summary = deck.media_summary || {total: 0, ready: 0, missing: 0};
    const actions = $("deck-detail-actions");
    const start = node("button", "secondary-button", summary.missing > 0 ? "Télécharger les images manquantes" : "Exporter le deck sur le Bureau");
    start.type = "button";
    start.disabled = Boolean(state.deckTaskId) || !matched || summary.total === 0;
    start.addEventListener("click", () => summary.missing > 0 ? startDeckDownload(deck.id) : exportDeck(deck.id));
    start.id = "deck-output";
    const copyPersonal = personalButton("Créer un deck perso", () => void importPersonalDeck(deck.id, copyPersonal), true);
    actions.replaceChildren($("back-to-deck-list"), start, copyPersonal);
    const progress = node("div", "deck-progress"); progress.id = "deck-progress"; target.append(progress);
    const labels = {commander:"Commandants", mainboard:"Deck principal", sideboard:"Réserve", display_commander:"Commandants de présentation", planar_deck:"Planechase", scheme_deck:"Machinations", tokens:"Jetons"};
    const orderedSections = sections.slice().sort(([a], [b]) => a === "commander" ? -1 : b === "commander" ? 1 : a === "tokens" ? 1 : b === "tokens" ? -1 : 0);
    for (const [key, cards] of orderedSections) {
      if (!cards.length) continue;
      const section = node("section", "deck-section");
      const heading = node("div", "deck-section-heading");
      heading.append(deckSectionTitle(labels[key] || key, cards));
      section.append(heading);
      const list = node("div", `deck-cards${key === "tokens" ? " deck-token-cards" : ""}`);
      for (const card of cards) {
        const tile = node(card.provider_card_id ? "button" : "article", `deck-card-tile${card.provider_card_id ? " deck-card-open" : ""}`);
        if (card.provider_card_id) { tile.type = "button"; tile.addEventListener("click", () => openCard(card.provider_card_id)); }
        const img = card.image_path ? node("img", "deck-card-image") : node("div", "deck-card-image deck-card-image-empty", "Image absente");
        if (card.image_path) { img.src = `/assets?${params({path: card.image_path})}`; img.alt = card.printed_name || card.name_fr || card.name || "Carte"; img.loading = "lazy"; img.onerror = () => img.replaceWith(node("div", "deck-card-image deck-card-image-empty", "Image indisponible")); }
        tile.append(img, node("span", "deck-card-name", `${card.count || 1} × ${card.printed_name || card.name_fr || card.name}`));
        list.append(tile);
      }
      section.append(list); target.append(section);
    }
    target.append(scrollTopFooter("deck-open-heading"));
    if (state.deckTaskId) pollDeckTask();
    loadDeckFacets(true);
  } catch (error) { if (request === state.deckDetailRequest) showError(target, error.message); }
}

async function startDeckDownload(deckId) {
  const progress = $("deck-progress");
  progress.replaceChildren(node("p", "muted", "Préparation du téléchargement…"));
  try {
    const task = await postJSON("/api/v2/deck/download", { deck_id: deckId });
    state.deckTaskId = task.task_id;
    const button = $("deck-output");
    if (button && state.openDeckId === deckId) button.disabled = true;
    pollDeckTask();
  } catch (error) { showError(progress, error.message); }
}

async function exportDeck(deckId) {
  const progress = $("deck-progress"), button = $("deck-output");
  if (button) { button.disabled = true; button.textContent = "Export en cours…"; }
  progress.replaceChildren(node("p", "muted", "Préparation de l’export…"));
  try {
    const result = await postJSON("/api/v2/deck/export", {deck_id: deckId});
    const fileCount = Array.isArray(result.files) ? result.files.length : Number(result.files || 0);
    progress.replaceChildren(node("p", "", `Export terminé dans ${result.folder} · ${formatNumber(fileCount)} fichiers (${formatNumber(result.total)} cartes au total).`));
  } catch (error) { showError(progress, error.message); }
  finally { if (button) { button.disabled = false; button.textContent = "Exporter le deck sur le Bureau"; } }
}

async function pollDeckTask() {
  if (!state.deckTaskId) return;
  if (state.deckPollTimer) clearTimeout(state.deckPollTimer);
  try {
    const task = await getJSON("/api/v2/deck/task", { task_id: state.deckTaskId });
    const progress = $("deck-progress");
    if (progress) {
      progress.replaceChildren(node("p", "", `${task.completed || 0}/${task.total || 0} cartes traitées · ${task.ready || 0} images prêtes · ${task.unavailable || 0} impressions non indexées · ${task.failed || 0} échecs${task.current ? ` · ${task.current}` : ""}`));
      if ((task.unavailable_cards || []).length) {
        const details = node("details", "deck-errors");
        details.append(node("summary", "muted", `${task.unavailable_cards.length} impression(s) absente(s) du catalogue`));
        const list = node("ul");
        for (const name of task.unavailable_cards) list.append(node("li", "", name));
        details.append(list); progress.append(details);
      }
      if ((task.errors || []).length) {
        const details = node("details", "deck-errors");
        details.append(node("summary", "error-state", `${task.errors.length} carte(s) à vérifier`));
        const list = node("ul");
        for (const message of task.errors) list.append(node("li", "", message));
        details.append(list); progress.append(details);
      }
      if (["queued", "running"].includes(task.status)) {
        const cancel = node("button", "secondary-button", "Annuler");
        cancel.type = "button";
        cancel.addEventListener("click", async () => { cancel.disabled = true; try { await postJSON("/api/v2/deck/cancel", { task_id: state.deckTaskId }); } catch (error) { showError(progress, error.message); } });
        progress.append(cancel);
      }
    }
    if (["queued", "running"].includes(task.status)) state.deckPollTimer = setTimeout(pollDeckTask, 1500);
    else {
      state.deckTaskId = null;
      const deckId = state.selectedDeck?.id;
      if (deckId) await openDeck(deckId);
    }
  } catch (error) { const progress = $("deck-progress"); if (progress) showError(progress, error.message); }
}

const archiveOcrViews = new Map();
let archiveRequest = 0;
const archiveResolvedByPage = new Map();
function archiveResolvedCount(beforePage = Infinity) {
  let count = 0;
  for (const [page, resolved] of archiveResolvedByPage) if (page < beforePage) count += resolved;
  return count;
}
const hoverPreview=node("img","archive-hover-preview");
hoverPreview.hidden=true;hoverPreview.alt="";document.body.append(hoverPreview);
function attachHoverPreview(thumbnail, focusTarget=thumbnail, enlarged=hoverPreview){
  const position=(x,y)=>{
    const ratio=thumbnail.naturalWidth/thumbnail.naturalHeight;
    const width=Math.min(350,window.innerWidth-24,window.innerHeight*.82*ratio);
    const height=width/ratio;
    enlarged.style.width=`${width}px`;
    enlarged.style.height=`${height}px`;
    const left=x+18+width>window.innerWidth?x-width-18:x+18;
    enlarged.style.left=`${Math.max(12,Math.min(left,window.innerWidth-width-12))}px`;
    enlarged.style.top=`${Math.max(12,Math.min(y-height/2,window.innerHeight-height-12))}px`;
  };
  const show=event=>{if(!thumbnail.complete||!thumbnail.naturalWidth)return;
    enlarged.src=thumbnail.src;enlarged.alt=thumbnail.alt;
    enlarged.hidden=false;position(event.clientX||thumbnail.getBoundingClientRect().right,event.clientY||thumbnail.getBoundingClientRect().top);
  };
  if (focusTarget === thumbnail) thumbnail.tabIndex=0;
  thumbnail.addEventListener("mouseenter",show);
  thumbnail.addEventListener("mousemove",event=>{if(!enlarged.hidden)position(event.clientX,event.clientY);});
  focusTarget.addEventListener("focus",event=>{if(focusTarget.matches(":focus-visible"))show(event);});
  thumbnail.addEventListener("mouseleave",()=>{enlarged.hidden=true;});
  focusTarget.addEventListener("blur",()=>{enlarged.hidden=true;});
}
function archiveCardName(fileName) {
  return fileName.replace(/\.[^.]+$/, "").replace(/^\d+\s*-\s*/, "").trim() || fileName;
}
async function openArchiveInGoogleLens(path, button, status) {
  const target=`magic-lens-${Date.now()}`;
  const tab=window.open("about:blank",target);
  if(!tab){setText(status,"Autorisez l’ouverture du nouvel onglet pour Google Lens.");return;}
  button.disabled=true;
  try{
    const result=await postJSON("/api/v2/archive/lens",{path});
    if(!/^[0-9a-f]{32}$/.test(result.ticket||""))throw new Error("Ticket Google invalide.");
    tab.opener=null;
    tab.location.replace(`/api/v2/archive-lens-bridge?${params({ticket:result.ticket})}`);
    setText(status,"");
  }catch(error){tab.close();setText(status,`Recherche Google impossible : ${error.message}`);}
  finally{button.disabled=false;}
}
let archiveOcrBatch = null;
async function runArchiveOcrBatch() {
  if(archiveOcrBatch||archiveAutoNameBatch)return;
  const task={stop:false};archiveOcrBatch=task;
  const start=$("archive-ocr-all"),autoStart=$("archive-auto-name-all"),stop=$("archive-ocr-stop"),status=$("archive-ocr-batch-status");
  start.disabled=true;autoStart.disabled=true;stop.hidden=false;
  let done=0,total=0,cached=0,imported=0,review=0,failed=0;const errors=[];
  try{
    const first=await getJSON("/api/v2/archive",{limit:100,offset:0});total=first.total||0;
    setText(status,`Préparation de l’OCR : ${formatNumber(total)} archive(s) à analyser.`);
    const items=[...(first.items||[])];
    for(let offset=100;offset<total&&!task.stop;offset+=100){
      const page=offset===0?first:await getJSON("/api/v2/archive",{limit:100,offset});
      items.push(...(page.items||[]));
    }
    const missing = items.filter(entry => !entry.ocr_result);
    const already = items.length - missing.length;
    setText(status,`${already} archive(s) déjà analysée(s), ${missing.length} à analyser.`);
    let next=0;
    const worker=async()=>{
      while(!task.stop&&next<missing.length){
        const entry=missing[next++];
        try{
          const result=await postJSON("/api/v2/archive/ocr",{path:entry.relative_path});
          if(result.cached)cached++;
          if(result.automation?.status==="imported")imported++;else review++;
          archiveOcrViews.get(entry.relative_path)?.(result);
        }catch(error){failed++;if(errors.length<8)errors.push(`${entry.file_name} : ${error.message}`);}
        done++;
        setText(status,`${done}/${missing.length} à analyser · ${already} déjà lue(s) · ${imported} importée(s) · ${review} à revoir · ${failed} erreur(s)`);
      }
    };
    await Promise.all(Array.from({length:Math.min(10,missing.length)},()=>worker()));
    const summary=task.stop?`OCR arrêté après ${done}/${missing.length} fichier(s) · ${imported} importée(s). Résultats conservés.`:`OCR terminé : ${done} analysée(s), ${already} déjà lue(s), ${imported} importée(s), ${review} à revoir, ${failed} erreur(s).`;
    status.replaceChildren(node("span","",summary));
    if(errors.length){const details=node("details","deck-errors");details.append(node("summary","error-state","Fichiers en erreur"));const list=node("ul");for(const error of errors)list.append(node("li","",error));details.append(list);status.append(details);}
    if(state.view==="archives"){archiveResolvedByPage.clear();await renderArchivePage(true);}
  }catch(error){setText(status,`Analyse interrompue : ${error.message}`);}
  finally{archiveOcrBatch=null;start.disabled=false;autoStart.disabled=false;stop.hidden=true;}
}
let archiveAutoNameBatch = null;
async function runArchiveAutoNameBatch() {
  if(archiveAutoNameBatch||archiveOcrBatch)return;
  const task={stop:false};archiveAutoNameBatch=task;
  const start=$("archive-auto-name-all"),ocrStart=$("archive-ocr-all"),stop=$("archive-ocr-stop"),status=$("archive-ocr-batch-status");
  start.disabled=true;ocrStart.disabled=true;stop.hidden=false;
  let done=0,total=0,renamed=0,kept=0,unnamed=0,failed=0;const errors=[];
  try{
    const first=await getJSON("/api/v2/archive",{limit:100,offset:0});total=first.total||0;
    setText(status,`Préparation du nommage : ${formatNumber(total)} archive(s) à traiter.`);
    const items=[...(first.items||[])];
    for(let offset=100;offset<total&&!task.stop;offset+=100){
      const page=offset===0?first:await getJSON("/api/v2/archive",{limit:100,offset});
      items.push(...(page.items||[]));
    }
    setText(status,`Nommage de ${formatNumber(items.length)} archive(s) en cours.`);
    let next=0;
    const worker=async()=>{
      while(!task.stop&&next<items.length){
        const entry=items[next++];
        try{
          const decision=await postJSON("/api/v2/archive/auto-name",{path:entry.relative_path});
          if(decision.status==="renamed")renamed++;else if(decision.status==="kept")kept++;else unnamed++;
          archiveOcrViews.get(entry.relative_path)?.({display_name:decision});
        }catch(error){failed++;if(errors.length<8)errors.push(`${entry.file_name} : ${error.message}`);}
        done++;
        setText(status,`${done}/${items.length} traitée(s) · ${renamed} renommée(s) · ${kept} nom(s) conservé(s) · ${unnamed} sans titre · ${failed} erreur(s)`);
      }
    };
    await Promise.all(Array.from({length:Math.min(10,items.length)},()=>worker()));
    const summary=task.stop?`Nommage arrêté après ${done}/${items.length} fichier(s). Les noms déjà enregistrés sont conservés.`:`Nommage terminé : ${done} traitée(s), ${renamed} renommée(s), ${kept} nom(s) conservé(s), ${unnamed} sans titre lisible, ${failed} erreur(s).`;
    status.replaceChildren(node("span","",summary));
    if(errors.length){const details=node("details","deck-errors");details.append(node("summary","error-state","Fichiers en erreur"));const list=node("ul");for(const error of errors)list.append(node("li","",error));details.append(list);status.append(details);}
    if(state.view==="archives"){archiveResolvedByPage.clear();await renderArchivePage(true);}
  }catch(error){setText(status,`Nommage interrompu : ${error.message}`);}
  finally{archiveAutoNameBatch=null;start.disabled=false;ocrStart.disabled=false;stop.hidden=true;}
}
let archivePageKey = null;
function archiveSelectionKey() {
  return JSON.stringify([$("archive-status").value, state.archivePage]);
}
function ensureArchivePage() {
  if (archivePageKey !== archiveSelectionKey()) return renderArchivePage();
}
async function renderArchivePage(background = false) {
  archivePageKey = archiveSelectionKey();
  if (state.view === "archives") syncRoute();
  const request = ++archiveRequest;
  const pageIndex = state.archivePage;
  const path = "/api/v2/archive";
  if (background) invalidatePageCache(path);
  else cancelPagePrefetch();
  const container = $("archive-list"), offset = Math.max(0, pageIndex * PAGE_SIZE - archiveResolvedCount(pageIndex)); hoverPreview.hidden=true;archiveOcrViews.clear();if (!background) container.replaceChildren(pageLoading("Chargement des archives…"));
  try {
    const query = { limit: PAGE_SIZE, offset, status: $("archive-status").value };
    const data = archiveResolvedCount() ? await getJSON(path, query) : await fetchCataloguePage(path, query);
    if (request !== archiveRequest) return;
    state.archiveTotal = data.total;
    container.replaceChildren();
    if (!data.items.length) container.append(node("p", "empty-state", "Aucune archive sur cette page."));
    for (const entry of data.items) {
      const article=node("article","archive-card"), img=node("img"); img.alt=`Archive ${entry.file_name}`;img.loading="eager";img.src=`/api/v2/archive-image?${params({path:entry.relative_path})}`;img.onerror=()=>img.replaceWith(node("div","missing-image","Aperçu indisponible"));attachHoverPreview(img);
      const body=node("div","archive-card-body");const initialName=typeof entry.display_name==="string"&&entry.display_name.trim()?entry.display_name.trim():archiveCardName(entry.file_name);const cardName=node("h3","",initialName);body.append(cardName,node("p","archive-file-name",entry.file_name));
      const review=node("div","archive-review");
      const ocrButton=node("button","secondary-button","Lire le texte avec OCR");ocrButton.type="button";
      const lensButton=node("button","secondary-button","Recherche Google image inversée");lensButton.type="button";lensButton.hidden=true;
      const status=node("p","muted archive-review-status");
      const searchForm=node("form","archive-search-form");
      const query=node("input","");query.type="search";query.minLength=2;query.placeholder="Nom de la carte";query.value=initialName;query.setAttribute("aria-label","Nom de la carte à rechercher");
      const language=node("select","");language.setAttribute("aria-label","Langue de la carte");language.append(new Option("Langue de l’image", ""),new Option("Français","fr"),new Option("Anglais","en"));
      const setCode=node("input","");setCode.type="text";setCode.maxLength=12;setCode.placeholder="Code d’édition";setCode.setAttribute("aria-label","Code d’édition lu sur l’image");
      const searchButton=node("button","secondary-button","Rechercher dans la bibliothèque");searchButton.type="submit";
      const nameLabel=node("label","archive-name-label","Nom de la carte");nameLabel.append(query);
      searchForm.append(nameLabel,language,setCode,searchButton);
      const results=node("div","archive-match-results");
      searchForm.hidden=true;
      const importControls=node("div","archive-import-controls");importControls.hidden=true;
      const faceSelect=node("select","");faceSelect.setAttribute("aria-label","Face de la carte");
      const importButton=node("button","primary-button","Importer l’image d’archive");importButton.type="button";
      importControls.append(faceSelect,importButton);
      let selected=null, sha256=null, sourceLanguage="";
      const updateImportButton=()=>{
        const slot=(selected?.image_slots||[]).find(item=>item.face_index===Number(faceSelect.value));
        importButton.textContent=slot?.status==="ready"?"Remplacer par l’image d’archive":"Importer l’image d’archive";
      };
      faceSelect.addEventListener("change",updateImportButton);
      const markImported=()=>{
        if(article.classList.contains("archive-card-imported"))return;
        article.classList.add("archive-card-imported");
        hoverPreview.hidden=true;archiveOcrViews.delete(entry.relative_path);
        article.querySelector(".archive-auto-name")?.remove();article.querySelector(".archive-delete")?.remove();
        review.replaceChildren(node("p","archive-imported-message","✓ Image importée dans la base"));
        invalidatePageCache(path);
        archiveResolvedByPage.set(pageIndex,(archiveResolvedByPage.get(pageIndex)||0)+1);
        state.archiveTotal=Math.max(0,state.archiveTotal-1);
        setText($("archive-summary"),`${formatNumber(state.archiveTotal)} fichiers historiques sans identité exacte`);
      };
      const applyOcrResult=data=>{
        if(article.classList.contains("archive-card-imported"))return;
        if(data.automation?.status==="imported"){
          markImported();
          return;
        }
        sha256=data.sha256;ocrButton.hidden=true;lensButton.hidden=false;
        const identity=data.identity_proposal||{};
        const target=identity.target||identity.candidates?.[0];
        const evidence=identity.evidence||{};
        const catalogueName=[target?.title,target?.catalogue_name,target?.printed_name,target?.name]
          .find(value=>typeof value==="string"&&value.trim().length>=2);
        const displayDecision=data.display_name;
        if(displayDecision&&displayDecision.status==="renamed"&&typeof displayDecision.name==="string"&&displayDecision.name.trim().length>=2){
          query.value=displayDecision.name.trim();
          setText(status,`Nom remplacé : « ${displayDecision.previous} » → « ${displayDecision.name} ».`);
        }else if(!displayDecision&&catalogueName){
          query.value=catalogueName.trim();
        }
        if(target){language.value=target.language||"";setCode.value=target.set_code||target.code||"";}
        if(query.value.trim().length>=2)cardName.textContent=query.value.trim();
        searchForm.hidden=false;if(!status.textContent)setText(status,"");
      };
      const renderMatches=items=>{
        results.replaceChildren();selected=null;
        if(!items.length){results.append(node("p","muted","Aucune impression proposée. Essayez une autre recherche."));return;}
        for(const item of items){
          const match=node("article","archive-match");
          const slot=(item.image_slots||[]).find(value=>value.face_index===0&&value.status==="ready");
          if(slot?.relative_path){const preview=node("img","archive-match-image");preview.src=`/assets?${params({path:slot.relative_path})}`;preview.alt=`Illustration de ${item.printed_name||item.name||"la carte"}`;preview.loading="lazy";attachHoverPreview(preview);match.append(preview);}
          else if(item.edition_icon_url){const icon=node("img","archive-match-symbol");icon.src=item.edition_icon_url;icon.alt="";icon.loading="lazy";match.append(icon);}
          else match.classList.add("archive-match-no-visual");
          const details=node("div","archive-match-details");const matchTitle=node("strong","",item.printed_name||item.name||"Nom inconnu");matchTitle.title=matchTitle.textContent;details.append(matchTitle);
          const edition=node("p","archive-match-edition");
          if(slot?.relative_path&&item.edition_icon_url){const icon=node("img","archive-edition-icon");icon.src=item.edition_icon_url;icon.alt="";icon.loading="lazy";edition.append(icon);}
          edition.append(node("span","",`${item.set_code||"—"} · ${item.collector_number||"—"} · ${(item.lang||"").toUpperCase()}`));
          details.append(edition);
          const pick=node("button","secondary-button","Choisir cette impression");pick.type="button";
          pick.addEventListener("click",()=>{
            selected=item;sourceLanguage=item.lang||language.value||"fr";results.querySelectorAll(".archive-match").forEach(el=>el.classList.remove("selected"));match.classList.add("selected");
            faceSelect.replaceChildren();for(const imageSlot of (item.image_slots||[])){const faceOption=node("option","",imageSlot.face_index===0?"Recto":`Face ${imageSlot.face_index+1}`);faceOption.value=String(imageSlot.face_index);faceSelect.append(faceOption);}
            if(!faceSelect.options.length){faceSelect.append(new Option("Recto","0"));}
            const slots=item.image_slots||[];
            const faceZeroReady=slots.find(slot=>String(slot.face_index)==="0")?.status==="ready";
            const firstMissingFace=Array.from(faceSelect.options).find(option=>slots.find(slot=>String(slot.face_index)===option.value)?.status!=="ready");
            faceSelect.value=faceZeroReady&&firstMissingFace?firstMissingFace.value:(faceSelect.options[0]?.value||"0");
            updateImportButton();
            importControls.hidden=false;setText(status,"");
          });
          details.append(pick);match.append(details);results.append(match);
        }
      };
      ocrButton.addEventListener("click",async()=>{
        ocrButton.disabled=true;setText(status,"Lecture locale de l’image…");
        try{
          const data=await postJSON("/api/v2/archive/ocr",{path:entry.relative_path});applyOcrResult(data);
        }catch(error){setText(status,`OCR impossible : ${error.message}`);}
        finally{ocrButton.disabled=false;}
      });
      lensButton.addEventListener("click",()=>void openArchiveInGoogleLens(entry.relative_path,lensButton,status));
      searchForm.addEventListener("submit",async event=>{
        event.preventDefault();if(query.value.trim().length<2){setText(status,"Saisissez au moins deux caractères.");return;}
        searchButton.disabled=true;setText(status,"Recherche de correspondances…");
        try{const data=await postJSON("/api/v2/archive/search",{query:query.value.trim(),set_code:setCode.value.trim()||undefined,language:language.value||undefined,limit:30});renderMatches(data.items||[]);setText(status,"");}
        catch(error){results.replaceChildren(node("p","error-state",`Recherche impossible : ${error.message}`));}
        finally{searchButton.disabled=false;}
      });
      importButton.addEventListener("click",async()=>{
        if(!selected)return;
        const faceIndex=Number(faceSelect.value),selectedLanguage=sourceLanguage||selected.lang||"fr";
        importButton.disabled=true;setText(status,"Vérification de l’impression et de la face…");
        try{
          if(!sha256){const source=await postJSON("/api/v2/archive/hash",{path:entry.relative_path});sha256=source.sha256;}
          const targetResponse=await postJSON("/api/v2/archive/target",{provider_card_id:selected.provider_card_id,printing_id:selected.printing_id,face_index:faceIndex,language:selectedLanguage});
          const target=targetResponse.target||targetResponse;
          const action=target.image_status==="ready"?"replace_indexed":"archive";
          const detail=`${target.title||selected.printed_name||selected.name} · ${target.set_code||selected.set_code} #${target.collector_number||selected.collector_number} · face ${faceIndex+1} · ${selectedLanguage.toUpperCase()}`;
          const actionText=action==="replace_indexed"?"remplacer l’image actuelle par cette archive":"importer l’image d’archive";
          if(!window.confirm(`Confirmer cette identification et ${actionText} ?\n\n${detail}\n\nAprès vérification de l’image prête, l’original pourra être retiré des archives.`)){setText(status,"Import annulé. Aucune modification effectuée.");return;}
          const imported=await postJSON("/api/v2/archive/import",{path:entry.relative_path,sha256,provider_card_id:selected.provider_card_id,printing_id:selected.printing_id,face_index:faceIndex,language:selectedLanguage,action});
          if(["imported","already_indexed","replaced_indexed"].includes(imported.status))markImported();
          else setText(status,`Import terminé : ${imported.status||"résultat inconnu"}.`);
        }catch(error){setText(status,`Import refusé : ${error.message}`);}
        finally{importButton.disabled=false;}
      });
      archiveOcrViews.set(entry.relative_path,applyOcrResult);
      const autoNameButton=node("button","archive-auto-name","🔎");autoNameButton.type="button";
      autoNameButton.title="Rechercher le nom de la carte automatiquement";autoNameButton.setAttribute("aria-label",`Rechercher automatiquement le nom de ${entry.file_name}`);
      autoNameButton.addEventListener("click",async()=>{
        autoNameButton.disabled=true;setText(status,"Recherche automatique du nom…");
        try{
          const decision=await postJSON("/api/v2/archive/auto-name",{path:entry.relative_path});
          if(typeof decision.name==="string"&&decision.name.trim().length>=2){query.value=decision.name.trim();cardName.textContent=decision.name.trim();}
          applyOcrResult({display_name:decision});
          if(decision.status==="kept")setText(status,`« ${decision.name} » est déjà trouvable dans la base : nom conservé. Recherchez puis intégrez la carte ci-dessous.`);
          else if(decision.status!=="renamed")setText(status,`Nom automatique indisponible : ${decision.reason||"aucun titre lisible par l’OCR"}. La zone d’intégration reste disponible.`);
        }catch(error){setText(status,`Recherche du nom impossible : ${error.message}`);}
        finally{autoNameButton.disabled=false;}
      });
      const deleteButton=node("button","archive-delete","🗑");deleteButton.type="button";
      deleteButton.title="Supprimer cette image de l’archive";deleteButton.setAttribute("aria-label",`Supprimer ${entry.file_name} de l’archive`);
      deleteButton.addEventListener("click",async()=>{
        if(!window.confirm(`Supprimer « ${entry.file_name} » de l’archive ?\n\nLe fichier sera définitivement retiré, sans import dans le catalogue.`))return;
        deleteButton.disabled=true;
        try{
          await postJSON("/api/v2/archive/delete",{path:entry.relative_path});
          article.remove();archiveOcrViews.delete(entry.relative_path);
          invalidatePageCache(path);
          archiveResolvedByPage.set(pageIndex,(archiveResolvedByPage.get(pageIndex)||0)+1);
          state.archiveTotal=Math.max(0,state.archiveTotal-1);
          setText($("archive-summary"),`${formatNumber(state.archiveTotal)} fichiers historiques sans identité exacte`);
        }catch(error){deleteButton.disabled=false;setText(status,`Suppression impossible : ${error.message}`);}
      });
      review.append(ocrButton,lensButton,searchForm,status,results,importControls);body.append(review);
      article.append(autoNameButton,deleteButton,img,body);container.append(article);
      if(entry.ocr_result)applyOcrResult(entry.ocr_result);
    }
    setText($("archive-summary"),`${formatNumber(state.archiveTotal)} fichiers historiques sans identité exacte`);const visibleTotal = data.total + archiveResolvedCount();
    pageControls.get("archive-pager")(state.archivePage, Math.max(1, Math.ceil(visibleTotal / PAGE_SIZE)));
    if (!archiveResolvedCount()) prefetchNeighbourPages(path, query, data.total, container,
      () => state.view === "archives" && request === archiveRequest);
  } catch (error) {
    if (request === archiveRequest) {
      archivePageKey = null;
      showError(container, error.message);
    }
  }
}

$("view-info").append(scrollTopFooter("info-title"));
attachScrollTop($("search-pager"), "search-title");
attachScrollTop($("edition-pager"), "editions-title", true);
attachScrollTop($("deck-pager"), "decks-title", true);
attachScrollTop($("archive-pager"), "archive-title", true);
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => {
  const view = button.dataset.view;
  if (view === "decks" && state.view === view && state.openDeckId) {
    closeDeck();
    return;
  }
  if (view === "perso" && state.view === view && personalState.deck) {
    void returnToPersonalList();
    return;
  }
  setView(view);
}));
document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); setView("info"); });
window.addEventListener("popstate", restoreRoute);
window.addEventListener("hashchange", restoreRoute);
$("search-form").addEventListener("input", event => {
  if (event.target === $("query")) state.searchIdentity = null;
  searchDraftTouched = true;
  persistSearchDraft();
  setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche.");
});
$("search-form").addEventListener("change", () => { searchDraftTouched = true; persistSearchDraft(); setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche."); });
window.addEventListener("pagehide", persistSearchDraft);
$("search-form").addEventListener("submit", event => {
  event.preventDefault();
  invalidatePageCache("/api/v2/search");
  rememberSearchTerm($("query").value);
  state.searchBaseFilters = mainSearchFilters();
  state.refineFilters = refineSearchFilters();
  state.searchPage = 0;
  void runSearch();
});
$("search-unique").addEventListener("change", () => {
  if (!state.searchExecuted) return;
  state.searchBaseFilters = mainSearchFilters();
  state.searchPage = 0;
  void runSearch();
});
$("download-search-results").addEventListener("click", () => void startBatchDownload("search"));
$("download-edition-results").addEventListener("click", () => void startBatchDownload("edition"));
$("download-deck-results").addEventListener("click", () => void startBatchDownload("decks"));
async function refreshVisibleView() {
  switch (state.view) {
    case "info":
      return Promise.all([loadHome(), loadMaintenanceStatus()]);
    case "recherche":
      if (state.searchController) return;
      return Promise.all([reloadEditions(), state.searchSnapshot && refreshSearchImages()]);
    case "editions":
      await reloadEditions();
      if (state.view === "editions") return refreshEditionPage();
      return;
    case "decks":
      return state.openDeckId ? openDeck(state.openDeckId, null) : refreshDeckResults();
    case "perso":
      return personalState.deck ? refreshOpenPersonalDeck() : personalState.refreshList?.(true);
    case "proxy":
      return renderProxies({ quiet: true });
    case "archives":
      return renderArchivePage(true);
  }
}
$("refresh-view").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  const view = state.view, top = window.scrollY;
  try {
    await refreshVisibleView();
    if (state.view === view) window.scrollTo({ top, behavior: "instant" });
  } finally {
    button.disabled = false;
  }
});
$("clear-search").addEventListener("click", () => { state.searchController?.abort(); state.searchController = null; ++state.searchRequest; $("search-form").reset(); state.searchIdentity = null; for (const id of ["filter-set", "filter-rarity", "filter-type"]) setMultiValue(id, ""); $("filter-color").querySelectorAll(".mana-toggle").forEach(button => setManaPressed(button, false)); searchDraftTouched = true; persistSearchDraft(); hideRecentSearches(); searchEditionSuggestions.hide(); setText($("search-status"), "Critères effacés. Cliquez sur Rechercher."); });
$("filter-color").querySelectorAll(".mana-toggle").forEach(button => button.addEventListener("click", () => { toggleMana(button, $("filter-color")); searchDraftTouched = true; persistSearchDraft(); setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche."); }));
registerPager("search-pager", "previous-page", "page-status", "next-page", "results", jumpToCataloguePage(page => {
  if (!state.searchSnapshot) return;
  state.searchPage = page;
  return loadSearchPage(state.searchSnapshot);
}));
$("edition-select").addEventListener("change",()=>{state.exactEditionCode=null;state.editionPage=0;editionSuggestions.hide();renderEditionPage();});
const editionSuggestions = attachEditionSuggestions("edition-query", "edition-suggestions", "edition-select", code => {
  const select = $("edition-select");
  select.value = code;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}, { onQuery: () => { if (state.view === "editions") syncRoute(); } });
registerPager("edition-pager", "edition-prev", "edition-page-status", "edition-next", "edition-cards", jumpToCataloguePage(page => {
  state.editionPage = page;
  return renderEditionPage();
}));
const searchEditionSuggestions = attachEditionSuggestions("search-edition-query", "search-edition-suggestions",
  "filter-set", code => addEditionToFilter("filter-set", $("search-status"), code), { keepOpen: true });
const deckEditionSuggestions = attachEditionSuggestions("deck-edition-query", "deck-edition-suggestions",
  "deck-edition", code => addEditionToFilter("deck-edition", $("deck-edition-status"), code), { keepOpen: true });
$("deck-submit").addEventListener("click", () => {
  rememberSearchTerm($("deck-query").value, RECENT_DECK_SEARCHES_KEY);
  hideDeckSuggestions();
  deckEditionSuggestions.hide();
  setText($("deck-edition-status"), "");
  invalidatePageCache("/api/v2/decks");
  state.deckAppliedFilters = deckResultFilters();
  state.deckExecuted = true;
  state.deckPage = 0;
  void renderDeckPage();
});
$("deck-edition-query").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.isComposing && !event.defaultPrevented) {
    event.preventDefault();
    $("deck-submit").click();
  }
});
$("clear-deck-filters").addEventListener("click", () => {
  for (const id of ["deck-query", "deck-edition-query", "deck-format", "deck-type", "deck-edition", "deck-color-mode", "deck-image-filter", "deck-sort", "deck-order"]) {
    const field = $(id); field.value = field.options ? field.options[0].value : "";
  }
  for (const id of ["deck-edition", "deck-format", "deck-type"]) setMultiValue(id, "");
  document.querySelectorAll(".deck-color-filter .mana-toggle").forEach(button => setManaPressed(button, false));
  hideDeckSuggestions();
  deckEditionSuggestions.hide();
  setText($("deck-edition-status"), "");
  state.deckExecuted = false; state.deckAppliedFilters = null; state.deckPage = 0; state.deckTotal = 0;
  state.deckListKey = null; state.deckController?.abort(); state.deckController = null; ++state.deckRequest;
  $("deck-list").replaceChildren();
  $("deck-list").removeAttribute("aria-busy");
  pageControls.get("deck-pager")(0, 1);
  updateBatchButtons();
  if (state.view === "decks") syncRoute();
});
$("deck-query").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); $("deck-submit").click(); }
});
document.querySelectorAll(".deck-color-filter .mana-toggle").forEach(button=>button.addEventListener("click",()=>{toggleMana(button, document.querySelector(".deck-color-filter"));}));
registerPager("deck-pager", "deck-prev", "deck-page-status", "deck-next", "deck-list", jumpToCataloguePage(page => {
  state.deckPage = page;
  return renderDeckPage();
}));
$("archive-status").addEventListener("change",()=>{archiveResolvedByPage.clear();state.archivePage=0;renderArchivePage();});
registerPager("archive-pager", "archive-prev", "archive-page-status", "archive-next", "archive-list", page => {
  if (page < state.archivePage) archiveResolvedByPage.clear();
  state.archivePage = page;
  void renderArchivePage();
}, false);
$("archive-ocr-all").addEventListener("click",()=>void runArchiveOcrBatch());
$("archive-auto-name-all").addEventListener("click",()=>void runArchiveAutoNameBatch());
$("archive-ocr-stop").addEventListener("click",()=>{if(archiveOcrBatch)archiveOcrBatch.stop=true;if(archiveAutoNameBatch)archiveAutoNameBatch.stop=true;setText($("archive-ocr-batch-status"),"Arrêt demandé. Les lectures déjà lancées se termineront avant l’arrêt.");});
$("update-library").addEventListener("click",()=>{ void toggleLibraryUpdate(); });
async function pollListImport() {
  try {
    const task = await getJSON("/api/v2/list-import");
    const active = ["queued", "running"].includes(task.status);
    $("list-import-start").disabled = active;
    $("list-import-export").disabled = task.status !== "complete" || !(task.items || []).length;
    setText($("list-import-status"), task.status === "idle" ? "" :
      task.status === "failed" ? `Import interrompu : ${task.error}` :
      `${task.completed || 0} / ${task.total || 0} cartes traitées${active ? "…" : "."}`);
    const results = $("list-import-results");
    results.replaceChildren();
    for (const item of task.items || []) {
      const label = item.status === "ready" ? "Image prête" : item.status === "not_found" ? "Introuvable" : "Échec";
      const imageLanguage = item.download?.results?.find(face => face.language)?.language;
      if (item.provider_card_id) {
        const tile = editionTile(item);
        tile.append(node("span", "list-import-result-label", `${label} · ${item.set_code} #${item.collector_number} · ${(imageLanguage || item.lang).toUpperCase()}`));
        results.append(tile);
      } else {
        const tile = node("article", "deck-card-tile edition-card-tile list-import-unmatched");
        tile.append(node("div", "deck-card-image deck-card-image-empty", "Image absente"), node("span", "deck-card-name", item.input), node("span", "list-import-result-label", label));
        results.append(tile);
      }
    }
    if (active) setTimeout(pollListImport, 1500);
    else if (task.status === "complete") void loadHome();
  } catch (error) { setText($("list-import-status"), `Suivi impossible : ${error.message}`); $("list-import-start").disabled = false; }
}
$("list-import-start").addEventListener("click", async () => {
  $("list-import-start").disabled = true;
  try { await postJSON("/api/v2/list-import", {cards: $("list-import-cards").value}); await pollListImport(); }
  catch (error) { setText($("list-import-status"), `Import impossible : ${error.message}`); $("list-import-start").disabled = false; }
});
$("list-import-export").addEventListener("click", async () => {
  const button = $("list-import-export");
  button.disabled = true;
  setText($("list-import-export-status"), "Création du dossier sur le Bureau…");
  try {
    const result = await postJSON("/api/v2/list-import/export", {});
    setText($("list-import-export-status"), `${result.folder} · ${result.total} image(s), dont ${result.blank} blanche(s).`);
  } catch (error) { setText($("list-import-export-status"), `Export impossible : ${error.message}`); }
  finally { button.disabled = false; }
});
void pollListImport();
$("close-dialog").addEventListener("click",()=>$("card-dialog").close());
$("card-dialog").addEventListener("click",(event)=>{if(event.target===$("card-dialog"))$("card-dialog").close();});
$("card-dialog").addEventListener("close",()=>{hoverPreview.hidden=true;requestAnimationFrame(()=>{hoverPreview.hidden=true;});});
document.addEventListener("keydown", event => {
  if (!$("card-dialog").open || $("image-choice-dialog").open || event.target.closest?.(".personal-picker, .proxy-association") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const target = event.target;
  if (target?.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target?.tagName || "") || event.defaultPrevented) return;
  event.preventDefault();
  navigateCardGallery(event.key === "ArrowLeft" ? -1 : 1);
});
$("image-choice-dialog").querySelector(".comparison-dialog-close").addEventListener("click",() => $("image-choice-dialog").close());
$("image-choice-dialog").addEventListener("close", () => {
  if (!imageDemoMode) return;
  imageDemoMode = null;
  imageDemoCandidate = null;
  imageDemoCardId = null;
  history.replaceState(null, "", "/" + location.hash);
});
$("back-to-deck-list").addEventListener("click", () => closeDeck());
const initialView = location.hash.slice(1).split("?", 1)[0];
setupMultiFilters();
activateView(ROUTE_VIEWS.has(initialView) ? initialView : "info");
restoreHomeCounters();
setInterval(refreshVisibleHome, 45000);
document.addEventListener("visibilitychange", refreshVisibleHome);
if (imageDemoMode) {
  void (async () => {
    try {
      const demo = await getJSON("/api/v2/demo-images", { mode: imageDemoMode });
      imageDemoCandidate = demo.candidate;
      imageDemoCardId = demo.provider_card_id;
      await openCard(demo.provider_card_id);
      $("card-detail").querySelector(".card-image-search")?.click();
    } catch (error) {
      $("stats-error").textContent = `Aperçu indisponible : ${error.message}`;
    }
  })();
}

// Personal compositions live outside the versioned catalogue. Drafts survive navigation/reload.
const personalState = { editing: false, sectionOrganization: null, deck: null, change: 0, saved: 0, saving: null, error: null, timer: null, list: null, refreshList: null, drag: null };
const proxyState = { page: 0, total: 0, request: 0, loading: false, deleting: false, snapshot: null };
const PERSONAL_CURRENT_KEY = "database-magic-personal-current-v1";
function personalButton(label, action, primary = false) {
    const b = node("button", primary ? "primary-button" : "secondary-button", label);
    b.type = "button";
    b.addEventListener("click", action);
    return b;
}
function personalSectionPosition(form, source, count) {
    let destination = source;
    const up = personalButton("↑", () => changeDestination(destination - 1));
    const down = personalButton("↓", () => changeDestination(destination + 1));
    up.setAttribute("aria-label", "Monter la section");
    down.setAttribute("aria-label", "Descendre la section");
    const position = node("span", "muted");
    position.setAttribute("role", "status");
    function changeDestination(index) {
        destination = index;
        up.disabled = destination === 0;
        down.disabled = destination === count - 1;
        setText(position, `Position ${destination + 1} sur ${count}`);
    }
    changeDestination(source);
    form.append(position, up, down);
    return {
        reset: () => changeDestination(source),
        destination: () => destination,
    };
}
function personalRemoveButton(label, action) {
    const button = personalButton("", action);
    button.setAttribute("aria-label", label);
    button.classList.add("personal-card-remove");
    button.append(cardActionIcon("close"));
    return button;
}
function removableProxyTile(item) {
    const entry = node("article", "personal-list-entry");
    const name = item.printed_name || item.name;
    const remove = personalRemoveButton(`Supprimer le proxy ${name}`, async () => {
        if (proxyState.deleting) return;
        proxyState.deleting = true;
        const status = $("proxy-folder-status");
        try {
            if (!window.confirm(`Supprimer définitivement le proxy « ${name} » ? L’image sera supprimée du dossier des proxies et le proxy disparaîtra de l’application.`)) return;
            remove.disabled = true;
            await postJSON("/api/v2/proxies/delete", { provider_card_id: item.provider_card_id, confirmed: true });
            invalidatePageCache();
            if (state.searchItems.some(card => card.provider_card_id === item.provider_card_id)) {
                state.searchItems = state.searchItems.filter(card => card.provider_card_id !== item.provider_card_id);
                state.searchTotal = Math.max(0, state.searchTotal - 1);
                renderEditionItems($("results"), state.searchItems);
                updateSearchPager();
                updateBatchButtons();
            }
            status.classList.remove("error-state");
            setText(status, `Proxy « ${name} » supprimé.`);
            void ensureEditions(true);
            if (state.view === "proxy") await renderProxies();
        } catch (error) {
            status.classList.add("error-state");
            setText(status, error.message);
            remove.disabled = false;
        } finally {
            proxyState.deleting = false;
        }
    });
    entry.append(editionTile(item), remove);
    return entry;
}
async function renderProxies({ quiet = false, paginating = false } = {}) {
    const path = "/api/v2/proxies";
    if (paginating) cancelPagePrefetch();
    else invalidatePageCache(path);
    const request = ++proxyState.request;
    const root = $("proxy-content");
    proxyState.loading = true;
    const grid = node("div", "personal-proxy-groups");
    grid.id = "personal-proxy-grid";
    grid.setAttribute("aria-live", "polite");
    const pager = node("div", "pager");
    const changePage = page => {
        proxyState.page = page;
        syncRoute();
        void renderProxies({ paginating: true });
    };
    const prev = node("button", "secondary-button", "Précédent");
    const next = node("button", "secondary-button", "Suivant");
    const pageLabel = node("span", "muted");
    pager.hidden = true;
    pager.append(prev, pageLabel, next);
    const updatePager = makePager(pager, prev, pageLabel, next, changePage);
    if (!quiet) {
        proxyState.snapshot = null;
        root.replaceChildren(node("p", "muted", "Chargement…"));
    }
    try {
        const query = { limit: PROXY_PAGE_SIZE, offset: proxyState.page * PROXY_PAGE_SIZE };
        const data = await fetchCataloguePage(path, query);
        if (request !== proxyState.request) return;
        proxyState.total = data.total;
        const pages = Math.max(1, Math.ceil(data.total / PROXY_PAGE_SIZE));
        if (proxyState.page >= pages) {
            proxyState.page = pages - 1;
            if (state.view === "proxy") syncRoute();
            await renderProxies({ quiet });
            return;
        }
        const snapshot = JSON.stringify(data);
        if (snapshot === proxyState.snapshot) {
            prefetchNeighbourPages(path, query, data.total, $("personal-proxy-grid"),
                () => state.view === "proxy" && request === proxyState.request);
            return;
        }
        for (const [title, linked] of [["À référencer", false], ["Référencés", true]]) {
            const items = data.items.filter(item => Boolean(item.proxy_source_id) === linked);
            if (!items.length) continue;
            const section = node("section", "deck-section");
            const cards = node("div", "results-grid personal-proxy-cards");
            section.append(node("h3", "", title), cards);
            cards.append(...items.map(removableProxyTile));
            grid.append(section);
        }
        if (!data.total) grid.append(node("p", "empty-state", "Aucun proxy dans la collection."));
        updatePager(proxyState.page, pages, `${formatNumber(data.total)} proxies`);
        root.replaceChildren(grid, pager);
        attachScrollTop(pager, "proxy-title", true);
        proxyState.snapshot = snapshot;
        prefetchNeighbourPages(path, query, data.total, grid,
            () => state.view === "proxy" && request === proxyState.request);
    } catch (error) {
        if (request !== proxyState.request || quiet) return;
        showError(grid, error.message);
        root.replaceChildren(grid, personalButton("Réessayer", () => void renderProxies()));
    } finally {
        if (request === proxyState.request) proxyState.loading = false;
    }
}

function refreshProxiesOnReturn() {
    if (!document.hidden && state.view === "proxy" && !proxyState.loading && !proxyState.deleting && !$("card-dialog").open) {
        void renderProxies({ quiet: true });
    }
}
window.addEventListener("focus", refreshProxiesOnReturn);
document.addEventListener("visibilitychange", refreshProxiesOnReturn);

$("proxy-import").addEventListener("click", () => $("proxy-file").click());
$("proxy-file").addEventListener("change", async event => {
    const input = event.currentTarget;
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    const button = $("proxy-import"), status = $("proxy-folder-status");
    button.disabled = true;
    setText(status, "Importation…");
    try {
        const response = await fetch("/api/v2/proxies/import?" + params({ filename: file.name }), {
            method: "POST", body: file,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || result.error);
        invalidatePageCache();
        setText(status, result.filename + " importé.");
        await renderProxies({ quiet: true });
        void ensureEditions(true);
    } catch (error) {
        setText(status, "Importation impossible. " + error.message);
    } finally {
        button.disabled = false;
    }
});

function proxyAssociationPanel(card, gallery) {
    const panel = node("section", "proxy-association");
    const body = node("div", "proxy-association-body");
    body.id = "proxy-association-body";
    const title = node("h2", "", "Associer à une carte officielle");
    const name = card.printed_name || card.name;
    const association = node("p", "muted", card.proxy_source_id ? `Associé à ${name}` : `${name} · Aucune carte associée`);
    const enlarged = node("img", "archive-hover-preview proxy-association-preview");
    enlarged.hidden = true;
    enlarged.setAttribute("aria-hidden", "true");
    $("card-dialog").append(enlarged);
    const form = node("form", "proxy-association-form");
    const nameLabel = node("label", "", "Nom de la carte"), query = node("input");
    query.type = "search";
    nameLabel.append(query);
    const editionLabel = node("label", "", "Édition officielle"), edition = node("select");
    edition.append(new Option("Toutes les éditions", ""));
    editionLabel.append(edition);
    const search = node("button", "primary-button", "Rechercher");
    search.type = "submit";
    const status = node("p", "muted");
    status.setAttribute("role", "status");
    const results = node("div", "proxy-association-results");
    const pager = node("div", "pager");
    pager.hidden = true;
    const prev = node("button", "secondary-button", "Précédent");
    const next = node("button", "secondary-button", "Suivant");
    const pageLabel = node("span", "muted");
    pager.append(prev, pageLabel, next);
    const updatePager = makePager(pager, prev, pageLabel, next, page => void load(page));
    form.append(nameLabel, editionLabel, search);
    body.append(title, association, form, status, results, pager);
    edition.disabled = true;
    const loadEditionsOnce = () => ensureEditions().then(() => {
        if (edition.options.length > 1) return;
        const options = document.createDocumentFragment();
        for (const item of exactEditions) {
            if (item.code !== PROXY_SET_CODE) options.append(new Option(`${item.name_fr || item.name} (${item.code})`, item.code));
        }
        edition.append(options);
        edition.disabled = !exactEditions.length;
    });
    if (card.proxy_source_id) {
        body.hidden = true;
        const toggle = personalButton("Réassocier", () => {
            body.hidden = !body.hidden;
            toggle.setAttribute("aria-expanded", String(!body.hidden));
            toggle.textContent = body.hidden ? "Réassocier" : "Masquer la recherche";
            if (body.hidden) enlarged.hidden = true;
            else {
                void loadEditionsOnce();
                query.focus();
            }
        });
        toggle.classList.add("proxy-association-toggle");
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-controls", body.id);
        panel.append(toggle);
    } else void loadEditionsOnce();
    panel.append(body);

    let controller, page = 0, filters;
    const pageSize = 20;
    const report = (message, error = false) => {
        setText(status, message);
        status.classList.toggle("error-state", error);
    };
    const associate = async item => {
        const buttons = [...results.querySelectorAll("button:not(:disabled)")];
        const controls = [search, ...buttons];
        const wasHidden = pager.hidden;
        pager.hidden = true;
        controls.forEach(button => { button.disabled = true; });
        report("Enregistrement de l’association…");
        try {
            await postJSON("/api/v2/proxies/associate", { provider_card_id: card.provider_card_id, source_id: item.provider_card_id });
            invalidatePageCache();
            editionPageKey = null;
            if (state.searchSnapshot && state.view === "recherche") void loadSearchPage(state.searchSnapshot);
            if (state.view === "editions") void renderEditionPage();
            if (state.view === "proxy") void renderProxies();
            if (panel.isConnected && $("card-dialog").open && gallery.current === card.provider_card_id) {
                await openCard(card.provider_card_id, gallery);
            }
        } catch (error) {
            report(`Association impossible : ${error.message}`, true);
            pager.hidden = wasHidden;
            controls.forEach(button => { button.disabled = false; });
        }
    };
    async function load(requestedPage) {
        controller?.abort();
        const path = "/api/v2/search";
        cancelPagePrefetch();
        const current = new AbortController();
        controller = current;
        enlarged.hidden = true;
        pager.hidden = true;
        results.replaceChildren();
        report("Recherche…");
        try {
            const query = { ...filters, official_only: 1, limit: pageSize, offset: requestedPage * pageSize };
            const data = await fetchCataloguePage(path, query, { signal: current.signal });
            if (!panel.isConnected || controller !== current) return;
            page = requestedPage;
            for (const item of data.items) {
                const itemName = item.printed_name || item.name;
                const row = node("div", "proxy-association-result");
                const thumbnail = personalImage(item, "proxy-association-thumbnail");
                if (thumbnail instanceof HTMLImageElement) attachHoverPreview(thumbnail, thumbnail, enlarged);
                const description = node("div", "proxy-association-description");
                const set = exactEditions.find(edition => edition.code === item.set_code);
                const meta = [set?.name_fr || set?.name || item.set_code, `n° ${item.collector_number}`, item.lang?.toUpperCase()].filter(Boolean).join(" · ");
                description.append(node("strong", "", itemName), node("span", "muted", meta));
                const selected = item.provider_card_id === card.proxy_source_id;
                const button = personalButton(selected ? "Associée" : "Associer", () => void associate(item));
                button.disabled = selected;
                button.setAttribute("aria-label", selected ? `${itemName}, impression associée` : `Associer à ${itemName}, ${meta}`);
                row.append(thumbnail, description, button);
                results.append(row);
            }
            report(data.total ? `${formatNumber(data.total)} résultat(s)` : "Aucune carte trouvée.");
            const pages = Math.ceil(data.total / pageSize);
            updatePager(page, pages);
            prefetchNeighbourPages(path, query, data.total, results,
                () => panel.isConnected && $("card-dialog").open && controller === current);
        } catch (error) {
            if (controller === current && !current.signal.aborted && panel.isConnected) report(`Recherche impossible : ${error.message}`, true);
        }
    }
    form.addEventListener("submit", event => {
        event.preventDefault();
        if (search.disabled) return;
        const q = query.value.trim(), set = edition.value;
        if (!q && !set) {
            report("Saisissez un nom ou choisissez une édition.");
            query.focus();
            return;
        }
        cancelPagePrefetch();
        cataloguePageCaches.delete("proxy-association");
        filters = { q, set };
        void load(0);
    });
    return panel;
}
function personalRead(key) {
    try {
        const storage = key === PERSONAL_CURRENT_KEY || key.startsWith("personal-draft-") ? sessionStorage : localStorage;
        return JSON.parse(storage.getItem(key));
    }
    catch {
        return null;
    }
}
function personalStore(key, value) {
    try {
        const storage = key === PERSONAL_CURRENT_KEY || key.startsWith("personal-draft-") ? sessionStorage : localStorage;
        if (value === null)
            storage.removeItem(key);
        else
            storage.setItem(key, JSON.stringify(value));
    }
    catch { /* The server remains the durable store when browser storage is unavailable. */
    }
}
function personalStatus(message, error = false) {
    const el = $("personal-save-status");
    el.textContent = message;
    el.className = error ? "error-state" : "muted";
}
function personalDraft() {
    if (personalState.deck)
        personalStore(`personal-draft-${personalState.deck.id}`, personalState.deck);
}
function personalChanged() {
    personalState.change++;
    personalState.error = null;
    personalDraft();
    personalStatus("Modifications en attente…");
    clearTimeout(personalState.timer);
    personalState.timer = setTimeout(() => void flushPersonal(), 350);
}
async function flushPersonal() {
    clearTimeout(personalState.timer);
    if (personalState.saving) {
        await personalState.saving;
        if (personalState.error)
            return false;
    }
    if (!personalState.deck || personalState.saved === personalState.change)
        return !personalState.error;
    const work = async () => {
        while (personalState.saved < personalState.change) {
            const version = personalState.change, snapshot = JSON.parse(JSON.stringify(personalState.deck));
            personalStatus("Enregistrement…");
            try {
                const result = await postJSON("/api/v2/personal-decks/save", { deck: snapshot });
                personalState.deck.revision = result.deck.revision;
                personalState.saved = version;
                personalState.error = null;
                if (personalState.change === version) {
                    personalStore(`personal-draft-${snapshot.id}`, null);
                }
                else
                    personalDraft();
            }
            catch (error) {
                personalState.error = error;
                personalDraft();
                personalStatus(`Non enregistré : ${error.message}. Votre brouillon est conservé sur cet appareil.`, true);
                const retry = personalButton("Réessayer", () => void flushPersonal());
                const reload = personalButton("Recharger le deck enregistré", async () => {
                    if (!window.confirm("Recharger la version enregistrée sur le serveur ? Le brouillon courant sera conservé séparément sur cet appareil, mais ne sera plus restauré automatiquement.")) return;
                    clearTimeout(personalState.timer);
                    if (personalState.saving) await personalState.saving;
                    const current = personalState.deck;
                    if (!current) return;
                    try {
                        localStorage.setItem(`personal-recovery-${current.id}-${Date.now()}`, JSON.stringify(current));
                        personalStore(`personal-draft-${current.id}`, null);
                    } catch (storageError) {
                        personalStatus(`Le brouillon n’a pas pu être mis de côté : ${storageError.message}. Le deck courant est conservé.`, true);
                        $("personal-save-status").append(retry, reload);
                        return;
                    }
                    personalState.deck = null;
                    personalState.change = 0;
                    personalState.saved = 0;
                    personalState.error = null;
                    await openPersonalDeck(current.id);
                });
                $("personal-save-status").append(retry, reload);
                return;
            }
        }
        personalStatus("Enregistré");
    };
    personalState.saving = work();
    await personalState.saving;
    personalState.saving = null;
    return !personalState.error;
}
function personalColors(values, onChange) {
    const group = node("div", "personal-colors mana-filter");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Couleurs du deck (aucune : incolore)");
    for (const color of ["W", "U", "B", "R", "G", "C"]) {
        const b = personalButton(COLOR_NAMES[color], () => {
            toggleMana(b, group);
            onChange([...group.querySelectorAll('[aria-pressed="true"]')].map(x => x.dataset.color));
        });
        b.className = `mana-toggle mana-${color.toLowerCase()}`;
        b.dataset.color = color;
        b.setAttribute("aria-label", COLOR_NAMES[color]);
        setManaPressed(b, values.includes(color));
        const symbols = colorSymbols([color]);
        b.replaceChildren(...symbols.firstChild.childNodes);
        group.append(b);
    }
    return group;
}
function personalImageUrl(card) {
    const path = card?.image_path || card?.image_slots?.find(s => s.status === "ready" && s.face_index === 0)?.relative_path;
    return currentCardImageUrl(card) || (path ? `/assets?${params({ path })}` : "");
}
function personalImage(card, className = "deck-card-image") {
    const imageUrl = personalImageUrl(card);
    if (!imageUrl)
        return node("div", `${className} deck-card-image-empty`, card ? "Image absente" : "Deck vide");
    const img = node("img", className);
    img.draggable = false;
    img.alt = card.printed_name || card.name || "Carte";
    img.loading = "lazy";
    img.onerror = () => img.replaceWith(node("div", `${className} deck-card-image-empty`, "Image indisponible"));
    img.src = imageUrl;
    return img;
}
async function showPersonalDecks() {
    if (personalState.deck) {
        renderPersonalEditor();
        return;
    }
    const remembered = personalRead(PERSONAL_CURRENT_KEY);
    if (remembered) {
        await openPersonalDeck(remembered);
        return;
    }
    renderPersonalList();
}
async function refreshPersonalOnFocus() {
    if (document.hidden || state.view !== "perso") return;
    const deck = personalState.deck;
    if (!deck) { personalState.refreshList?.(); return; }
    if (personalState.saving || personalState.change !== personalState.saved) return;
    try {
        const result = await getJSON("/api/v2/personal-deck", { id: deck.id });
        if (personalState.deck !== deck || personalState.saving || personalState.change !== personalState.saved) return;
        if (result.deck.revision === deck.revision) return;
        if (personalState.editing) {
            personalStatus("Ce deck a changé dans un autre onglet. Terminez l’édition puis actualisez le deck.", true);
            return;
        }
        personalState.deck = result.deck;
        renderPersonalEditor();
        personalStatus("Deck actualisé depuis un autre onglet.");
    } catch (error) { personalStatus(`Actualisation impossible : ${error.message}`, true); }
}
async function refreshOpenPersonalDeck() {
    const deck = personalState.deck;
    try {
        if (!await flushPersonal() || personalState.deck !== deck || state.view !== "perso") return;
        const { deck: fresh } = await getJSON("/api/v2/personal-deck", { id: deck.id });
        if (personalState.deck !== deck || state.view !== "perso" || personalState.saved !== personalState.change) return;
        personalState.deck = fresh;
        renderPersonalEditor();
        personalStatus("Deck actualisé.");
    } catch (error) {
        if (personalState.deck === deck && state.view === "perso") personalStatus(error.message, true);
    }
}
window.addEventListener("focus", () => void refreshPersonalOnFocus());
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refreshPersonalOnFocus(); });
function personalColorField(values, onChange) {
    const field = node("div", "personal-color-field");
    field.append(node("span", "", "Couleurs du deck"), personalColors(values.length ? values : ["C"],
        colors => onChange(colors.filter(color => color !== "C"))));
    return field;
}
function showDeckGridSkeleton(grid, count, withEdition = false) {
    grid.setAttribute("aria-busy", "true");
    grid.replaceChildren(...Array.from({ length: count }, () => {
        const tile = node("span", "personal-deck-tile deck-tile-loading");
        tile.setAttribute("aria-hidden", "true");
        tile.append(node("span", "deck-box"));
        if (withEdition) tile.append(node("span", "deck-tile-edition", "\u00a0"));
        return tile;
    }));
}
function deckCoverTile(name, colors, cover, open, edition = null, design = "standard", options) {
    const tile = personalButton("", open);
    tile.className = "personal-deck-tile deck-tile-loading";
    tile.setAttribute("aria-label", name);
    tile.setAttribute("aria-busy", "true");
    const box = node("span", "deck-box");
    const panel = node("span", "deck-box-panel");
    const frame = node("span", "deck-box-window");
    const image = personalImage(cover, "deck-box-art");
    if (image.tagName === "IMG") image.alt = "";
    frame.append(image);

    const title = node("span", "deck-box-title");
    title.append(node("span", "", name));
    title.style.fontSize = fitDeckBoxTitle(name);
    const mana = node("span", "deck-box-mana");
    const symbols = colorSymbols(colors);
    if (symbols) mana.append(symbols);
    panel.append(frame, title, mana);
    box.append(node("span", "deck-box-fallback"), panel);
    tile.append(box);
    if (edition) tile.append(edition);
    const images = [image, edition?.querySelector("img")].filter(element => element?.tagName === "IMG");
    const imagesReady = images.map(async element => {
        if (!element.complete) {
            // A lazy image has no current request to decode until it approaches the viewport.
            await new Promise(resolve => {
                const controller = new AbortController();
                const settle = () => {
                    controller.abort();
                    resolve();
                };
                const options = { signal: controller.signal };
                element.addEventListener("load", settle, options);
                element.addEventListener("error", settle, options);
            });
        }
        try { await element.decode(); }
        catch { element.remove(); }
    });
    void Promise.all([mountDeckBox(tile, box, panel, frame, title, mana, design, options), ...imagesReady]).then(() => {
        tile.title = tile.getAttribute("aria-label");
        tile.removeAttribute("aria-busy");
        tile.classList.remove("deck-tile-loading");
    });
    return tile;
}
async function readPersonalTextFile(file) {
    const maxBytes = 200 * 1024;
    if (file.size > 2 * maxBytes) throw new Error("Le fichier de deck dépasse 200 Kio de texte.");
    const data = new Uint8Array(await file.arrayBuffer());
    let text;
    if (data[0] === 0xff && data[1] === 0xfe) text = new TextDecoder("utf-16le").decode(data);
    else if (data[0] === 0xfe && data[1] === 0xff) text = new TextDecoder("utf-16be").decode(data);
    else {
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(data); }
        catch { text = new TextDecoder("windows-1252").decode(data); }
    }
    if (new TextEncoder().encode(text).byteLength > maxBytes) throw new Error("Le fichier de deck dépasse 200 Kio de texte.");
    return text;
}
function keepPersonalDropInView(findGrid, update) {
    const grid = findGrid();
    if (!grid?.getClientRects().length) {
        update();
        return;
    }
    const top = grid.getBoundingClientRect().top;
    const page = document.scrollingElement;
    const previousAnchor = page.style.overflowAnchor;
    page.style.overflowAnchor = "none";
    try {
        update();
        const next = findGrid();
        if (next?.getClientRects().length) {
            window.scrollBy({ top: next.getBoundingClientRect().top - top, behavior: "instant" });
        }
    } finally {
        page.style.overflowAnchor = previousAnchor;
    }
}
function personalDetailContent() {
    const root = $("personal-content");
    let detail = root.querySelector(":scope > .personal-detail-content");
    if (!detail) {
        detail = node("div", "personal-detail-content");
        root.append(detail);
    }
    const list = personalState.list;
    if (list && !list.root.hidden) {
        if (state.view === "perso") list.scroll = window.scrollY;
        list.root.hidden = true;
    }
    detail.hidden = false;
    return detail;
}

function renderPersonalList() {
    updatePersonalHeading();
    personalStatus("");
    const detail = $("personal-content").querySelector(":scope > .personal-detail-content");
    if (detail) {
        detail.hidden = true;
        detail.replaceChildren();
    }
    if (personalState.list) {
        const { root, actions, scroll } = personalState.list;
        $("personal-heading-content").querySelector(".deck-heading").append(actions);
        root.hidden = false;
        if (state.view === "perso") window.scrollTo({ top: scroll, behavior: "instant" });
        void personalState.refreshList?.(true);
        return;
    }
    const root = node("div", "personal-list-content");
    let decks = null, groups = [], organizing = false, importing = false;
    let busy = false, drag = null, landedDeckId = null, latestLoad = 0;
    let ungroupedSection = null;
    let visibleEntries = new Map();
    const create = personalButton("Créer un deck", renderPersonalCreate, true);
    const actions = node("div", "search-header-actions personal-list-actions");
    const filePicker = node("input");
    filePicker.type = "file";
    filePicker.accept = ".txt,.dec,text/plain";
    filePicker.hidden = true;
    const importText = personalButton("Importer", () => {
        if (busy) return;
        importing = true;
        filePicker.click();
    });
    const organize = personalButton("Organiser les sections", () => {
        organizing = !organizing;
        organize.setAttribute("aria-pressed", String(organizing));
        setText(organize, organizing ? "Terminer l’organisation" : "Organiser les sections");
        groupForm.hidden = !organizing;
        create.disabled = organizing;
        importText.disabled = organizing || (busy && importing);
        organize.classList.toggle("primary-button", organizing);
        organize.classList.toggle("secondary-button", !organizing);
        render();
    });
    organize.setAttribute("aria-pressed", "false");
    importText.disabled = true;
    organize.disabled = true;
    actions.append(create, importText, organize, filePicker);
    const importReport = node("div", "personal-import-report");
    const groupForm = node("form", "personal-section-form personal-section-organizer-form");
    const groupName = node("input");
    groupName.required = true;
    groupName.maxLength = 200;
    groupName.setAttribute("aria-label", "Nom de la nouvelle section");
    groupName.placeholder = "Nom de la nouvelle section";
    const addGroup = node("button", "primary-button", "Ajouter une section");
    addGroup.type = "submit";
    groupForm.append(groupName, addGroup);
    groupForm.hidden = true;
    groupForm.addEventListener("submit", event => {
        event.preventDefault();
        const name = groupName.value.trim();
        if (!name) return;
        void mutate(async () => {
            const { group } = await postJSON("/api/v2/personal-decks/group-create", { name });
            groups.unshift(group);
            groupForm.reset();
        }, addGroup);
    });
    filePicker.addEventListener("cancel", () => { importing = false; });
    filePicker.addEventListener("change", () => {
        const file = filePicker.files[0];
        filePicker.value = "";
        if (!file) { importing = false; return; }
        void mutate(async () => {
            importReport.replaceChildren();
            const result = await postJSON("/api/v2/personal-decks/import-text", {
                filename: file.name, text: await readPersonalTextFile(file),
            });
            await load(true);
            const unresolved = result.unresolved || [];
            if (unresolved.length) {
                const report = node("details");
                const lines = node("ul");
                for (const row of unresolved) {
                    lines.append(node("li", "", `Ligne ${row.line} · ${row.input} · ${row.reason}`));
                }
                report.append(node("summary", "", `${formatNumber(unresolved.length)} ligne(s) à vérifier`), lines);
                importReport.append(report);
            }
        }, importText).finally(() => { importing = false; });
    });
    const filters = node("div", "search-form"), row = node("div", "search-primary-row personal-filter-row");
    filters.setAttribute("role", "search");
    filters.setAttribute("aria-label", "Rechercher dans les decks personnels");
    const nameLabel = node("label", "", "Nom du deck"), name = node("input");
    name.type = "search";
    name.placeholder = "Rechercher par nom";
    nameLabel.append(name);
    const field = node("fieldset", "search-color-filter"), mana = node("div", "mana-filter-controls");
    let colors = [];
    const colorFilter = personalColors([], value => { colors = value; render(); });
    const modeLabel = node("label", "color-mode", "Mode de couleur"), mode = node("select");
    mode.append(new Option("Normal", "contains"), new Option("Couleurs exactes", "exact"));
    modeLabel.append(mode);
    mana.append(colorFilter, modeLabel);
    field.append(node("legend", "", "Couleurs du deck"), mana);
    row.append(nameLabel, field);
    const refine = node("div", "catalogue-refine"), orderFields = node("div", "catalogue-refine-fields");
    const sortLabel = node("label", "", "Trier par"), sort = node("select");
    sort.append(new Option("Ordre manuel", "manual"), new Option("Ajout récent", "recent"), new Option("Nom du deck", "name"));
    sortLabel.append(sort);
    const orderLabel = node("label", "", "Ordre"), order = node("select");
    order.append(new Option("Décroissant", "desc"), new Option("Croissant", "asc"));
    orderLabel.append(order);
    orderFields.append(sortLabel, orderLabel);
    refine.append(orderFields);
    filters.append(row, refine);
    const list = node("div", "personal-list-groups");
    $("personal-heading-content").querySelector(".deck-heading").append(actions);
    root.append(importReport, filters, groupForm, list, scrollTopFooter("personal-title"));
    $("personal-content").append(root);
    personalState.list = { root, actions, scroll: 0 };
    personalState.refreshList = (manual = false, forceRender = false) => {
        if (!root.hidden && !busy && !importing && !drag && (manual || !organizing)) return load(true, forceRender);
    };
    const normalize = value => value.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase("fr").trim();
    function render() {
        if (!decks) return;
        finishDrag();
        order.disabled = sort.value === "manual";
        const query = normalize(name.value);
        const items = decks.filter(deck => {
            if (!normalize(deck.name).includes(query)) return false;
            const current = deck.composition_colors ?? deck.colors ?? [];
            if (colors.includes("C")) return !current.length;
            return colors.every(color => current.includes(color))
                && (!colors.length || mode.value !== "exact" || colors.length === current.length);
        });
        const direction = order.value === "asc" ? 1 : -1;
        if (sort.value === "name") {
            items.sort((a, b) => direction * a.name.localeCompare(b.name, "fr"));
        } else if (sort.value === "recent") {
            items.sort((a, b) => direction * (a.creation_order - b.creation_order));
        }
        const content = document.createDocumentFragment();
        const groupsById = new Map(groups.map(group => [group.id, []]));
        const ungrouped = [];
        for (const deck of items) (groupsById.get(deck.group_id) || ungrouped).push(deck);
        ungroupedSection = null;
        if (groups.length || organizing) {
            if (ungrouped.length) {
                ungroupedSection = renderGroup(null, ungrouped);
                content.append(ungroupedSection);
            }
            for (const group of groups) content.append(renderGroup(group, groupsById.get(group.id)));
        } else if (ungrouped.length) {
            content.append(renderGrid(null, ungrouped));
        }
        if (!items.length && !groups.length && !organizing)
            content.append(node("p", "empty-state", "Aucun deck personnel pour cette sélection."));
        list.replaceChildren(content);
        visibleEntries = new Map(items.map(deck => [deck.id, visibleEntries.get(deck.id)]));
    }
    function renderGrid(groupId, items) {
        const grid = node("div", "personal-deck-grid");
        grid.dataset.groupId = groupId || "";
        grid.append(...items.map(renderDeck));
        return grid;
    }
    function renderDeck(deck) {
        const deckId = deck.id;
        const colors = deck.composition_colors ?? deck.colors ?? [];
        const manual = sort.value === "manual";
        const key = JSON.stringify([deck.name, colors, personalImageUrl(deck.cover_card),
            Boolean(deck.cover_card), deck.box_design, manual]);
        const previous = visibleEntries.get(deckId);
        if (previous?.key === key && deckId !== landedDeckId) return previous.entry;
        const entry = node("article", "personal-list-entry");
        entry.dataset.deckId = deckId;
        visibleEntries.set(deckId, { key, entry });
        const tile = deckCoverTile(deck.name, colors, deck.cover_card,
            () => void openPersonalDeck(deckId), null, deck.box_design);
        if (deckId === landedDeckId) {
            tile.classList.add("personal-deck-landed");
            landedDeckId = null;
        }
        entry.append(tile);
        if (manual) {
            tile.classList.add("personal-deck-grabbable");
            tile.addEventListener("pointerdown", event => {
                if (busy || drag || !event.isPrimary || event.button !== 0) return;
                const listeners = new AbortController();
                const options = { signal: listeners.signal };
                drag = {
                    deck: decks.find(item => item.id === deckId), entry, tile, pointer: event.pointerId, listeners,
                    x: event.clientX, y: event.clientY, source: entry.parentElement,
                    next: entry.nextElementSibling, animations: new Map(), frame: 0,
                };
                window.addEventListener("pointermove", updateDrag, options);
                window.addEventListener("pointerup", releaseDrag, options);
                window.addEventListener("pointercancel", cancelPointerDrag, options);
                list.addEventListener("lostpointercapture", cancelPointerDrag, options);
                window.addEventListener("blur", () => finishDrag(), options);
                window.addEventListener("keydown", event => {
                    if (event.key === "Escape") finishDrag();
                }, options);
            });
            tile.addEventListener("dragstart", event => event.preventDefault());
        }
        return entry;
    }
    function updateDrag(event) {
        if (event.pointerId !== drag?.pointer) return;
        if (!drag.active) {
            if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
            if (!list.isConnected) { finishDrag(); return; }
            const bounds = drag.tile.getBoundingClientRect();
            drag.offsetX = drag.x - bounds.left;
            drag.offsetY = drag.y - bounds.top;
            drag.tile.style.width = `${bounds.width}px`;
            // Set both coordinates before moving the live tile out of its grid.
            drag.tile.style.translate = `${bounds.left}px ${bounds.top}px`;
            drag.source.style.minHeight = `${drag.source.offsetHeight}px`;
            drag.entry.classList.add("personal-deck-slot");
            drag.tile.classList.add("personal-deck-dragging");
            document.body.append(drag.tile);
            document.body.classList.add("personal-deck-drag-active");
            list.setPointerCapture(event.pointerId);
            drag.active = true;
            drag.tile.dispatchEvent(new Event("deckboxlift"));
            drag.frame = requestAnimationFrame(scrollDrag);
        }
        drag.x = event.clientX;
        drag.y = event.clientY;
        drag.tile.style.translate = `${drag.x - drag.offsetX}px ${drag.y - drag.offsetY}px`;
        previewDrag();
    }
    function previewDrag() {
        const hovered = document.elementFromPoint(drag.x, drag.y);
        const grid = hovered?.closest(".personal-deck-grid")
            || hovered?.closest(".personal-list-group")?.querySelector(".personal-deck-grid");
        drag.target = grid && list.contains(grid) ? grid : null;
        if (!drag.target) {
            positionSlot(drag.source, drag.next);
            return;
        }
        const bounds = grid.getBoundingClientRect();
        const x = drag.x - bounds.left;
        const y = drag.y - bounds.top;
        const entries = [...grid.querySelectorAll(":scope > .personal-list-entry")];
        const slot = drag.entry;
        // Offset geometry excludes the FLIP transforms used by neighbouring tiles.
        if (slot.parentElement === grid
            && x >= slot.offsetLeft && x < slot.offsetLeft + slot.offsetWidth
            && y >= slot.offsetTop && y < slot.offsetTop + slot.offsetHeight) return;
        const before = entries.find(entry => entry !== slot && (
            y < entry.offsetTop || (y < entry.offsetTop + entry.offsetHeight
                && x < entry.offsetLeft + entry.offsetWidth / 2)
        ));
        positionSlot(grid, before || grid.querySelector(":scope > .empty-state"));
    }
    function positionSlot(grid, before) {
        const slot = drag.entry;
        if (before === slot || (slot.parentElement === grid && slot.nextElementSibling === before)) return;
        const previous = new Map();
        if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
            const affected = new Set([slot.parentElement, grid]);
            for (const parent of affected) {
                for (const entry of parent.querySelectorAll(":scope > .personal-list-entry")) {
                    if (entry !== slot) previous.set(entry, entry.getBoundingClientRect());
                }
            }
        }
        grid.insertBefore(slot, before);
        for (const [entry, from] of previous) {
            drag.animations.get(entry)?.cancel();
            const to = entry.getBoundingClientRect();
            const x = from.left - to.left, y = from.top - to.top;
            if (x || y) {
                drag.animations.set(entry, entry.animate([
                    { transform: `translate(${x}px,${y}px)` }, { transform: "none" },
                ], { duration: 170, easing: "cubic-bezier(.2,.75,.3,1)" }));
            }
        }
    }
    function scrollDrag() {
        if (!list.isConnected || list.getClientRects().length === 0) { finishDrag(); return; }
        const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
        const edge = 80;
        const up = Math.max(0, Math.min(1, 1 - (drag.y - top) / edge));
        const down = Math.max(0, Math.min(1, 1 - (innerHeight - drag.y) / edge));
        if (up || down) {
            const previous = scrollY;
            window.scrollBy({ top: 18 * (down - up), behavior: "instant" });
            if (scrollY !== previous) previewDrag();
        }
        drag.frame = requestAnimationFrame(scrollDrag);
    }
    function cancelPointerDrag(event) {
        if (event.pointerId === drag?.pointer) finishDrag();
    }
    function finishDrag(keepPosition = false, anchorGrid = drag?.source) {
        if (!drag) return;
        const { listeners, active, pointer, tile, entry, source, next, animations, frame } = drag;
        drag = null;
        listeners.abort();
        if (!active) return;
        cancelAnimationFrame(frame);
        for (const animation of animations.values()) animation.cancel();
        if (list.hasPointerCapture(pointer)) list.releasePointerCapture(pointer);
        keepPersonalDropInView(() => anchorGrid, () => {
            if (!keepPosition) source.insertBefore(entry, next);
            source.style.removeProperty("min-height");
            tile.style.removeProperty("width");
            tile.style.removeProperty("translate");
            tile.classList.remove("personal-deck-dragging");
            entry.classList.remove("personal-deck-slot");
            entry.append(tile);
            if (ungroupedSection) {
                ungroupedSection.hidden = !ungroupedSection.querySelector(".personal-list-entry");
            }
        });
        document.body.classList.remove("personal-deck-drag-active");
        tile.dispatchEvent(new Event("deckboxrelease"));
    }
    function releaseDrag(event) {
        if (event.pointerId !== drag?.pointer) return;
        if (!drag.active) { finishDrag(); return; }
        if (event.clientX !== drag.x || event.clientY !== drag.y) updateDrag(event);
        // Keep the pointerup under capture so its click cannot open a deck.
        const { deck, entry, target, source, next } = drag;
        const changed = target && (target !== source || entry.nextElementSibling !== next);
        const beforeId = entry.nextElementSibling?.dataset.deckId || null;
        const groupId = target?.dataset.groupId || null;
        const findGrid = () => list.querySelector(`[data-group-id="${CSS.escape(groupId || "")}"]`);
        finishDrag(Boolean(changed), target || source);
        if (!changed) return;
        const renderAfterMove = () => keepPersonalDropInView(findGrid, render);
        void mutate(async () => {
            try {
                await postJSON("/api/v2/personal-decks/group-move", {
                    deck_id: deck.id, group_id: groupId, before_deck_id: beforeId,
                });
                decks.splice(decks.indexOf(deck), 1);
                const before = decks.findIndex(item => item.id === beforeId);
                decks.splice(before < 0 ? decks.length : before, 0, deck);
                deck.group_id = groupId;
                landedDeckId = deck.id;
            } catch (error) {
                if (list.isConnected) renderAfterMove();
                throw error;
            }
        }, null, renderAfterMove);
    }
    function renderGroup(group, items) {
        const section = node("section", "deck-section personal-list-group");
        const heading = node("div", "deck-section-heading personal-section-heading");
        const title = node("h3", "", `${group?.name || "Sans section"} (${formatNumber(items.length)})`);
        heading.append(title);
        section.append(heading);
        if (organizing && group) {
            const renameForm = node("form", "personal-section-form");
            const field = node("input");
            field.required = true;
            field.maxLength = 200;
            field.setAttribute("aria-label", "Nom de la section");
            renameForm.hidden = true;
            const edit = personalButton("Modifier", () => {
                heading.hidden = true;
                renameForm.hidden = false;
                field.value = group.name;
                position.reset();
                field.focus();
                field.select();
            });
            const cancel = personalButton("Annuler", () => {
                renameForm.hidden = true;
                heading.hidden = false;
                edit.focus();
            });
            const source = groups.indexOf(group);
            renameForm.append(field);
            const position = personalSectionPosition(renameForm, source, groups.length);
            const save = node("button", "primary-button", "Valider");
            save.type = "submit";
            renameForm.append(save, cancel);
            renameForm.addEventListener("submit", event => {
                event.preventDefault();
                const name = field.value.trim();
                if (!name) return;
                void mutate(async () => {
                    await postJSON("/api/v2/personal-decks/group-rename", {
                        group_id: group.id, name, move: position.destination() - source,
                    });
                    await load(true);
                }, save);
            });
            const remove = personalButton("Supprimer", () => {
                if (!window.confirm(`Supprimer la section « ${group.name} » ? Ses decks seront déplacés dans « Sans section ».`)) return;
                void mutate(async () => {
                    await postJSON("/api/v2/personal-decks/group-delete", { group_id: group.id });
                    await load(true);
                }, remove);
            });
            heading.append(edit);
            renameForm.append(remove);
            section.append(renameForm);
        }
        const grid = renderGrid(group?.id || null, items);
        if (!items.length) grid.append(node("p", "empty-state",
            sort.value === "manual" ? "Déposez un deck ici." : "Aucun deck dans cette section."));
        section.append(grid);
        return section;
    }
    async function mutate(action, button, renderResult = render) {
        if (busy) return;
        busy = true;
        ++latestLoad;
        if (button) button.disabled = true;
        try {
            personalStatus("");
            await action();
            if (list.isConnected) renderResult();
        } catch (error) {
            if (!root.hidden) personalStatus(error.message, true);
        } finally {
            if (button) button.disabled = button === importText && organizing;
            busy = false;
        }
    }
    async function load(quiet = false, forceRender = false) {
        const request = ++latestLoad;
        if (!quiet) {
            const grid = node("div", "personal-deck-grid");
            showDeckGridSkeleton(grid, 9);
            list.replaceChildren(grid);
        }
        try {
            const response = await getJSON("/api/v2/personal-decks", { summary: 1 });
            if (request !== latestLoad || !list.isConnected) return;
            if (!forceRender && JSON.stringify([response.items, response.groups || []]) === JSON.stringify([decks, groups])) return;
            decks = response.items;
            groups = response.groups || [];
            importText.disabled = organizing || (busy && importing);
            organize.disabled = false;
            render();
        } catch (error) {
            if (request !== latestLoad || !list.isConnected || (quiet && decks)) return;
            showError(list, error.message);
            list.append(personalButton("Réessayer", () => void load()));
        }
    }
    for (const select of [mode, sort, order]) select.addEventListener("change", render);
    name.addEventListener("input", render);
    void load();
}
function renderPersonalCreate() {
    updatePersonalHeading();
    const root = personalDetailContent(), form = node("form", "panel personal-create"), label = node("label", "", "Nom du deck"), name = node("input");
    name.required = true;
    name.maxLength = 200;
    label.append(name);
    let colors = [];
    const submit = node("button", "primary-button", "Créer le deck"), errorBox = node("p", "error-state");
    submit.type = "submit";
    const back = personalButton("Retour à la liste", renderPersonalList);
    errorBox.hidden = true;
    errorBox.setAttribute("role", "alert");
    form.append(label, personalColorField([], value => {
        colors = value;
    }), errorBox, submit, back);
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        back.disabled = true;
        errorBox.hidden = true;
        try {
            await postJSON("/api/v2/personal-decks/create", { name: name.value.trim(), colors, commander: true });
            if (form.isConnected) renderPersonalList();
        }
        catch (error) {
            errorBox.textContent = error.message;
            errorBox.hidden = false;
            submit.disabled = false;
            back.disabled = false;
        }
    });
    root.replaceChildren(form);
    name.focus();
}
function adoptPersonalDeck(deck) {
    personalState.editing = false;
    personalState.sectionOrganization = null;
    personalState.deck = deck;
    personalState.change = 0;
    personalState.saved = 0;
    personalState.error = null;
    personalStore(PERSONAL_CURRENT_KEY, deck.id);
    if (state.view === "perso") syncRoute();
    renderPersonalEditor();
    personalStatus("Enregistré");
}
async function openPersonalDeck(id) {
    const root = personalDetailContent();
    const loading = node("p", "muted", "Chargement du deck…");
    root.replaceChildren(loading);
    try {
        const result = await getJSON("/api/v2/personal-deck", { id });
        if (!loading.isConnected) return;
        if (personalState.deck && personalState.deck.id !== id && personalState.saved < personalState.change) {
            await flushPersonal();
            if (!loading.isConnected) return;
        }
        const draft = personalRead(`personal-draft-${id}`);
        adoptPersonalDeck(result.deck);
        if (draft) {
            const cardsById = new Map(result.deck.sections.flatMap(section =>
                section.cards.map(card => [card.id, card])));
            for (const section of draft.sections) {
                for (const card of section.cards) {
                    const current = cardsById.get(card.id);
                    if (!current) continue;
                    card.mana_value = current.mana_value ?? card.mana_value;
                    card.colors_key = current.colors_key ?? card.colors_key;
                }
            }
            personalState.deck = draft;
            personalState.change = 1;
            renderPersonalEditor();
            personalStatus("Brouillon local restauré, en attente d’enregistrement.");
            void flushPersonal();
        }
    }
    catch (error) {
        if (!loading.isConnected) return;
        showError(root, error.message);
        root.append(personalButton("Retour à la liste", () => {
            personalStore(PERSONAL_CURRENT_KEY, null);
            renderPersonalList();
        }));
    }
}
function personalCardInsertion(grid, tile, point) {
    const bounds = grid.getBoundingClientRect();
    const x = point.clientX - bounds.left;
    const y = point.clientY - bounds.top;
    const contains = (left, top, width, height) =>
        x >= left && x < left + width && y >= top && y < top + height;

    // Layout offsets stay steady while the tiles animate toward them.
    if (tile.parentElement === grid && contains(tile.offsetLeft, tile.offsetTop, tile.offsetWidth, tile.offsetHeight)) {
        return tile.nextElementSibling;
    }

    const tiles = [...grid.querySelectorAll("[data-personal-card]")].filter(item => item !== tile);
    if (y < 0) return tiles[0] || null;
    let rowTop = null;
    let rowBottom = null;
    for (const item of tiles) {
        if (item.offsetTop !== rowTop) {
            if (rowBottom !== null && y < (rowBottom + item.offsetTop) / 2) return item;
            rowTop = item.offsetTop;
            rowBottom = rowTop;
        }
        rowBottom = Math.max(rowBottom, rowTop + item.offsetHeight);
        if (y < rowBottom && x < item.offsetLeft + item.offsetWidth / 2) return item;
    }
    return null;
}
function previewPersonalCardDrop(drag, point) {
    const { tile, ghost } = drag;
    ghost.style.left = `${point.clientX - drag.grabX}px`;
    ghost.style.top = `${point.clientY - drag.grabY}px`;
    const section = document.elementFromPoint(point.clientX, point.clientY)?.closest(".personal-section");
    const target = drag.root.contains(section) ? section.querySelector(".personal-cards-edit") : null;
    const grid = target || drag.source;
    const before = target ? personalCardInsertion(target, tile, point) : drag.originalNext;
    if (drag.target !== target) {
        drag.target?.parentElement.classList.remove("personal-drop-target");
        target?.parentElement.classList.add("personal-drop-target");
        drag.target = target;
    }

    if (tile.parentElement === grid && tile.nextElementSibling === before) return;
    const neighbors = drag.reduceMotion ? [] : [...new Set([tile.parentElement, grid])]
        .flatMap(container => [...container.querySelectorAll("[data-personal-card]")])
        .filter(item => item !== tile);
    const previous = neighbors.map(item => item.getBoundingClientRect());
    drag.animations.forEach(animation => animation.cancel());
    grid.insertBefore(tile, before);
    const destinations = neighbors.map(item => item.getBoundingClientRect());
    drag.animations = neighbors.flatMap((item, index) => {
        const x = previous[index].left - destinations[index].left;
        const y = previous[index].top - destinations[index].top;
        if (!x && !y) return [];
        return [item.animate([
            { transform: `translate(${x}px, ${y}px)`, zIndex: 1 },
            { transform: `translate(${x * .75}px, ${y * .75 - 3}px) scale(1.02)`, zIndex: 1, offset: .25 },
            { transform: "none", zIndex: 1 },
        ], { duration: 190, easing: "ease-out" })];
    });
}
function startPersonalCardDrag(event, root, tile, source, sectionId, cardId) {
    if (event.button !== 0 || !event.isPrimary || event.pointerType === "touch"
        || event.target.closest("input,select,label,.personal-card-remove") || personalState.drag) return;

    const pointerId = event.pointerId;
    const startX = event.clientX, startY = event.clientY;
    const controller = new AbortController();
    const options = { signal: controller.signal };
    let drag;
    let frame = 0;
    let point = event;
    let speed = 0;

    function finish(commit) {
        controller.abort();
        cancelAnimationFrame(frame);
        if (!drag) return;
        if (root.hasPointerCapture(pointerId)) root.releasePointerCapture(pointerId);

        const target = commit ? drag.target : null;
        const before = tile.nextElementSibling;
        const changed = target && (target !== source || before !== drag.originalNext);
        const destinationId = target?.dataset.personalCardSection || sectionId;
        const findGrid = () => root.querySelector(`[data-personal-card-section="${CSS.escape(destinationId)}"]`);
        keepPersonalDropInView(findGrid, () => {
            drag.animations.forEach(animation => animation.cancel());
            drag.target?.parentElement.classList.remove("personal-drop-target");
            drag.ghost.remove();
            source.style.minHeight = "";
            tile.classList.remove("personal-card-placeholder");
            if (source.isConnected) source.insertBefore(tile, drag.originalNext);
            personalState.drag = null;

            if (changed) personalMove(sectionId, cardId, destinationId, before?.dataset.personalCard);
        });
    }

    function scroll() {
        frame = 0;
        if (!speed) return;
        const position = window.scrollY;
        window.scrollBy({ top: speed, behavior: "instant" });
        if (window.scrollY === position) return;
        previewPersonalCardDrop(drag, point);
        frame = requestAnimationFrame(scroll);
    }

    window.addEventListener("pointermove", e => {
        if (e.pointerId !== pointerId) return;
        if (!tile.isConnected) {
            finish(false);
            return;
        }
        if (!drag) {
            if (Math.hypot(e.clientX - startX, e.clientY - startY) < 6) return;
            const face = tile.querySelector(".personal-card-open");
            const ghost = node("div", "personal-card-drag-preview");
            ghost.setAttribute("aria-hidden", "true");
            ghost.append(...[...face.children].map(child => child.cloneNode(true)));
            const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
            root.querySelectorAll("[data-personal-card]").forEach(item => {
                item.getAnimations().forEach(animation => {
                    if (animation.id === "personal-card-sort") animation.cancel();
                });
            });
            const bounds = face.getBoundingClientRect();
            const grabX = startX - bounds.left, grabY = startY - bounds.top;
            ghost.style.width = `${bounds.width}px`;
            ghost.style.setProperty("--card-grab-origin", `${grabX}px ${grabY}px`);
            drag = { root, tile, source, ghost, originalNext: tile.nextElementSibling, target: null,
                grabX, grabY, animations: [], reduceMotion };
            personalState.drag = drag;
            hoverPreview.hidden = true;
            source.style.minHeight = `${source.offsetHeight}px`;
            root.setPointerCapture(pointerId);
            tile.classList.add("personal-card-placeholder");
            document.body.append(ghost);
        }
        e.preventDefault();
        point = e;
        previewPersonalCardDrop(drag, e);
        const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
        const edge = 85;
        const up = Math.max(0, 1 - (e.clientY - top) / edge);
        const down = Math.max(0, 1 - (innerHeight - e.clientY) / edge);
        speed = 20 * (Math.min(1, down) - Math.min(1, up));
        if (speed && !frame) frame = requestAnimationFrame(scroll);
    }, options);
    window.addEventListener("pointerup", e => {
        if (e.pointerId !== pointerId) return;
        if (drag && tile.isConnected) {
            const suppressClick = click => {
                click.preventDefault();
                click.stopImmediatePropagation();
            };
            window.addEventListener("click", suppressClick, { capture: true, once: true });
            setTimeout(() => window.removeEventListener("click", suppressClick, true), 0);
            previewPersonalCardDrop(drag, e);
        }
        finish(tile.isConnected);
    }, options);
    window.addEventListener("pointercancel", e => {
        if (e.pointerId === pointerId) finish(false);
    }, options);
    window.addEventListener("blur", () => finish(false), options);
    window.addEventListener("keydown", e => {
        if (e.key !== "Escape" || !drag) return;
        e.preventDefault();
        e.stopPropagation();
        finish(false);
    }, { ...options, capture: true });
}
function personalMove(sourceId, cardId, targetId, beforeId = null) {
    const deck = personalState.deck, source = deck.sections.find(s => s.id === sourceId), target = deck.sections.find(s => s.id === targetId);
    if (!source || !target || cardId === beforeId)
        return;
    const index = source.cards.findIndex(c => c.id === cardId);
    if (index < 0)
        return;
    const [card] = source.cards.splice(index, 1), before = target.cards.findIndex(c => c.id === beforeId);
    target.cards.splice(before < 0 ? target.cards.length : before, 0, card);
    personalChanged();
    renderPersonalEditor();
}
function sortPersonalCards(deck, sectionGrids) {
    const animate = !matchMedia("(prefers-reduced-motion: reduce)").matches;
    const colorOrder = "WUBRG";
    const sortKey = card => ({
        mana: card.mana_value ?? Infinity,
        colors: card.colors_key == null ? "6" : [...colorOrder]
            .map((color, index) => card.colors_key.includes(color) ? index + 1 : "")
            .join(""),
    });
    let changed = false;
    for (const section of deck.sections) {
        const sorted = section.cards.map(card => ({ card, ...sortKey(card) }))
            .sort((a, b) => (a.mana - b.mana) || a.colors.localeCompare(b.colors))
            .map(item => item.card);
        if (sorted.every((card, index) => card === section.cards[index])) continue;

        const grid = sectionGrids.get(section.id);
        const tiles = new Map([...grid.children].map(tile => [tile.dataset.personalCard, tile]));
        const positions = animate ? new Map([...tiles.values()].map(tile => [tile, tile.getBoundingClientRect()])) : null;
        const orderedTiles = sorted.map(card => tiles.get(card.id));
        grid.append(...orderedTiles);
        section.cards = sorted;
        changed = true;
        if (!positions) continue;

        const destinations = orderedTiles.map(tile => tile.getBoundingClientRect());
        orderedTiles.forEach((tile, index) => {
            const start = positions.get(tile), end = destinations[index];
            const x = start.left - end.left, y = start.top - end.top;
            if (!x && !y) return;
            const layer = y < 0 || (!y && x < 0) ? 2 : 1;
            tile.animate([
                { transform: `translate(${x}px, ${y}px)`, zIndex: layer },
                { transform: `translate(${x * .8}px, ${y * .8 - 6}px) scale(1.035)`, zIndex: layer, offset: .25 },
                { transform: "none", zIndex: layer },
            ], { id: "personal-card-sort", duration: 460, easing: "ease-in-out" });
        });
    }
    if (changed) personalChanged();
}
function confirmPersonalCardSort(deck, sectionGrids, button) {
    const dialog = node("dialog", "personal-picker");
    const heading = node("h2", "", "Voulez-vous trier les cartes par coût de mana ?");
    const form = node("form", "personal-picker-footer");
    form.method = "dialog";
    const validate = node("button", "primary-button", "Valider");
    validate.value = "sort";
    const cancel = node("button", "secondary-button", "Annuler");
    cancel.autofocus = true;
    form.append(validate, cancel);
    dialog.append(heading, form);
    dialog.setAttribute("aria-label", heading.textContent);
    dialog.addEventListener("close", () => {
        dialog.remove();
        if (dialog.returnValue === "sort" && button.isConnected
            && personalState.deck === deck && state.view === "perso") {
            sortPersonalCards(deck, sectionGrids);
        }
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
}
function updatePersonalHeading(deck = null, editButton = null) {
    const [eyebrow, heading] = deckTitleContent(
        deck ? "Deck personnel" : "Votre collection de compositions",
        deck?.name || "Deck perso",
        deck ? deck.composition_colors ?? deck.colors ?? [] : null,
        "personal-title",
    );
    if (editButton) heading.append(editButton);
    $("personal-heading-content").replaceChildren(eyebrow, heading);
}
async function returnToPersonalList() {
    const deck = personalState.deck;
    if (!await flushPersonal() || personalState.deck !== deck) return false;
    personalState.deck = null;
    personalState.sectionOrganization = null;
    personalState.editing = null;
    personalStore(PERSONAL_CURRENT_KEY, null);
    if (state.view === "perso") syncRoute();
    renderPersonalList();
    return true;
}
function confirmPersonalDeckDeletion(deck) {
    const dialog = node("dialog", "shutdown-dialog");
    const heading = node("h2", "", "Supprimer le deck ?");
    heading.id = "personal-delete-deck-title";
    const message = node("p", "", "Voulez-vous vraiment supprimer le deck « " + deck.name + " » ?");
    message.id = "personal-delete-deck-message";
    dialog.setAttribute("aria-labelledby", heading.id);
    dialog.setAttribute("aria-describedby", message.id);
    const actions = node("div", "shutdown-actions");
    const cancel = personalButton("Annuler", () => dialog.close());
    cancel.autofocus = true;
    const confirm = personalButton("Confirmer", async () => {
        if (confirm.disabled) return;
        confirm.disabled = true;
        cancel.disabled = true;
        try {
            if (!await flushPersonal() || personalState.deck !== deck) return;
            await postJSON("/api/v2/personal-decks/delete", { deck_id: deck.id, revision: deck.revision });
            personalStore("personal-draft-" + deck.id, null);
            if (personalState.deck === deck) await returnToPersonalList();
        } catch (error) {
            if (personalState.deck === deck) personalStatus("Suppression impossible : " + error.message, true);
        } finally {
            dialog.close();
        }
    }, true);
    dialog.addEventListener("cancel", event => {
        if (confirm.disabled) event.preventDefault();
    });
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    actions.append(cancel, confirm);
    dialog.append(heading, message, actions);
    document.body.append(dialog);
    dialog.showModal();
}
function renderPersonalEditor() {
    const deck = personalState.deck, editing = personalState.editing;
    const sectionOrganization = personalState.sectionOrganization;
    hoverPreview.hidden = true;
    if (!deck)
        return;
    const root = personalDetailContent();
    const content = document.createDocumentFragment();
    const actions = node("div", "personal-toolbar"), back = personalButton("Retour à la liste", async () => {
        back.disabled = true;
        if (!await returnToPersonalList()) back.disabled = false;
    }, true);
    function deckExportButton(label, route, pendingMessage, successMessage) {
        const button = personalButton(label, async () => {
            button.disabled = true;
            try {
                if (!await flushPersonal() || personalState.deck?.id !== deck.id) return;
                personalStatus(pendingMessage);
                const result = await postJSON(route, { deck_id: deck.id });
                if (personalState.deck?.id === deck.id) personalStatus(successMessage(result));
            } catch (error) {
                if (personalState.deck?.id === deck.id) personalStatus(`Export impossible : ${error.message}`, true);
            } finally {
                button.disabled = false;
            }
        });
        return button;
    }
    actions.append(
        back,
        deckExportButton("Exporter les images", "/api/v2/personal-decks/export",
            "Préparation des images…", result => `Export terminé : ${result.folder || result.path || "Bureau"}`),
        deckExportButton("Exporter en TXT", "/api/v2/personal-decks/export-text",
            "Écriture du fichier TXT…", result => `Fichier créé sur le Bureau · ${result.file || result.path}`),
    );
    const editButton = personalButton("Modifier", () => {
        personalState.editing = { name: deck.name, boxDesign: deck.box_design ?? "standard" };
        renderPersonalEditor();
        $("personal-content").querySelector(".personal-deck-fields input")?.focus();
    });
    editButton.classList.add("personal-heading-edit");
    editButton.setAttribute("aria-label", "Modifier le nom et le design de la boîte");
    updatePersonalHeading(deck, editing ? null : editButton);
    const organize = personalButton(sectionOrganization ? "Terminer l’organisation" : "Organiser les sections", () => {
        personalState.sectionOrganization = sectionOrganization ? null : { name: "" };
        renderPersonalEditor();
        const nextFocus = $(sectionOrganization ? "personal-organize-sections" : "personal-new-section-name");
        nextFocus?.focus();
    });
    organize.id = "personal-organize-sections";
    organize.setAttribute("aria-pressed", String(Boolean(sectionOrganization)));
    organize.setAttribute("aria-expanded", String(Boolean(sectionOrganization)));
    const editActions = node("div", "search-header-actions personal-edit-actions");
    const duplicate = personalButton("Dupliquer", () => duplicatePersonalDeck(deck));
    if (sectionOrganization) {
        for (const button of [...actions.querySelectorAll("button"), duplicate]) button.disabled = true;
        organize.classList.replace("secondary-button", "primary-button");
    }
    const sectionGrids = new Map();
    const sort = personalButton("Trier les cartes", () => confirmPersonalCardSort(deck, sectionGrids, sort));
    sort.disabled = !deck.sections.some(section => section.cards.length > 1);
    editActions.append(duplicate, organize, sort);
    actions.append(editActions);
    content.append(actions);
    if (sectionOrganization) {
        const form = node("form", "personal-section-form personal-section-organizer-form");
        const name = node("input");
        name.id = "personal-new-section-name";
        name.required = true;
        name.maxLength = 200;
        name.placeholder = "Nom de la nouvelle section";
        name.setAttribute("aria-label", name.placeholder);
        name.value = sectionOrganization.name;
        name.addEventListener("input", () => { sectionOrganization.name = name.value; });
        const add = node("button", "primary-button", "Ajouter une section");
        add.type = "submit";
        form.append(name, add);
        form.addEventListener("submit", event => {
            event.preventDefault();
            const sectionName = name.value.trim();
            if (!sectionName) { name.focus(); return; }
            deck.sections.unshift({ id: crypto.randomUUID(), name: sectionName, cards: [] });
            sectionOrganization.name = "";
            personalChanged();
            renderPersonalEditor();
            $(name.id)?.focus();
        });
        content.append(form);
    }
    if (editing) {
        const fields = node("form", "panel personal-toolbar personal-deck-fields"), label = node("label", "", "Nom du deck"), name = node("input");
        name.value = editing.name;
        name.maxLength = 200;
        name.required = true;
        name.addEventListener("input", () => { editing.name = name.value; });
        label.append(name);
        const buttons = node("div", "personal-deck-field-actions");
        const validate = node("button", "primary-button", "Valider");
        validate.type = "submit";
        const finish = () => {
            personalState.editing = false;
            renderPersonalEditor();
        };
        buttons.append(validate, personalButton("Supprimer le deck", () => confirmPersonalDeckDeletion(deck)),
            personalButton("Annuler", finish));
        const designField = node("div", "personal-box-design-field");
        const designLabel = node("span", "", "Design du deckbox");
        designLabel.id = "personal-box-design-label";
        const currentDesign = deckBoxDesigns.find(([id]) => id === editing.boxDesign)?.[1] ?? "Standard";
        const design = personalButton(currentDesign, () => choosePersonalBoxDesign(deck, editing, (id, title) => {
            editing.boxDesign = id;
            setText(design, title);
        }));
        design.id = "personal-box-design-button";
        design.setAttribute("aria-haspopup", "dialog");
        design.setAttribute("aria-labelledby", `${designLabel.id} ${design.id}`);
        designField.append(designLabel, design);
        fields.append(label, designField, buttons);
        fields.addEventListener("submit", event => {
            event.preventDefault();
            const nextName = name.value.trim();
            if (!nextName) { name.focus(); return; }
            const designChanged = editing.boxDesign !== (deck.box_design ?? "standard");
            deck.name = nextName;
            deck.box_design = editing.boxDesign;
            personalChanged();
            finish();
            if (designChanged) void flushPersonal().then(async saved => {
                if (!saved || state.view !== "info") return;
                await homeActivityRequest;
                if (state.view === "info") await refreshHomeActivity();
            });
        });
        content.append(fields);
    }
    if (!deck.sections.some(section => section.cards.length)) {
        content.append(node("p", "empty-state", "Ajoutez des cartes depuis Recherche."));
    }
    for (const section of deck.sections) {
        const container = node("section", "deck-section personal-section");
        const heading = node("div", "deck-section-heading personal-section-heading");
        const title = deckSectionTitle(section.name, section.cards);
        const updateSectionCount = () => {
            title.textContent = `${section.name} (${deckSectionCount(section.cards)})`;
        };
        const form = node("form", "personal-section-form"), field = node("input");
        field.setAttribute("aria-label", "Nom de la section");
        field.required = true;
        field.maxLength = 200;
        form.hidden = true;
        form.append(field);
        const source = deck.sections.indexOf(section);
        const position = personalSectionPosition(form, source, deck.sections.length);
        const setSectionEditing = active => {
            form.hidden = !active;
            heading.hidden = active;
            if (active) {
                position.reset();
                field.value = section.name;
                field.focus();
                field.select();
            } else {
                modify.focus();
            }
        };
        const modify = personalButton("Modifier", () => setSectionEditing(true));
        modify.id = `personal-section-modify-${section.id}`;
        modify.setAttribute("aria-label", `Modifier la section ${section.name}`);
        heading.append(title);
        if (sectionOrganization) heading.append(modify);
        const saveSection = node("button", "primary-button", "Valider");
        saveSection.type = "submit";
        form.addEventListener("submit", event => {
            event.preventDefault();
            const nextName = field.value.trim();
            if (!nextName) { field.focus(); return; }
            const destination = position.destination();
            if (destination !== source) {
                section.name = nextName;
                deck.sections.splice(source, 1);
                deck.sections.splice(destination, 0, section);
                personalChanged();
                renderPersonalEditor();
                $(modify.id)?.focus();
                return;
            }
            if (nextName !== section.name) {
                section.name = nextName;
                modify.setAttribute("aria-label", `Modifier la section ${nextName}`);
                updateSectionCount();
                personalChanged();
            }
            setSectionEditing(false);
        });
        const remove = personalButton("Supprimer", () => {
            const target = deck.sections.find(s => s.id !== section.id);
            if (!target)
                return;
            if (section.cards.length && !window.confirm(`Déplacer les ${section.cards.length} références vers « ${target.name} » et supprimer « ${section.name} » ?`))
                return;
            target.cards.push(...section.cards);
            deck.sections = deck.sections.filter(s => s.id !== section.id);
            personalChanged();
            renderPersonalEditor();
        });
        remove.disabled = deck.sections.length < 2;
        form.append(saveSection, personalButton("Annuler", () => setSectionEditing(false)), remove);
        container.append(heading);
        if (sectionOrganization) container.append(form);
        const cards = node("div", "deck-cards personal-cards-edit");
        sectionGrids.set(section.id, cards);
        cards.dataset.personalCardSection = section.id;
        for (const card of section.cards) {
            const cardName = card.printed_name || card.name_fr || card.name || "Carte non résolue";
            const preview = personalImage(card);
            const tile = node("article", "deck-card-tile personal-card-edit");
            tile.dataset.personalCard = card.id;
            tile.addEventListener("pointerdown", e => startPersonalCardDrag(e, root, tile, cards, section.id, card.id));
            tile.addEventListener("dragstart", e => e.preventDefault());
            const open = personalButton("", () => void openCard(card.provider_card_id));
            open.className = "personal-card-open";
            open.setAttribute("aria-label", cardName);
            open.disabled = !card.provider_card_id;
            open.append(preview);
            if (preview.tagName !== "IMG") open.append(node("span", "deck-card-name", cardName));
            tile.append(open);
            const countLabel = node("label", "personal-quantity", "Quantité"), count = node("input");
            count.type = "number";
            count.min = "1";
            count.max = "9999";
            count.value = card.count;
            count.setAttribute("aria-label", `Quantité de ${card.name}`);
            count.required = true;
            count.step = "1";
            const storeCount = () => {
                const value = Number(count.value);
                if (!count.checkValidity() || !Number.isInteger(value) || value < 1)
                    return false;
                if (card.count !== value) {
                    card.count = value;
                    personalChanged();
                    updateSectionCount();
                }
                return true;
            };
            count.addEventListener("input", storeCount);
            count.addEventListener("change", () => {
                if (!storeCount()) {
                    count.reportValidity();
                    count.value = card.count;
                }
            });
            countLabel.append(count);
            tile.append(countLabel);
            const removeCard = personalButton("", () => {
                if (!window.confirm(`Retirer « ${cardName} » de « ${section.name} » ? Tous les exemplaires de cette ligne seront retirés.`)) return;
                section.cards.splice(section.cards.findIndex(c => c.id === card.id), 1);
                personalChanged();
                renderPersonalEditor();
            });
            removeCard.classList.add("personal-card-remove");
            removeCard.append(cardActionIcon("close"));
            removeCard.setAttribute("aria-label", `Retirer ${cardName} du deck`);
            tile.append(removeCard);
            cards.append(tile);
        }
        if (!section.cards.length)
            cards.append(node("p", "muted", "Section vide · Déposez une carte ici."));
        container.append(cards);
        content.append(container);
    }
    content.append(scrollTopFooter("personal-title"));
    root.replaceChildren(content);
}
function choosePersonalBoxDesign(deck, editing, onSelect) {
    const dialog = node("dialog", "personal-picker personal-box-design-picker");
    const heading = node("h2", "", "Design du deckbox");
    heading.id = "personal-box-design-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const gallery = node("div", "personal-picker-body personal-box-design-gallery");
    const cover = deck.sections.find(section => section.cards.length)?.cards[0];
    const colors = deck.composition_colors ?? deck.colors ?? [];
    for (const [category, designs] of deckBoxDesignGroups) {
        const section = node("section", "personal-box-design-category");
        const grid = node("div", "personal-box-design-grid");
        section.append(node("h3", "", category), grid);
        for (const [id, label] of designs) {
            const tile = deckCoverTile(editing.name.trim() || deck.name, colors, cover, () => {
                onSelect(id, label);
                dialog.close();
            }, node("span", "personal-box-design-name", label), id, { preview: true });
            tile.setAttribute("aria-label", label);
            tile.setAttribute("aria-pressed", String(id === editing.boxDesign));
            if (id === editing.boxDesign) tile.autofocus = true;
            grid.append(tile);
        }
        gallery.append(section);
    }
    const footer = node("div", "personal-picker-footer");
    footer.append(personalButton("Annuler", () => dialog.close()));
    dialog.append(heading, gallery, footer);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
}
function duplicatePersonalDeck(deck) {
    const dialog = node("dialog", "personal-picker personal-duplicate");
    const heading = node("h2", "", "Dupliquer le deck");
    const form = node("form", "personal-picker-form");
    const label = node("label", "", "Nom du nouveau deck"), name = node("input");
    name.value = deck.name;
    name.maxLength = 200;
    name.required = true;
    label.append(name);
    const status = node("p", "error-state");
    status.setAttribute("role", "status");
    const actions = node("div", "personal-picker-footer");
    const validate = node("button", "primary-button", "Valider");
    validate.type = "submit";
    const cancel = personalButton("Annuler", () => dialog.close());
    actions.append(validate, cancel);
    form.append(label, status, actions);
    dialog.append(heading, form);
    dialog.setAttribute("aria-label", heading.textContent);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.addEventListener("cancel", event => { if (validate.disabled) event.preventDefault(); });
    form.addEventListener("submit", async event => {
        event.preventDefault();
        const newName = name.value.trim();
        if (!newName || validate.disabled) { name.focus(); return; }
        name.disabled = validate.disabled = cancel.disabled = true;
        status.textContent = "";
        try {
            if (!await flushPersonal()) throw new Error("Enregistrez d’abord le deck ouvert.");
            const { deck: copy } = await postJSON("/api/v2/personal-decks/duplicate", {
                deck_id: deck.id, name: newName, revision: deck.revision,
            });
            dialog.close();
            adoptPersonalDeck(copy);
        } catch (error) {
            status.textContent = error.message;
            name.disabled = validate.disabled = cancel.disabled = false;
            name.focus();
        }
    });
    document.body.append(dialog);
    dialog.showModal();
    name.select();
}
async function importPersonalDeck(id, button) {
    button.disabled = true;
    try {
        if (!await flushPersonal())
            throw new Error("Enregistrez d’abord le deck personnel ouvert.");
        const result = await postJSON("/api/v2/personal-decks/import", { deck_id: id });
        adoptPersonalDeck(result.deck);
        setView("perso");
    }
    catch (error) {
        $("deck-progress").append(node("p", "error-state", `Copie impossible : ${error.message}`));
    }
    finally {
        button.disabled = false;
    }
}
async function choosePersonalDeck(card) {
    const dialog = node("dialog", "personal-picker personal-add-picker"), heading = node("h2", "", "Ajouter à un deck personnel"), status = node("p", "muted", "Chargement…"), list = node("div", "personal-picker-list");
    status.setAttribute("role", "status");
    const form = node("form", "personal-picker-form");
    const nameLabel = node("label", "", "Nom du deck");
    const name = node("input");
    name.type = "text";
    name.maxLength = 200;
    name.disabled = true;
    nameLabel.hidden = true;
    nameLabel.append(name);
    const create = personalButton("Créer un deck", () => {
        nameLabel.hidden = !nameLabel.hidden;
        name.disabled = nameLabel.hidden;
        create.setAttribute("aria-expanded", String(!nameLabel.hidden));
        create.textContent = nameLabel.hidden ? "Créer un deck" : "Annuler la création";
        if (!nameLabel.hidden) name.focus();
    });
    create.setAttribute("aria-expanded", "false");
    create.disabled = true;
    const validate = node("button", "primary-button", "Valider");
    validate.type = "submit";
    const footer = node("div", "personal-picker-footer");
    footer.append(create, validate);
    form.append(nameLabel, footer);
    const body = node("div", "personal-picker-body");
    body.append(status, list);
    dialog.append(heading, body, form);
    dialog.setAttribute("aria-label", heading.textContent);
    document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.showModal();
    let busy = false;
    function isBackdrop(event) {
        if (event.target !== dialog) return false;
        const bounds = dialog.getBoundingClientRect();
        return event.clientX < bounds.left || event.clientX >= bounds.right ||
            event.clientY < bounds.top || event.clientY >= bounds.bottom;
    }
    let pointerStartedOutside = false;
    dialog.addEventListener("pointerdown", event => { pointerStartedOutside = isBackdrop(event); });
    dialog.addEventListener("click", event => {
        if (pointerStartedOutside && isBackdrop(event) && !busy) form.requestSubmit(validate);
        pointerStartedOutside = false;
    });
    dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
    function report(message, error = false) {
        status.textContent = message;
        status.className = error ? "error-state" : "muted";
    }
    function setBusy(value) {
        busy = value;
        create.disabled = validate.disabled = value;
        name.disabled = value || nameLabel.hidden;
        for (const button of list.querySelectorAll("button")) button.disabled = value;
    }
    function deckButton(deck) {
        const button = personalButton(deck.name, async () => {
            if (busy) return;
            setBusy(true);
            report(`Ajout à « ${deck.name} »…`);
            try {
                if (!await flushPersonal())
                    throw new Error("Le deck ouvert contient un brouillon non enregistré.");
                const result = await postJSON("/api/v2/personal-decks/add", { deck_id: deck.id, provider_card_id: card.provider_card_id, printing_id: card.printing_id });
                if (personalState.deck?.id === deck.id) adoptPersonalDeck(result.deck);
                report(`Carte ajoutée à « ${deck.name} ».`);
            } catch (error) {
                report(`Ajout impossible : ${error.message}`, true);
            } finally {
                setBusy(false);
                if (dialog.open) button.focus({ preventScroll: true });
            }
        });
        return button;
    }
    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (busy) return;
        if (nameLabel.hidden) { dialog.close(); return; }
        const deckName = name.value.trim();
        if (!deckName) { report("Saisissez un nom de deck.", true); name.focus(); return; }
        setBusy(true);
        try {
            const { deck } = await postJSON("/api/v2/personal-decks/create", { name: deckName, colors: [], commander: true });
            name.value = "";
            nameLabel.hidden = true;
            create.textContent = "Créer un deck";
            create.setAttribute("aria-expanded", "false");
            const button = deckButton(deck);
            list.prepend(button);
            body.scrollTop = 0;
            report(`Deck « ${deck.name} » créé. Cliquez sur son nom pour ajouter la carte.`);
            setBusy(false);
            if (dialog.open) button.focus({ preventScroll: true });
        } catch (error) {
            report(`Création impossible : ${error.message}`, true);
            setBusy(false);
            if (dialog.open) name.focus();
        }
    });
    try {
        const { items } = await getJSON("/api/v2/personal-decks", { summary: 1 });
        report(items.length ? "Cliquez sur un deck pour y ajouter la carte." : "Créez un deck pour y ajouter cette carte.");
        list.append(...items.map(deckButton));
    } catch (error) {
        report(`Chargement impossible : ${error.message}`, true);
    } finally {
        create.disabled = false;
    }
}
window.addEventListener("beforeunload", event => {
    if (personalState.saved < personalState.change) {
        personalDraft();
        event.preventDefault();
        event.returnValue = "";
    }
});
void ensureEditions();
void restoreRoute();
