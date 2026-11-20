"use strict";

const $ = (id) => document.getElementById(id);
const state = { view: "info", searchPage: 0, editionPage: 0, deckPage: 0, archivePage: 0, searchTotal: 0, searchItems: [], editionCardsTotal: 0, deckTotal: 0, deckExecuted: false, deckAppliedFilters: null, archiveTotal: 0, editions: [], activeSet: "", selectedDeck: null, openDeckId: null, deckDetailRequest: 0, deckListKey: null, deckListScroll: 0, deckListOpener: null, deckTaskId: null, deckPollTimer: null, deckFacetsLoaded: false, deckColorAttempted: false, deckColorPollTimer: null, deckSuggestionRequest: 0, deckRequest: 0, deckController: null, searchRequest: 0, searchController: null, editionRequest: 0, cardRequest: 0, cardController: null, cardGallery: null, maintenancePollTimer: null };
const viewScrollPositions = new Map();
const SEARCH_DRAFT_KEY = "database-magic-search-draft-v1";
const RECENT_SEARCHES_KEY = "database-magic-recent-searches-v1";
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
async function refreshNotifications() {
  try {
    const response = await getJSON("/api/v2/notifications");
    const items = (response.items || []).filter(item => !["complete", "cancelled", "ready"].includes(item.status));
    const count = items.filter(item => ["queued", "running", "needs_review", "error", "failed"].includes(item.status)).length;
    document.getElementById("notification-count").textContent = String(count);
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
      } else if (["ai_generation", "image_preview"].includes(item.kind) && ["queued", "running"].includes(item.status)) {
        const cancel = node("button", "secondary-button", "Annuler"); cancel.addEventListener("click", async event => { event.stopPropagation(); await postJSON("/api/v2/image/task/cancel", { task_id: item.task_id }); void refreshNotifications(); }); row.append(cancel);
      }
      list.append(row);
    }
  } catch { document.getElementById("notification-count").textContent = "!"; }
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
    label.append(checkbox, document.createTextNode(option.textContent)); list.append(label);
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
// Lucide SVG paths (ISC/MIT); see LUCIDE_LICENSE.txt.
function cardActionIcon(name) {
  const shapes = {
    search: [["path", { d: "m21 21-4.34-4.34" }], ["circle", { cx: 11, cy: 11, r: 8 }]],
    minus: [["path", { d: "M5 12h14" }]],
    close: [["path", { d: "m18 6-12 12M6 6l12 12" }]],
    arrowUp: [["path", { d: "M12 19V5m-7 7 7-7 7 7" }]],
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
    renderEditionSuggestions();
    state.editionPage = routePage(query);
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
  if (view === "recherche" && query.get("auto") === "1") { void runSearch(); return; }
  if (view === "perso" && query.get("deck") && personalState.deck?.id !== query.get("deck"))
    void openPersonalDeck(query.get("deck"));
  if (restoreDeckScroll) scrollToDeckList();
  if (view === "recherche" && !state.searchItems.length) {
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
  if (view !== previousView) viewScrollPositions.set(previousView, window.scrollY);
  activateView(view);
  if (urlMode) syncRoute(urlMode);
  if (view === "editions") {
    if (previousView !== view && $("edition-select").value === PROXY_SET_CODE) editionPageKey = null;
    void ensureEditions(previousView !== view).then(ensureEditionPage);
  }
  if (view === "recherche" && previousView !== view) void ensureEditions(true);
  if (view === "decks" && !state.openDeckId) void loadDeckFacets().then(() => { if (state.deckExecuted) ensureDeckPage(); });
  if (view === "perso" && !$("personal-content").hasChildNodes() && !new URLSearchParams(location.hash.split("?")[1] || "").has("deck")) void showPersonalDecks();
  if (view === "proxy") void renderProxies({ quiet: view === previousView && Boolean(urlMode) });
  if (view === "archives") void ensureArchivePage();
  if (view === "info") {
    void loadHome();
    void loadMaintenanceStatus();
  }
  if (view !== previousView) {
    window.scrollTo({ top: viewScrollPositions.get(view) ?? 0, behavior: "instant" });
  }
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
  try {
    const { personal_decks: decks } = await getJSON("/api/v2/recent-activity");
    const snapshot = JSON.stringify(decks);
    if (snapshot !== homeActivitySnapshot) {
      const recentDecks = decks.map(deck => {
        const tile = deckCoverTile(deck.name, deck.colors || [], deck.cover_card, () => {
          location.hash = `#perso?deck=${encodeURIComponent(deck.id)}`;
        });
        tile.title = deck.name;
        return tile;
      });
      $("home-recent-decks").replaceChildren(...(recentDecks.length ? recentDecks :
        [node("p", "muted", "Aucun deck personnel.")]));
      homeActivitySnapshot = snapshot;
    }
    errorBox.hidden = true;
  } catch (error) {
    if (homeActivitySnapshot === undefined) {
      $("home-recent-decks").replaceChildren();
    }
    setText(errorBox, `Activité récente indisponible : ${error.message}`);
    errorBox.hidden = false;
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
    const option = node("option", "", `${code.toUpperCase()} · édition précise`);
    option.value = code; select.append(option);
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
    if (!groups.has(key)) groups.set(key, { ...root, members: [] });
    groups.get(key).members.push(edition.code);
  }
  return [...groups.values()].map(group => ({ ...group, code: [group.code, ...group.members.filter(code => code !== group.code)].join(",") }))
    .sort((a, b) => Number(b.code === PROXY_SET_CODE) - Number(a.code === PROXY_SET_CODE)
      || (b.released_at || "").localeCompare(a.released_at || "") || a.name.localeCompare(b.name));
}
async function loadEditions() {
  const all = []; let offset = 0; let total = Infinity;
  while (offset < total) { const page = await getJSON("/api/v2/editions", { limit: 100, offset }); total = page.total; all.push(...page.items); offset += page.items.length; if (!page.items.length) break; }
  exactEditions = all;
  const grouped = groupEditions(all);
  state.editions = grouped.map(e => ({ ...e, search_fr: editionSearchKey(e.name_fr || e.name), search_en: editionSearchKey(e.name || e.name_fr) }));
  for (const id of ["filter-set", "edition-select"]) {
    const select = $(id), selected = select.value;
    const options = document.createDocumentFragment(); options.append(select.options[0]);
    for (const e of grouped) { const option = document.createElement("option"); option.value = e.code; option.textContent = `${e.name_fr || e.name} (${e.code.split(",")[0]})`; option.dataset.searchTerms = [e.name_fr || e.name, e.name || e.name_fr, ...e.code.split(",")].map(editionSearchKey).join("|"); options.append(option); }
    select.replaceChildren(options); select.value = groupedEditionCode(selected);
    if (id === "filter-set") renderMultiFilter(id);
  }
  renderEditionSuggestions();
  setText($("edition-count"), "");
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
  if (!editionsPromise) editionsPromise = loadEditions().catch(error => { editionsPromise = null; setText($("edition-count"), `Éditions indisponibles : ${error.message}`); });
  return editionsPromise;
}
function editionSearchKey(value) { return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("fr"); }
let editionSuggestionCodes = [];
let activeEditionSuggestion = -1;
function hideEditionSuggestions() {
  $("edition-suggestions").hidden = true;
  $("edition-query").setAttribute("aria-expanded", "false");
  $("edition-query").removeAttribute("aria-activedescendant");
  activeEditionSuggestion = -1;
}
function highlightEditionSuggestion(index) {
  const options = $("edition-suggestions").querySelectorAll('[role="option"]');
  if (!options.length) return;
  activeEditionSuggestion = (index + options.length) % options.length;
  options.forEach((option, i) => option.setAttribute("aria-selected", i === activeEditionSuggestion ? "true" : "false"));
  $("edition-query").setAttribute("aria-activedescendant", options[activeEditionSuggestion].id);
  options[activeEditionSuggestion].scrollIntoView({ block: "nearest" });
}
function chooseEditionSuggestion(code) {
  $("edition-select").value = code;
  $("edition-query").value = "";
  hideEditionSuggestions();
  state.editionPage = 0;
  void renderEditionPage();
}
function matchingEditionOptions(select, term) {
  const options = Array.from(select.options).filter(option => option.value);
  const terms = option => (option.dataset.searchTerms || editionSearchKey(option.textContent)).split("|");
  const starts = options.filter(option => terms(option).some(value => value.startsWith(term)));
  const contains = options.filter(option => !starts.includes(option) && terms(option).some(value => value.includes(term)));
  return [...starts, ...contains].slice(0, 8);
}
function renderEditionSuggestions() {
  const term = editionSearchKey($("edition-query").value.trim());
  const list = $("edition-suggestions");
  list.replaceChildren();
  editionSuggestionCodes = [];
  hideEditionSuggestions();
  if (!term) return;
  const matches = matchingEditionOptions($("edition-select"), term);
  for (const edition of matches) {
    const option = node("div", "edition-suggestion", edition.textContent);
    option.id = `edition-suggestion-${editionSuggestionCodes.length}`;
    option.setAttribute("role", "option"); option.setAttribute("aria-selected", "false");
    option.addEventListener("click", () => chooseEditionSuggestion(edition.value));
    list.append(option); editionSuggestionCodes.push(edition.value);
  }
  if (!matches.length) { list.append(node("p", "edition-suggestion-empty", "Aucune édition trouvée.")); }
  list.hidden = false;
  $("edition-query").setAttribute("aria-expanded", "true");
}
function attachEditionSuggestions(inputId, listId, selectId, onSelect) {
  const input = $(inputId), list = $(listId), select = $(selectId);
  let codes = [], active = -1;
  const hide = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); active = -1; };
  const choose = code => { if (MULTI_FILTER_IDS.includes(selectId)) setMultiValue(selectId, code); else select.value = code; input.value = ""; hide(); onSelect(); };
  const render = () => {
    const term = editionSearchKey(input.value.trim());
    list.replaceChildren(); codes = []; hide();
    if (!term) return;
    for (const option of matchingEditionOptions(select, term)) {
      const item = node("div", "edition-suggestion", option.textContent);
      item.id = `${listId}-${codes.length}`;
      item.setAttribute("role", "option"); item.setAttribute("aria-selected", "false");
      item.addEventListener("click", () => choose(option.value));
      list.append(item); codes.push(option.value);
    }
    if (!codes.length) list.append(node("p", "edition-suggestion-empty", "Aucune édition trouvée."));
    list.hidden = false; input.setAttribute("aria-expanded", "true");
  };
  input.addEventListener("input", render);
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") { hide(); return; }
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && codes.length && !list.hidden) {
      event.preventDefault(); active = (active + (event.key === "ArrowDown" ? 1 : -1) + codes.length) % codes.length;
      list.querySelectorAll('[role="option"]').forEach((item, index) => item.setAttribute("aria-selected", index === active ? "true" : "false"));
      input.setAttribute("aria-activedescendant", `${listId}-${active}`);
    } else if (event.key === "Enter" && active >= 0) { event.preventDefault(); choose(codes[active]); }
  });
  document.addEventListener("pointerdown", event => { if (!event.target.closest(`#${inputId}`) && !event.target.closest(`#${listId}`)) hide(); });
}
let deckSuggestionIds = [];
let activeDeckSuggestion = -1;
let deckSuggestionTimer = null;
function hideDeckSuggestions() {
  $("deck-suggestions").hidden = true;
  $("deck-query").setAttribute("aria-expanded", "false");
  $("deck-query").removeAttribute("aria-activedescendant");
  activeDeckSuggestion = -1;
}
function highlightDeckSuggestion(index) {
  const options = $("deck-suggestions").querySelectorAll('[role="option"]');
  if (!options.length) return;
  activeDeckSuggestion = (index + options.length) % options.length;
  options.forEach((option, i) => option.setAttribute("aria-selected", i === activeDeckSuggestion ? "true" : "false"));
  $("deck-query").setAttribute("aria-activedescendant", options[activeDeckSuggestion].id);
  options[activeDeckSuggestion].scrollIntoView({ block: "nearest" });
}
async function renderDeckSuggestions() {
  const term = $("deck-query").value.trim(), list = $("deck-suggestions");
  const request = ++state.deckSuggestionRequest;
  list.replaceChildren(); deckSuggestionIds = []; hideDeckSuggestions();
  if (!term) return;
  try {
    const data = await getJSON("/api/v2/decks", { q: term, limit: 8, offset: 0, sort: "name", order: "asc" });
    if (request !== state.deckSuggestionRequest || term !== $("deck-query").value.trim()) return;
    const key = editionSearchKey(term);
    const matches = (data.items || []).filter(deck => editionSearchKey(`${deck.name_fr || ""} ${deck.name || ""}`).includes(key)).slice(0, 8);
    for (const deck of matches) {
      const option = node("div", "edition-suggestion", deckNameLabel(deck));
      option.id = `deck-suggestion-${deckSuggestionIds.length}`;
      option.setAttribute("role", "option"); option.setAttribute("aria-selected", "false");
      option.addEventListener("click", () => { hideDeckSuggestions(); void openDeck(deck.id); });
      list.append(option); deckSuggestionIds.push(deck.id);
    }
    if (!matches.length) list.append(node("p", "edition-suggestion-empty", "Aucun deck trouvé."));
    list.hidden = false; $("deck-query").setAttribute("aria-expanded", "true");
  } catch (error) {
    if (request === state.deckSuggestionRequest) { list.append(node("p", "edition-suggestion-empty", `Suggestions indisponibles : ${error.message}`)); list.hidden = false; }
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
function recentSearchTerms() {
  try {
    const items = JSON.parse(localStorage.getItem(RECENT_SEARCHES_KEY) || "[]");
    return Array.isArray(items) ? items.filter(value => typeof value === "string" && value.trim()).slice(0, 10) : [];
  } catch { return []; }
}
function rememberSearchTerm(value) {
  const term = String(value || "").trim();
  if (!term) return;
  const items = recentSearchTerms().filter(item => item.toLocaleLowerCase("fr") !== term.toLocaleLowerCase("fr"));
  items.unshift(term);
  try { localStorage.setItem(RECENT_SEARCHES_KEY, JSON.stringify(items.slice(0, 10))); } catch {}
}
function forgetSearchTerm(term) {
  const items = recentSearchTerms().filter(item => item.toLocaleLowerCase("fr") !== term.toLocaleLowerCase("fr"));
  try { localStorage.setItem(RECENT_SEARCHES_KEY, JSON.stringify(items)); } catch {}
}
function hideRecentSearches() {
  $("search-suggestions").hidden = true;
  $("query").setAttribute("aria-expanded", "false");
}
function renderRecentSearches() {
  const input = $("query"), list = $("search-suggestions"), key = input.value.trim().toLocaleLowerCase("fr");
  if (!document.hasFocus() || (document.activeElement !== input && !list.contains(document.activeElement))) {
    hideRecentSearches();
    return;
  }
  const terms = recentSearchTerms().filter(term => term.toLocaleLowerCase("fr") !== key && (!key || term.toLocaleLowerCase("fr").includes(key))).slice(0, 8);
  list.replaceChildren();
  if (!terms.length) { hideRecentSearches(); return; }
  for (const term of terms) {
    const row = node("div", "recent-search-row");
    const option = node("button", "edition-suggestion", term);
    option.type = "button";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.addEventListener("pointerdown", event => event.preventDefault());
    option.addEventListener("click", () => {
      input.value = term;
      state.searchIdentity = null;
      searchDraftTouched = true;
      persistSearchDraft();
      input.focus();
      hideRecentSearches();
      setText($("search-status"), "Terme repris. Cliquez sur Rechercher.");
    });
    const remove = node("button", "recent-search-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Supprimer « ${term} » des recherches récentes`);
    remove.addEventListener("pointerdown", event => event.preventDefault());
    remove.addEventListener("click", () => {
      forgetSearchTerm(term);
      input.focus();
      renderRecentSearches();
    });
    row.append(option, remove);
    list.append(row);
  }
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}
function searchFilters() {
  const refined = state.refineFilters || refineSearchFilters();
  const direction = refined.result_order || "desc";
  const sortField = refined.result_sort || "date";
  const resultSort = sortField === "date" ? (direction === "asc" ? "date_asc" : "date") : `${sortField}_${direction}`;
  const { result_order, ...backendRefined } = refined;
  return { ...(state.searchBaseFilters || mainSearchFilters()), ...backendRefined, result_sort: resultSort, limit: SEARCH_PAGE_SIZE, offset: state.searchPage * SEARCH_PAGE_SIZE };
}
async function runSearch(updateDraft = true) {
  state.searchExecuted = true;
  if (state.view === "recherche") syncRoute();
  if (updateDraft) persistSearchDraft();
  hideRecentSearches();
  state.searchController?.abort();
  const controller = new AbortController();
  state.searchController = controller;
  const request = ++state.searchRequest;
  $("search-status").replaceChildren(node("span", "card-loading-spinner search-loading-spinner"), node("span", "", "Recherche…"));
  try {
    const data = await getJSON("/api/v2/search", searchFilters(), { signal: controller.signal });
    if (request !== state.searchRequest) return;
    const lastPage = Math.max(0, Math.ceil(data.total / SEARCH_PAGE_SIZE) - 1);
    if (!updateDraft && state.searchPage > lastPage) {
      state.searchPage = lastPage;
      return runSearch(false);
    }
    state.searchTotal = data.total;
    state.searchItems = data.items || [];
    renderEditionItems($("results"), state.searchItems);
    updateSearchPager(); updateBatchButtons();
    setText($("search-status"), `${formatNumber(data.total)} résultat(s)`);
  }
  catch (error) { if (request === state.searchRequest) { showError($("results"), error.message); setText($("search-status"), "La recherche a échoué."); } }
  finally {
    if (state.searchController === controller) state.searchController = null;
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
function updateSearchPager() { const pages = Math.max(1, Math.ceil(state.searchTotal / SEARCH_PAGE_SIZE)); $("search-pager").hidden = pages <= 1; setText($("page-status"), `Page ${state.searchPage + 1} sur ${pages} · ${formatNumber(state.searchTotal)} résultats`); $("previous-page").disabled = state.searchPage === 0; $("next-page").disabled = (state.searchPage + 1) * SEARCH_PAGE_SIZE >= state.searchTotal; }
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
    if (state.view === "recherche" && state.searchItems.length) void refreshSearchImages();
    if (state.view === "editions") void refreshEditionPage();
    if (state.view === "decks") void renderDeckPage();
  } catch (error) { batchPollTimer = setTimeout(pollBatchDownload, 3000); }
}
function openEdition(code) { $("edition-query").value = ""; hideEditionSuggestions(); state.exactEditionCode = null; $("edition-select").value = groupedEditionCode(code); state.activeSet = $("edition-select").value; state.editionPage = 0; setView("editions"); }
let editionPageKey = null;
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
  editionController?.abort();
  editionController = null;
  editionRefreshPending = false;
  editionPageKey = editionSelectionKey();
  if (state.view === "editions") syncRoute();
  const request = ++state.editionRequest;
  const code = $("edition-select").value; state.activeSet = code; const container = $("edition-cards");
  $("download-edition-results").disabled = true;
  $("edition-pager").hidden = !code;
  if (!code) { container.replaceChildren(); state.editionCardsTotal = 0; setText($("edition-page-status"), ""); updateBatchButtons(); return; }
  const controller = new AbortController();
  editionController = controller;
  if (!background) container.replaceChildren(node("p", "muted", "Chargement…"));
  try {
    const data = await getJSON("/api/v2/edition", { set: code, limit: EDITION_PAGE_SIZE, offset: state.editionPage * EDITION_PAGE_SIZE }, { signal: controller.signal });
    if (request !== state.editionRequest) return;
    state.editionCardsTotal = data.total;
    renderEditionItems(container, data.items);
    updateEditionPager();
    updateBatchButtons();
  }
  catch (error) {
    if (request === state.editionRequest) {
      editionPageKey = null;
      if (!background) showError(container, error.message);
    }
  } finally {
    if (editionController === controller) {
      editionController = null;
      const refresh = editionRefreshPending;
      editionRefreshPending = false;
      if (refresh && state.view === "editions" && editionPageKey === editionSelectionKey()) void refreshEditionPage();
    }
  }
}
function updateEditionPager() { const pages = Math.max(1, Math.ceil(state.editionCardsTotal / EDITION_PAGE_SIZE)); $("edition-pager").hidden = !state.activeSet || pages <= 1; setText($("edition-page-status"), `Page ${state.editionPage + 1} sur ${pages} · ${formatNumber(state.editionCardsTotal)} cartes`); $("edition-prev").disabled = state.editionPage === 0; $("edition-next").disabled = (state.editionPage + 1) * EDITION_PAGE_SIZE >= state.editionCardsTotal; }
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
  if (state.view === "recherche") return { kind: "search", page: state.searchPage, query: searchFilters(), total: state.searchTotal, rows: galleryRecords(visible("#results .edition-card-tile")), current: id };
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
async function galleryNeighbor(direction) {
  const ctx = state.cardGallery; if (!ctx) return;
  const navigation = ++galleryNavigation;
  const request = state.cardRequest;
  const isCurrent = () => navigation === galleryNavigation && request === state.cardRequest && $("card-dialog").open;
  let rows = ctx.rows || [], index = rows.findIndex(x => String(x.provider_card_id) === ctx.current);
  if (["search", "edition", "proxy"].includes(ctx.kind)) {
    const pageSize = { search: SEARCH_PAGE_SIZE, edition: EDITION_PAGE_SIZE, proxy: PROXY_PAGE_SIZE }[ctx.kind];
    const getPage = async page => {
      const path = { search: "/api/v2/search", edition: "/api/v2/edition", proxy: "/api/v2/proxies" }[ctx.kind];
      const values = { ...(ctx.kind === "search" ? ctx.query : { set: ctx.set }), limit: pageSize, offset: page * pageSize };
      const data = await getJSON(path, values);
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
        group("Éditions", entries.editions, e => `#editions?set=${encodeURIComponent(e.code)}&exact=1`, e => `${e.name_fr || e.name || e.code} (${e.code.toUpperCase()})`, e => {
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
    header.append(node("p", "eyebrow", [card.edition || card.set_code, card.collector_number && `n° ${card.collector_number}`].filter(Boolean).join(" · ")));
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
            if (wizardStep === "text" && String(index) === textSelect.value) button.classList.add("selected");
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
            const visual = options.find(item => String(item.index) === visualSelect.value);
            const visible = options.filter(value => (wizardStep !== "compare" || !value.visual_only && !value.text_reference_only)
              && (wizardStep === "text" ? value.language === "fr" || value.text_reference_only || value.local_original_reference : wizardStep === "visual" ? !value.text_reference_only || value.local_original_reference : !language || value.language === language));
            for (const item of visible)
              addThumb(item.preview_url, `Image ${item.language.toUpperCase()}${item.local_original_reference ? " déjà intégrée" : item.text_reference_only ? " même nom" : ""}`, item.index,
                `${item.language.toUpperCase()}${item.local_original_reference ? " · originale intégrée" : item.text_reference_only ? " · même nom · autre édition" : ""} · ${item.width} × ${item.height} px`);
            if (!visible.length) thumbs.append(node("p", "muted", wizardStep === "compare" ? "Aucune autre image à comparer." : "Aucune image disponible pour ce choix."));
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
          const acknowledgeError = node("button", "primary-button", "Compris, revenir aux images"); acknowledgeError.type = "button";
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
            const validLocalText = Boolean(visual && options.some(item=>String(item.index)===textSelect.value&&(item.language==="fr"&&item.printing_id===visual.printing_id&&item.face_index===visual.face_index||item.text_reference_only||item.local_original_reference)));
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
            stepDescription.hidden = !choosing;
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
              stepDescription.textContent = "Choisissez l’original intégré, une image française du même nom (y compris d’une autre édition), ou le texte officiel de la base locale.";
              renderThumbnails("fr");
              const choice = options.find(item => String(item.index) === textSelect.value);
              if (choice) showPreview(choice.preview_url, "Référence textuelle française", selectionPreview);
              else selectionPreview.replaceChildren(node("p", "muted", "Sélectionnez une carte à gauche."));
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
            try { await postJSON("/api/v2/image/task/cancel", { task_id: aiTaskId }); setText(aiProgressMessage, "Annulation demandée…"); }
            catch (error) { setText(aiProgressMessage, `Annulation impossible : ${error.message}`); cancelWaiting.disabled = false; }
          });
          const runAIGeneration = async (promptOverride = null) => {
            const visual=options.find(item=>String(item.index)===visualSelect.value),text=options.find(item=>String(item.index)===textSelect.value);
            if (!visual || textMode === "image" && (!text || !(text.language === "fr" || text.text_reference_only || text.local_original_reference) || !(visual.printing_id===text.printing_id&&visual.face_index===text.face_index || text.text_reference_only || text.local_original_reference))) { setText(aiStatus, "Choisissez une référence visuelle, puis une image française, l’original intégré ou une image du même nom."); return; }
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
              aiTaskId = result.task_id;
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
                if (!aiTaskId) return;
                try {
                  const task = await getJSON("/api/v2/image/task", { task_id: aiTaskId });
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
                  if (["cancelled", "canceled"].includes(task.status)) { setText(errorMessage, "La génération a été annulée."); setWizardStep("error"); cancelWaiting.hidden = true; void refreshNotifications(); return; }
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
          acknowledgeError.addEventListener("click", () => { aiId = null; textMode = "image"; correction.value = ""; reviewCorrection.hidden = true; setWizardStep("compare"); });
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
      const cancel = node("button", "secondary-button", "Annuler"); cancel.type = "button"; cancel.addEventListener("click", async () => { cancel.disabled = true; try { await postJSON("/api/v2/image/task/cancel", { task_id: taskId }); } catch (error) { setText(waiting.querySelector("p"), `Annulation impossible : ${error.message}`); cancel.disabled = false; } });
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
      const failed = node("section", "image-comparison"); failed.append(node("h2", "comparison-title", task.status === "cancelled" ? "Génération annulée" : "Génération impossible"), node("p", "error-state", task.detail || "La génération n’a pas abouti."));
      const close = node("button", "secondary-button", providerId ? "Revenir à la sélection d’images" : "Fermer"); close.type = "button"; close.addEventListener("click", async () => { reviewDialog.close(); if (providerId) { await openCard(providerId, null, faceIndex); $("card-detail").querySelector(".card-image-search")?.click(); } }); failed.append(close); content.replaceChildren(failed);
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
  if (state.view === "editions") void refreshEditionPage();
  else if (state.view === "recherche") {
    if (proxyImage) void runSearch(false);
    else void refreshSearchImages();
  }
  else if (state.view === "decks") {
    void renderDeckPage();
    if (state.selectedDeck?.id) void openDeck(state.selectedDeck.id);
  } else if (state.view === "info") void loadHome();
  else if (state.view === "proxy" && proxyImage) void renderProxies();
  else if (state.view === "perso" && proxyImage) {
    if (personalState.deck) renderPersonalEditor();
    else renderPersonalList();
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
    if (state.view === "decks" && state.deckAppliedFilters === filters && !state.openDeckId) await renderDeckPage();
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
async function renderDeckPage() {
  if (state.openDeckId || !state.deckExecuted) return;
  if (state.view === "decks") syncRoute();
  const query = deckPageQuery();
  state.deckListKey = params(query);
  state.deckController?.abort();
  const controller = new AbortController();
  state.deckController = controller;
  const request = ++state.deckRequest;
  $("download-deck-results").disabled = true;
  const container = $("deck-list");
  const waiting = node("p", "muted search-loading-state");
  waiting.append(node("span", "card-loading-spinner search-loading-spinner"), node("span", "", "Chargement des decks…"));
  container.replaceChildren(waiting);
  try {
    const data = await getJSON("/api/v2/decks", query, { signal: controller.signal });
    if (request !== state.deckRequest) return;
    state.deckTotal = data.total;
    updateBatchButtons();
    container.replaceChildren();
    if (!data.items.length) container.append(node("p", "empty-state", "Aucun deck préconstruit trouvé."));
    for (const deck of data.items) {
      const name = deckNameLabel(deck);
      const tile = deckCoverTile(name, deck.colors, { name, image_path: deck.cover_image_path }, () => void openDeck(deck.id));
      const edition = node("span", "deck-tile-edition");
      if (deck.edition_icon_url) {
        const icon = node("img");
        icon.src = deck.edition_icon_url;
        icon.alt = "";
        icon.loading = "lazy";
        icon.addEventListener("error", () => icon.remove(), { once: true });
        edition.append(icon);
      }
      edition.append(deck.set_name_fr || deck.set_name || deck.set_code || "Édition inconnue");
      tile.append(edition);
      container.append(tile);
    }
    const pages = Math.max(1, Math.ceil(data.total / DECK_PAGE_SIZE));
    setText($("deck-page-status"), `Page ${state.deckPage + 1} sur ${pages} · ${formatNumber(data.total)} decks`);
    $("deck-pager").hidden = pages <= 1;
    $("deck-prev").disabled = state.deckPage === 0;
    $("deck-next").disabled = (state.deckPage + 1) * DECK_PAGE_SIZE >= data.total;
  } catch (error) {
    if (request === state.deckRequest) {
      state.deckListKey = null;
      showError(container, error.message);
    }
  }
  finally { if (state.deckController === controller) state.deckController = null; }
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
    }
  }
}
async function openDeck(deckId, urlMode = "push") {
  const target = $("deck-detail");
  const request = ++state.deckDetailRequest;
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
    header.replaceChildren(...deckTitleContent(eyebrow, deckNameLabel(deck), deck.colors));
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
      section.append(deckSectionTitle(labels[key] || key, cards));
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
    if(state.view==="archives"){archiveResolvedByPage.clear();await renderArchivePage();}
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
    if(state.view==="archives"){archiveResolvedByPage.clear();await renderArchivePage();}
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
async function renderArchivePage() {
  archivePageKey = archiveSelectionKey();
  if (state.view === "archives") syncRoute();
  const request = ++archiveRequest;
  const pageIndex = state.archivePage;
  const container = $("archive-list"), offset = Math.max(0, pageIndex * PAGE_SIZE - archiveResolvedCount(pageIndex)); hoverPreview.hidden=true;archiveOcrViews.clear();container.replaceChildren(node("p", "muted", "Chargement des archives…"));
  try {
    const data = await getJSON("/api/v2/archive", { limit: PAGE_SIZE, offset, status: $("archive-status").value });
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
          archiveResolvedByPage.set(pageIndex,(archiveResolvedByPage.get(pageIndex)||0)+1);
          state.archiveTotal=Math.max(0,state.archiveTotal-1);
          setText($("archive-summary"),`${formatNumber(state.archiveTotal)} fichiers historiques sans identité exacte`);
        }catch(error){deleteButton.disabled=false;setText(status,`Suppression impossible : ${error.message}`);}
      });
      review.append(ocrButton,lensButton,searchForm,status,results,importControls);body.append(review);
      article.append(autoNameButton,deleteButton,img,body);container.append(article);
      if(entry.ocr_result)applyOcrResult(entry.ocr_result);
    }
    setText($("archive-summary"),`${formatNumber(state.archiveTotal)} fichiers historiques sans identité exacte`);const visibleTotal=data.total+archiveResolvedCount();const pages=Math.max(1,Math.ceil(visibleTotal/PAGE_SIZE));$("archive-pager").hidden=pages<=1;setText($("archive-page-status"),`Page ${state.archivePage+1} sur ${pages}`);$("archive-prev").disabled=state.archivePage===0;$("archive-next").disabled=(state.archivePage+1)*PAGE_SIZE>=visibleTotal;
  } catch (error) {
    if (request === archiveRequest) {
      archivePageKey = null;
      showError(container, error.message);
    }
  }
}

document.querySelectorAll("[data-view]").forEach((button)=>button.addEventListener("click",()=>setView(button.dataset.view)));
document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); setView("info"); });
window.addEventListener("popstate", restoreRoute);
window.addEventListener("hashchange", restoreRoute);
$("search-form").addEventListener("input", event => {
  if (event.target === $("query")) state.searchIdentity = null;
  searchDraftTouched = true;
  persistSearchDraft();
  setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche.");
  if (event.target === $("query")) {
    renderRecentSearches();
  }
});
$("search-form").addEventListener("change", () => { searchDraftTouched = true; persistSearchDraft(); setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche."); });
window.addEventListener("pagehide", persistSearchDraft);
$("search-form").addEventListener("submit", event => {
  event.preventDefault();
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
      if (state.searchItems.some(item => !item.proxy && item.printing_id)) return refreshSearchImages();
      return state.searchExecuted ? runSearch(false) : ensureEditions(true);
    case "editions":
      await ensureEditions(true);
      if (state.view === "editions") return refreshEditionPage();
      return;
    case "decks":
      return state.openDeckId ? openDeck(state.openDeckId, null) : refreshDeckResults();
    case "perso":
      return personalState.deck ? refreshOpenPersonalDeck() : personalState.refreshList?.(true);
    case "proxy":
      return renderProxies();
    case "archives":
      return renderArchivePage();
  }
}
$("refresh-view").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await refreshVisibleView();
  } finally {
    button.disabled = false;
  }
});
$("clear-search").addEventListener("click", () => { state.searchController?.abort(); state.searchController = null; ++state.searchRequest; $("search-form").reset(); state.searchIdentity = null; for (const id of ["filter-set", "filter-rarity", "filter-type"]) setMultiValue(id, ""); $("filter-color").querySelectorAll(".mana-toggle").forEach(button => setManaPressed(button, false)); state.searchBaseFilters = mainSearchFilters(); state.refineFilters = refineSearchFilters(); state.searchPage = 0; searchDraftTouched = true; persistSearchDraft(); hideRecentSearches(); setText($("search-status"), "Critères effacés. Cliquez sur Rechercher."); });
$("filter-color").querySelectorAll(".mana-toggle").forEach(button => button.addEventListener("click", () => { toggleMana(button, $("filter-color")); searchDraftTouched = true; persistSearchDraft(); setText($("search-status"), "Critères modifiés. Cliquez sur Rechercher pour lancer la recherche."); }));
$("query").addEventListener("focus", renderRecentSearches);
$("query").addEventListener("click", renderRecentSearches);
$("query").addEventListener("blur", event => {
  if (!$("search-suggestions").contains(event.relatedTarget)) hideRecentSearches();
});
$("search-suggestions").addEventListener("focusout", event => {
  if (event.relatedTarget !== $("query") && !$("search-suggestions").contains(event.relatedTarget)) hideRecentSearches();
});
window.addEventListener("blur", hideRecentSearches);
document.addEventListener("visibilitychange", () => { if (document.hidden) hideRecentSearches(); });
$("query").addEventListener("keydown", event => {
  if (event.key === "Escape") { hideRecentSearches(); return; }
  if (event.key === "ArrowDown" && !$("search-suggestions").hidden) {
    const first = $("search-suggestions").querySelector('[role="option"]');
    if (first) { event.preventDefault(); first.focus(); }
  }
});
$("search-suggestions").addEventListener("keydown", event => {
  if (!["ArrowDown", "ArrowUp", "Escape"].includes(event.key)) return;
  if (event.key === "Escape") { event.preventDefault(); hideRecentSearches(); $("query").focus(); return; }
  const options = [...$("search-suggestions").querySelectorAll('[role="option"]')];
  const index = options.indexOf(document.activeElement);
  const next = event.key === "ArrowDown" ? Math.min(options.length - 1, index + 1) : Math.max(0, index - 1);
  if (options[next]) { event.preventDefault(); options[next].focus(); }
});
document.addEventListener("pointerdown", event => { if (!event.target.closest(".search-main")) hideRecentSearches(); });
$("previous-page").addEventListener("click",()=>{state.searchPage=Math.max(0,state.searchPage-1);runSearch();});
$("next-page").addEventListener("click",()=>{state.searchPage++;runSearch();});
$("edition-select").addEventListener("change",()=>{state.exactEditionCode=null;state.editionPage=0;hideEditionSuggestions();renderEditionPage();});
$("edition-query").addEventListener("input",()=>{renderEditionSuggestions();if (state.view === "editions") syncRoute();});
$("edition-query").addEventListener("keydown", event => {
  if (event.key === "Escape") { hideEditionSuggestions(); return; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!editionSuggestionCodes.length || $("edition-suggestions").hidden) return;
    event.preventDefault();
    highlightEditionSuggestion(activeEditionSuggestion + (event.key === "ArrowDown" ? 1 : -1));
  } else if (event.key === "Enter" && activeEditionSuggestion >= 0) {
    event.preventDefault(); chooseEditionSuggestion(editionSuggestionCodes[activeEditionSuggestion]);
  }
});
document.addEventListener("pointerdown", event => { if (!event.target.closest(".edition-search")) hideEditionSuggestions(); });
$("edition-prev").addEventListener("click",()=>{state.editionPage=Math.max(0,state.editionPage-1);renderEditionPage();});
$("edition-next").addEventListener("click",()=>{state.editionPage++;renderEditionPage();});
attachEditionSuggestions("search-edition-query", "search-edition-suggestions", "filter-set", () => { setText($("search-status"), "Édition choisie. Cliquez sur Rechercher."); });
attachEditionSuggestions("deck-edition-query", "deck-edition-suggestions", "deck-edition", () => {});
$("deck-query").addEventListener("input", () => { clearTimeout(deckSuggestionTimer); deckSuggestionTimer = setTimeout(() => { if (!state.openDeckId) renderDeckSuggestions(); }, 180); });
$("deck-submit").addEventListener("click", () => {
  clearTimeout(deckSuggestionTimer); ++state.deckSuggestionRequest;
  hideDeckSuggestions();
  state.deckAppliedFilters = deckResultFilters();
  state.deckExecuted = true;
  state.deckPage = 0;
  void renderDeckPage();
});
for (const id of ["deck-query", "deck-edition-query"]) {
  $(id).addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); $("deck-submit").click(); }
  });
}
$("clear-deck-filters").addEventListener("click", () => {
  for (const id of ["deck-query", "deck-edition-query", "deck-format", "deck-type", "deck-edition", "deck-color-mode", "deck-image-filter", "deck-sort", "deck-order"]) {
    const field = $(id); field.value = field.options ? field.options[0].value : "";
  }
  for (const id of ["deck-edition", "deck-format", "deck-type"]) setMultiValue(id, "");
  document.querySelectorAll(".deck-color-filter .mana-toggle").forEach(button => setManaPressed(button, false));
  clearTimeout(deckSuggestionTimer); ++state.deckSuggestionRequest;
  hideDeckSuggestions();
  state.deckExecuted = false; state.deckAppliedFilters = null; state.deckPage = 0; state.deckTotal = 0;
  state.deckListKey = null; state.deckController?.abort(); state.deckController = null; ++state.deckRequest;
  $("deck-list").replaceChildren();
  $("deck-pager").hidden = true;
  updateBatchButtons();
  if (state.view === "decks") syncRoute();
});
$("deck-query").addEventListener("keydown", event => {
  if (event.key === "Escape") { hideDeckSuggestions(); return; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const options = $("deck-suggestions").querySelectorAll('[role="option"]');
    if (!options.length || $("deck-suggestions").hidden) return;
    event.preventDefault(); highlightDeckSuggestion(activeDeckSuggestion + (event.key === "ArrowDown" ? 1 : -1));
  } else if (event.key === "Enter") {
    event.preventDefault();
    const id = activeDeckSuggestion >= 0 ? deckSuggestionIds[activeDeckSuggestion] : null;
    if (id) { hideDeckSuggestions(); void openDeck(id); }
    else $("deck-submit").click();
  }
});
document.addEventListener("pointerdown", event => { if (!event.target.closest(".deck-search")) hideDeckSuggestions(); });
document.querySelectorAll(".deck-color-filter .mana-toggle").forEach(button=>button.addEventListener("click",()=>{toggleMana(button, document.querySelector(".deck-color-filter"));}));
$("deck-prev").addEventListener("click",()=>{state.deckPage=Math.max(0,state.deckPage-1);renderDeckPage();});
$("deck-next").addEventListener("click",()=>{state.deckPage++;renderDeckPage();});
$("archive-status").addEventListener("change",()=>{archiveResolvedByPage.clear();state.archivePage=0;renderArchivePage();});
$("archive-prev").addEventListener("click",()=>{archiveResolvedByPage.clear();state.archivePage=Math.max(0,state.archivePage-1);renderArchivePage();});
$("archive-next").addEventListener("click",()=>{state.archivePage++;renderArchivePage();});
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
  void galleryNeighbor(event.key === "ArrowLeft" ? -1 : 1).catch(error => console.error("Impossible de changer de carte.", error));
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
const personalState = { editing: false, sectionOrganization: null, deck: null, change: 0, saved: 0, saving: null, error: null, timer: null, listRequest: 0, refreshList: null, drag: null };
const proxyState = { page: 0, total: 0, request: 0, loading: false, deleting: false, snapshot: null };
const PERSONAL_CURRENT_KEY = "database-magic-personal-current-v1";
function personalButton(label, action, primary = false) {
    const b = node("button", primary ? "primary-button" : "secondary-button", label);
    b.type = "button";
    b.addEventListener("click", action);
    return b;
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
async function renderProxies({ quiet = false } = {}) {
    const request = ++proxyState.request;
    const root = $("proxy-content");
    proxyState.loading = true;
    const grid = node("div", "personal-proxy-groups");
    grid.id = "personal-proxy-grid";
    grid.setAttribute("aria-live", "polite");
    const pager = node("div", "pager");
    const changePage = delta => {
        proxyState.page += delta;
        syncRoute();
        void renderProxies();
    };
    const prev = personalButton("Précédent", () => changePage(-1));
    const next = personalButton("Suivant", () => changePage(1));
    const pageLabel = node("span", "muted");
    pager.hidden = true;
    pager.append(prev, pageLabel, next);
    if (!quiet) {
        proxyState.snapshot = null;
        root.replaceChildren(node("p", "muted", "Chargement…"));
    }
    try {
        const data = await getJSON("/api/v2/proxies", { limit: PROXY_PAGE_SIZE, offset: proxyState.page * PROXY_PAGE_SIZE });
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
        if (snapshot === proxyState.snapshot) return;
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
        pager.hidden = pages <= 1;
        prev.disabled = proxyState.page === 0;
        next.disabled = proxyState.page + 1 >= pages;
        setText(pageLabel, `Page ${proxyState.page + 1} sur ${pages} · ${formatNumber(data.total)} proxies`);
        root.replaceChildren(grid, pager);
        proxyState.snapshot = snapshot;
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

$("proxy-open-folder").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const status = $("proxy-folder-status");
    button.disabled = true;
    status.textContent = "";
    try {
        await postJSON("/api/v2/proxies/open-folder", {});
    } catch (error) {
        status.textContent = `Impossible d’ouvrir le dossier des proxies. ${error.message}`;
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
    const prev = personalButton("Précédent", () => void load(page - 1));
    const next = personalButton("Suivant", () => void load(page + 1));
    const pageLabel = node("span", "muted");
    pager.append(prev, pageLabel, next);
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
            editionPageKey = null;
            if (state.searchExecuted && state.searchBaseFilters) void runSearch(false);
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
        const current = new AbortController();
        controller = current;
        enlarged.hidden = true;
        pager.hidden = true;
        results.replaceChildren();
        report("Recherche…");
        try {
            const data = await getJSON("/api/v2/search", { ...filters, official_only: 1, limit: pageSize, offset: requestedPage * pageSize }, { signal: current.signal });
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
            pager.hidden = pages <= 1;
            prev.disabled = page === 0;
            next.disabled = page + 1 >= pages;
            setText(pageLabel, `Page ${page + 1} sur ${pages}`);
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
function personalImage(card, className = "deck-card-image") {
    const path = card?.image_path || card?.image_slots?.find(s => s.status === "ready" && s.face_index === 0)?.relative_path;
    const imageUrl = currentCardImageUrl(card) || (path ? `/assets?${params({ path })}` : "");
    if (!imageUrl)
        return node("div", `${className} deck-card-image-empty`, card ? "Image absente" : "Deck vide");
    const img = node("img", className);
    img.draggable = false;
    img.src = imageUrl;
    img.alt = card.printed_name || card.name || "Carte";
    img.loading = "lazy";
    img.onerror = () => img.replaceWith(node("div", `${className} deck-card-image-empty`, "Image indisponible"));
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
function deckCoverTile(name, colors, cover, open) {
    const tile = personalButton("", open);
    tile.className = "personal-deck-tile";
    tile.append(personalImage(cover, "personal-cover"), node("h2", "", name));
    const symbols = colorSymbols(colors);
    if (symbols) tile.append(symbols);
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
function renderPersonalList() {
    updatePersonalHeading();
    const root = $("personal-content");
    let decks = null, groups = [], organizing = false, importing = false;
    let busy = false, draggedDeck = null, latestLoad = 0;
    const create = personalButton("Créer un deck", renderPersonalCreate, true);
    const actions = node("div", "personal-toolbar");
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
        render();
    });
    organize.setAttribute("aria-pressed", "false");
    importText.disabled = true;
    organize.disabled = true;
    actions.append(create, importText, organize, filePicker);
    const importReport = node("div", "personal-import-report");
    const groupForm = node("form", "personal-section-form");
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
            groups.push(group);
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
            personalStatus("Import du deck…");
            const result = await postJSON("/api/v2/personal-decks/import-text", {
                filename: file.name, text: await readPersonalTextFile(file),
            });
            await load(true);
            const unresolved = result.unresolved || [];
            personalStatus(`Deck « ${result.deck.name} » importé · ${formatNumber(result.imported)} carte(s).`);
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
    const form = node("form", "search-form"), row = node("div", "search-primary-row personal-filter-row");
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
    sort.append(new Option("Ajout récent", "recent"), new Option("Nom du deck", "name"));
    sortLabel.append(sort);
    const orderLabel = node("label", "", "Ordre"), order = node("select");
    order.append(new Option("Décroissant", "desc"), new Option("Croissant", "asc"));
    orderLabel.append(order);
    orderFields.append(sortLabel, orderLabel);
    refine.append(orderFields);
    const controls = node("div", "form-actions");
    const search = node("button", "primary-button", "Rechercher");
    search.type = "submit";
    controls.append(search, personalButton("Effacer les filtres", () => {
        form.reset();
        colors = [];
        colorFilter.querySelectorAll("button").forEach(button => setManaPressed(button, false));
        render();
    }));
    form.append(row, refine, controls);
    const list = node("div", "personal-list-groups");
    root.replaceChildren(actions, importReport, form, groupForm, list);
    personalState.refreshList = (manual = false) => {
        if (list.isConnected && !busy && !importing && (manual || !organizing)) return load(!manual);
    };
    personalStatus("");
    const normalize = value => value.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase("fr").trim();
    function render() {
        if (!decks) return;
        const query = normalize(name.value);
        const items = decks.filter(deck => {
            if (!normalize(deck.name).includes(query)) return false;
            const current = deck.colors || [];
            if (colors.includes("C")) return !current.length;
            return colors.every(color => current.includes(color))
                && (!colors.length || mode.value !== "exact" || colors.length === current.length);
        });
        if (sort.value === "name") {
            items.sort((a, b) => a.name.localeCompare(b.name, "fr") * (order.value === "asc" ? 1 : -1));
        } else if (order.value === "asc") {
            items.reverse();
        }
        list.replaceChildren();
        const groupsById = new Map(groups.map(group => [group.id, []]));
        const ungrouped = [];
        for (const deck of items) (groupsById.get(deck.group_id) || ungrouped).push(deck);
        if (groups.length || organizing) {
            for (const group of groups) list.append(renderGroup(group, groupsById.get(group.id)));
            if (ungrouped.length || organizing) list.append(renderGroup(null, ungrouped));
        } else if (ungrouped.length) {
            const grid = node("div", "personal-deck-grid");
            grid.append(...ungrouped.map(renderDeck));
            list.append(grid);
        }
        if (!items.length && !groups.length && !organizing)
            list.append(node("p", "empty-state", "Aucun deck personnel pour cette sélection."));
    }
    function renderDeck(deck) {
        const entry = node("article", "personal-list-entry");
        const tile = deckCoverTile(deck.name, deck.colors || [], deck.cover_card, () => void openPersonalDeck(deck.id));
        const remove = personalRemoveButton(`Supprimer le deck ${deck.name}`, () => {
            if (!window.confirm(`Supprimer le deck « ${deck.name} » ?`)) return;
            void mutate(async () => {
                await postJSON("/api/v2/personal-decks/delete", { deck_id: deck.id, revision: deck.revision });
                decks = decks.filter(item => item.id !== deck.id);
                personalStatus(`Deck « ${deck.name} » supprimé.`);
            }, remove);
        });
        entry.append(tile, remove);
        if (organizing && groups.length) {
            tile.draggable = true;
            tile.addEventListener("dragstart", event => {
                if (busy) { event.preventDefault(); return; }
                draggedDeck = deck;
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", deck.id);
            });
            tile.addEventListener("dragend", () => { draggedDeck = null; });
            const label = node("label", "personal-deck-group-choice", "Déplacer vers");
            const select = node("select");
            select.setAttribute("aria-label", `Section du deck ${deck.name}`);
            select.append(new Option("Sans section", ""), ...groups.map(group => new Option(group.name, group.id)));
            select.value = deck.group_id || "";
            select.addEventListener("change", () => {
                const next = select.value || null;
                select.value = deck.group_id || "";
                moveDeck(deck, next);
            });
            label.append(select);
            entry.append(label);
        }
        return entry;
    }
    function moveDeck(deck, groupId) {
        if ((deck.group_id || null) === groupId) return;
        void mutate(async () => {
            const result = await postJSON("/api/v2/personal-decks/group-move", { deck_id: deck.id, group_id: groupId });
            deck.group_id = result.group_id;
        });
    }
    function renderGroup(group, items) {
        const section = node("section", "deck-section personal-list-group");
        const heading = node("div", "personal-section-heading");
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
                field.focus();
                field.select();
            });
            const cancel = personalButton("Annuler", () => {
                renameForm.hidden = true;
                heading.hidden = false;
                edit.focus();
            });
            const save = node("button", "primary-button", "Valider");
            save.type = "submit";
            renameForm.append(field, save, cancel);
            renameForm.addEventListener("submit", event => {
                event.preventDefault();
                const name = field.value.trim();
                if (!name) return;
                void mutate(async () => {
                    const result = await postJSON("/api/v2/personal-decks/group-rename", { group_id: group.id, name });
                    group.name = result.group.name;
                }, save);
            });
            const remove = personalButton("Supprimer", () => {
                if (!window.confirm(`Supprimer la section « ${group.name} » ? Ses decks seront déplacés dans « Sans section ».`)) return;
                void mutate(async () => {
                    await postJSON("/api/v2/personal-decks/group-delete", { group_id: group.id });
                    groups = groups.filter(item => item.id !== group.id);
                    for (const deck of decks) if (deck.group_id === group.id) deck.group_id = null;
                }, remove);
            });
            heading.append(edit, remove);
            section.append(renameForm);
        }
        const grid = node("div", "personal-deck-grid");
        grid.append(...items.map(renderDeck));
        if (!items.length) grid.append(node("p", "empty-state", organizing ? "Déposez un deck ici." : "Aucun deck dans cette section."));
        section.append(grid);
        if (organizing) {
            const groupId = group?.id || null;
            section.addEventListener("dragover", event => {
                if (!draggedDeck || (draggedDeck.group_id || null) === groupId) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                section.classList.add("personal-list-drop-target");
            });
            section.addEventListener("dragleave", event => {
                if (!section.contains(event.relatedTarget)) section.classList.remove("personal-list-drop-target");
            });
            section.addEventListener("drop", event => {
                if (!draggedDeck) return;
                event.preventDefault();
                section.classList.remove("personal-list-drop-target");
                moveDeck(draggedDeck, groupId);
                draggedDeck = null;
            });
        }
        return section;
    }
    async function mutate(action, button) {
        if (busy) return;
        busy = true;
        ++latestLoad;
        if (button) button.disabled = true;
        try {
            personalStatus("");
            await action();
            if (list.isConnected) render();
        } catch (error) {
            if (list.isConnected) personalStatus(error.message, true);
        } finally {
            if (button) button.disabled = false;
            busy = false;
        }
    }
    async function load(quiet = false) {
        const request = ++latestLoad;
        if (!quiet) list.replaceChildren(node("p", "muted", "Chargement…"));
        try {
            const response = await getJSON("/api/v2/personal-decks", { summary: 1 });
            if (request !== latestLoad || !list.isConnected) return;
            decks = response.items;
            groups = response.groups || [];
            importText.disabled = false;
            organize.disabled = false;
            render();
        } catch (error) {
            if (request !== latestLoad || !list.isConnected || quiet) return;
            showError(list, error.message);
            list.append(personalButton("Réessayer", () => void load()));
        }
    }
    form.addEventListener("submit", event => { event.preventDefault(); render(); });
    for (const select of [mode, sort, order]) select.addEventListener("change", render);
    name.addEventListener("input", render);
    void load();
}
function renderPersonalCreate() {
    const root = $("personal-content"), form = node("form", "panel personal-create"), label = node("label", "", "Nom du deck"), name = node("input");
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
    const root = $("personal-content");
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
let personalDragFrame = 0;
let personalDragSpeed = 0;
function showPersonalDropIndex(drag, index) {
    if (index === drag.index) return;
    const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const previous = reduceMotion ? [] : drag.tiles.map(tile => tile.getBoundingClientRect());
    drag.animations.forEach(animation => animation.cancel());
    const order = drag.tiles.filter(tile => tile !== drag.tile);
    order.splice(index, 0, drag.tile);
    order.forEach((tile, position) => { tile.style.order = position; });
    drag.index = index;
    drag.animations = [];
    if (reduceMotion) return;
    drag.tiles.forEach((tile, position) => {
        if (tile === drag.tile) return;
        const next = tile.getBoundingClientRect();
        const x = previous[position].left - next.left;
        const y = previous[position].top - next.top;
        if (x || y) drag.animations.push(tile.animate(
            [{ transform: `translate(${x}px, ${y}px)` }, { transform: "none" }],
            { duration: 150, easing: "ease-out" },
        ));
    });
}
function previewPersonalDrop(drag, event) {
    const bounds = drag.grid.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    // Hit-test layout slots rather than cards that are still animating.
    const hovered = drag.tiles.find(tile => x >= tile.offsetLeft && x < tile.offsetLeft + tile.offsetWidth
        && y >= tile.offsetTop && y < tile.offsetTop + tile.offsetHeight);
    if (!hovered || hovered === drag.tile) return;
    const stationary = drag.tiles.filter(tile => tile !== drag.tile);
    const target = stationary.indexOf(hovered);
    const after = x >= hovered.offsetLeft + hovered.offsetWidth / 2;
    showPersonalDropIndex(drag, target + Number(after));
}
function endPersonalDrag() {
    const drag = personalState.drag;
    if (drag) {
        drag.animations.forEach(animation => animation.cancel());
        drag.tiles.forEach(tile => { tile.style.order = ""; });
        drag.tile.classList.remove("personal-card-placeholder");
    }
    personalState.drag = null;
    cancelAnimationFrame(personalDragFrame);
    personalDragFrame = 0;
    personalDragSpeed = 0;
    document.querySelectorAll(".personal-drop-target").forEach(node => node.classList.remove("personal-drop-target"));
}
function scrollPersonalDrag() {
    personalDragFrame = 0;
    if (!personalState.drag || !personalDragSpeed) return;
    window.scrollBy({ top: personalDragSpeed, behavior: "instant" });
    personalDragFrame = requestAnimationFrame(scrollPersonalDrag);
}
document.addEventListener("dragover", event => {
    const drag = personalState.drag;
    if (!drag) return;
    if (!drag.grid.closest(".personal-section").contains(event.target)) {
        showPersonalDropIndex(drag, drag.originalIndex);
    }
    const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
    const edge = 85;
    const up = Math.max(0, 1 - (event.clientY - top) / edge);
    const down = Math.max(0, 1 - (innerHeight - event.clientY) / edge);
    personalDragSpeed = 20 * (Math.min(1, down) - Math.min(1, up));
    if (personalDragSpeed && !personalDragFrame) personalDragFrame = requestAnimationFrame(scrollPersonalDrag);
});
document.addEventListener("drop", endPersonalDrag);
window.addEventListener("blur", endPersonalDrag);
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
function updatePersonalHeading(deck = null, editButton = null) {
    const [eyebrow, heading] = deckTitleContent(
        deck ? "Deck personnel" : "Votre collection de compositions",
        deck?.name || "Deck perso",
        deck ? deck.colors || [] : null,
        "personal-title",
    );
    if (editButton) heading.append(editButton);
    $("personal-heading-content").replaceChildren(eyebrow, heading);
}
function renderPersonalEditor() {
    const deck = personalState.deck, root = $("personal-content"), editing = personalState.editing;
    const sectionOrganization = personalState.sectionOrganization;
    hoverPreview.hidden = true;
    if (!deck)
        return;
    root.replaceChildren();
    const actions = node("div", "personal-toolbar"), back = personalButton("Retour à la liste", async () => {
        back.disabled = true;
        if (await flushPersonal()) {
            personalState.deck = null;
            personalStore(PERSONAL_CURRENT_KEY, null);
            syncRoute();
            renderPersonalList();
        }
        else
            back.disabled = false;
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
        personalState.editing = { name: deck.name, colors: [...(deck.colors || [])] };
        renderPersonalEditor();
        $("personal-content").querySelector(".personal-deck-fields input")?.focus();
    });
    editButton.classList.add("personal-heading-edit");
    editButton.setAttribute("aria-label", "Modifier le nom et les couleurs");
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
    editActions.append(personalButton("Dupliquer", () => duplicatePersonalDeck(deck)), organize);
    actions.append(editActions);
    root.append(actions);
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
            deck.sections.push({ id: crypto.randomUUID(), name: sectionName, cards: [] });
            sectionOrganization.name = "";
            personalChanged();
            renderPersonalEditor();
            $(name.id)?.focus();
        });
        root.append(form);
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
        buttons.append(validate, personalButton("Annuler", finish));
        fields.append(label, personalColorField(editing.colors, colors => { editing.colors = colors; }), buttons);
        fields.addEventListener("submit", event => {
            event.preventDefault();
            const nextName = name.value.trim();
            if (!nextName) { name.focus(); return; }
            deck.name = nextName;
            deck.colors = editing.colors;
            personalChanged();
            finish();
        });
        root.append(fields);
    }
    if (!deck.sections.some(section => section.cards.length)) {
        root.append(node("p", "empty-state", "Ajoutez des cartes depuis Recherche."));
    }
    for (const section of deck.sections) {
        const container = node("section", "deck-section personal-section");
        const heading = node("div", "personal-section-heading");
        const title = deckSectionTitle(section.name, section.cards);
        const updateSectionCount = () => {
            title.textContent = `${section.name} (${deckSectionCount(section.cards)})`;
        };
        const form = node("form", "personal-section-form"), field = node("input");
        field.setAttribute("aria-label", "Nom de la section");
        field.required = true;
        field.maxLength = 200;
        form.hidden = true;
        const setSectionEditing = active => {
            form.hidden = !active;
            heading.hidden = active;
            if (active) { field.value = section.name; field.focus(); field.select(); }
            else modify.focus();
        };
        const modify = personalButton("Modifier", () => setSectionEditing(true));
        modify.setAttribute("aria-label", `Modifier la section ${section.name}`);
        heading.append(title);
        if (sectionOrganization) heading.append(modify);
        const saveSection = node("button", "primary-button", "Valider");
        saveSection.type = "submit";
        form.addEventListener("submit", event => {
            event.preventDefault();
            const nextName = field.value.trim();
            if (!nextName) { field.focus(); return; }
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
        form.append(field, saveSection, personalButton("Annuler", () => setSectionEditing(false)), remove);
        container.append(heading);
        if (sectionOrganization) container.append(form);
        const cards = node("div", "deck-cards personal-cards-edit");
        container.addEventListener("dragover", e => {
            if (personalState.drag) {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                container.classList.add("personal-drop-target");
                if (personalState.drag.sectionId === section.id) previewPersonalDrop(personalState.drag, e);
            }
        });
        container.addEventListener("dragleave", e => {
            if (!container.contains(e.relatedTarget))
                container.classList.remove("personal-drop-target");
        });
        container.addEventListener("drop", e => {
            e.preventDefault();
            container.classList.remove("personal-drop-target");
            if (personalState.drag) {
                const drag = personalState.drag;
                const sameSection = drag.sectionId === section.id;
                const beforeId = sameSection
                    ? drag.tiles.filter(tile => tile !== drag.tile)[drag.index]?.dataset.personalCard
                    : e.target.closest("[data-personal-card]")?.dataset.personalCard;
                endPersonalDrag();
                if (!sameSection || drag.index !== drag.originalIndex) {
                    personalMove(drag.sectionId, drag.cardId, section.id, beforeId);
                }
            }
        });
        for (const card of section.cards) {
            const cardName = card.printed_name || card.name_fr || card.name || "Carte non résolue";
            const preview = personalImage(card);
            const tile = node("article", "deck-card-tile personal-card-edit");
            tile.draggable = true;
            tile.dataset.personalCard = card.id;
            tile.addEventListener("dragstart", e => {
                if (e.target.closest("input,select,.personal-card-remove")) {
                    e.preventDefault();
                    return;
                }
                const tiles = [...cards.children];
                const index = tiles.indexOf(tile);
                const drag = { sectionId: section.id, cardId: card.id, grid: cards, tile, tiles,
                    index, originalIndex: index, animations: [] };
                personalState.drag = drag;
                e.dataTransfer.setData("text/plain", card.id);
                e.dataTransfer.effectAllowed = "move";
                requestAnimationFrame(() => {
                    if (personalState.drag === drag) tile.classList.add("personal-card-placeholder");
                });
            });
            tile.addEventListener("dragend", endPersonalDrag);
            const open = personalButton("", () => void openCard(card.provider_card_id));
            open.className = "personal-card-open";
            open.setAttribute("aria-label", cardName);
            open.draggable = true;
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
        root.append(container);
    }
    const footer = node("div", "personal-deck-footer");
    const top = personalButton("", () => {
        const title = $("personal-title");
        title?.setAttribute("tabindex", "-1");
        title?.focus({ preventScroll: true });
        window.scrollTo({ top: 0 });
    });
    top.classList.add("personal-scroll-top");
    top.title = "Remonter en haut";
    top.setAttribute("aria-label", top.title);
    top.append(cardActionIcon("arrowUp"));
    footer.append(top);
    root.append(footer);
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
    const dialog = node("dialog", "personal-picker"), heading = node("h2", "", "Ajouter à un deck personnel"), status = node("p", "muted", "Chargement…"), list = node("div", "personal-picker-list");
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
void restoreRoute();
