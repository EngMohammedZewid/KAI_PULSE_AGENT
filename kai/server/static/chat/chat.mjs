// The chat window: header, welcome with topics, messages, cards and the message box.
// KAI Pulse changes are marked "KAI Pulse": sign-in, the server message index, feedback, new chat.
import { icon, topicIcon } from "./icons.mjs";
import { markdown, safeHref } from "./markdown.mjs";
import { reply } from "./reply.mjs";
import { session, signIn, signOut, api, onSessionChange, newConversation, rate as sendRating } from "./session.mjs"; // KAI Pulse
import { answerParts, isDownload } from "./reply.mjs"; // KAI Pulse
import { download, SignedOut } from "./session.mjs"; // KAI Pulse
import { chartCard, kpiCards } from "./kai-charts.mjs"; // KAI Pulse
import { TEXTS, CONTACT_URL } from "./texts.mjs";

const NS = "my-chat";
const MAX_CHARS = 4000;
const query = new URLSearchParams(location.search);
const embedded = query.get("embedded") === "1" && parent !== window;
const store = (key, value) => { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { /* storage can be blocked */ } return null; };
const calm = matchMedia("(prefers-reduced-motion: reduce)");
const app = document.getElementById("root");

let locale = [store("chat_lang"), query.get("locale"), "en"].find((value) => value in TEXTS);
let theme = ["light", "dark"].find((value) => value === (store("chat_theme") ?? query.get("theme"))) ?? "light";
let messages = [];   // { role, text, blocks, steps, ms, status, rating }
let run = null;      // the AbortController of the answer being written
let error = false;
let topic = 0;
let maximized = false;
let view = "chat";   // KAI Pulse: "chat" or "history"
let files = [];      // KAI Pulse: files picked for the next message
const t = (key, vars = {}) => TEXTS[locale].ui[key].replace(/\{(\w+)\}/g, (_m, name) => vars[name] ?? "");
const copy = () => TEXTS[locale].copy;
const number = (value, digits = 0) => new Intl.NumberFormat(locale === "ar" ? "ar-EG" : "en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "class") node.className = value;
    else if (name === "color") node.style.setProperty("--kc-topic", value);
    else node.setAttribute(name, value === true ? "" : value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}
const rise = (node, delay = 0) => {
  if (!calm.matches) node.animate([{ opacity: 0, transform: "translateY(8px) scale(0.98)" }, { opacity: 1, transform: "none" }], { duration: 320, delay, easing: "cubic-bezier(0.22, 1.2, 0.36, 1)", fill: "backwards" });
  return node;
};
const pop = (node) => {
  if (!calm.matches) node.animate([{ opacity: 0, transform: "scale(0.6)" }, { opacity: 1, transform: "none" }], { duration: 300, easing: "cubic-bezier(0.34, 1.56, 0.64, 1)" });
  return node;
};
const iconButton = (name, label, click, extra = {}) => el("button", { type: "button", class: "kc-icon-btn", "aria-label": label, title: label, onclick: click, ...extra }, icon(name));
const avatar = (size) => el("img", { class: "kc-avatar", src: "./assets/kai-logo.png", alt: "", width: size, height: size });
const toHost = (type, data = {}) => { if (embedded) parent.postMessage({ ns: NS, type, ...data }, location.origin); };

/* ---- Parts that stay ---- */
const header = el("header");
const scroll = el("div", { class: "kc-scroll" });
const notices = el("div");
const live = el("div", { class: "kc-sr-only", "aria-live": "polite", "aria-atomic": "true" });
const input = el("textarea", { id: "kc-input", rows: 1, dir: "auto" });
const inputLabel = el("label", { for: "kc-input", class: "kc-sr-only" });
const ACCEPT = ".csv,.xlsx,.xls,.pdf,.txt,.md,.json,.xml,.yaml,.yml,.png,.jpg,.jpeg,.gif"; // KAI Pulse: what the server takes
const picker = el("input", { type: "file", multiple: true, accept: ACCEPT, hidden: true, onchange: () => { files = [...files, ...picker.files].slice(0, 10); picker.value = ""; drawFiles(); updateInput(); input.focus(); } });
const attach = el("button", { type: "button", class: "kc-icon-btn kc-small", onclick: () => picker.click() }, icon("attach", 18));
const picked = el("div", { class: "kc-options-list", hidden: true });
function drawFiles() { // KAI Pulse: the picked files as chips; pressing one removes it
  picked.hidden = !files.length;
  picked.replaceChildren(...files.map((file, at) => el("button", { type: "button", class: "kc-chip", dir: "auto", "aria-label": t("removeFile", { name: file.name }), title: t("removeFile", { name: file.name }),
    onclick: () => { files.splice(at, 1); drawFiles(); updateInput(); input.focus(); } }, icon("attach", 14), file.name, icon("close", 14))));
}
const pill = el("div", { class: "kc-pill" }, el("span", { class: "kc-sparkle", "aria-hidden": "true" }, icon("sparkle")), inputLabel, input, attach, picker);
const sendSlot = el("div", { class: "kc-input-wrap kc-apart" }, pill);
const limit = el("p", { class: "kc-limit", id: "kc-limit", hidden: true });
const composer = el("form", { class: "kc-composer", onsubmit: (event) => { event.preventDefault(); submit(); } }, picked, sendSlot, limit);
const footer = el("div", { class: "kc-footer kc-footer-brand" });
const shell = el("div", { class: "kc-app kc-layout-float kc-turns-bubble", "data-send": "apart", "data-welcome": "hero", "data-header": "float" },
  header, el("div", { class: "kc-split" }, el("div", { class: "kc-chat" }, el("main", { class: "kc-main" }, scroll), notices, composer, footer)), live);

/* ---- Header ---- */
function drawHeader() {
  const over = messages.length === 0 && view === "chat";
  const focused = header.contains(document.activeElement) ? [...header.querySelectorAll(".kc-header-actions > *")].indexOf(document.activeElement) : -1;
  header.className = `kc-header kc-header-float${over ? " kc-header-over" : ""}`;
  const other = locale === "en" ? { code: "ar", mark: "ع", name: "العربية" } : { code: "en", mark: "EN", name: "English" };
  const switchTo = t("switch", { language: other.name });
  const contact = safeHref(CONTACT_URL);
  header.replaceChildren(
    ...(over ? [el("h1", { class: "kc-title kc-sr-only" }, "KAI")] : [avatar(32), el("div", { class: "kc-title-block" }, el("h1", { class: "kc-title" }, el("bdi", {}, "KAI")), el("p", { class: "kc-subtitle", dir: "auto" }, copy().subtitle))]),
    el("div", { class: "kc-header-actions" },
      contact && el("a", { class: "kc-icon-btn kc-contact", href: contact, target: "_blank", rel: "noopener noreferrer nofollow", "aria-label": t("contact"), title: t("contact") }, icon("contact")),
      el("button", { type: "button", class: "kc-icon-btn kc-lang", lang: other.code, "aria-label": switchTo, title: switchTo, onclick: () => setLocale(other.code) }, other.mark),
      iconButton(theme === "dark" ? "sun" : "moon", t(theme === "dark" ? "themeLight" : "themeDark"), () => setTheme(theme === "dark" ? "light" : "dark")),
      session.token && iconButton("history", t("history"), openHistory), // KAI Pulse
      !over && session.token && iconButton("new", t("newChat"), newChat),
      embedded && !shell.matches('[data-compact="true"]') && iconButton(maximized ? "collapse" : "expand", t(maximized ? "restore" : "maximize"), () => { maximized = !maximized; toHost("maximize", { on: maximized }); drawHeader(); }),
      embedded && iconButton("close", t("close"), () => toHost("close"))));
  if (focused >= 0) header.querySelectorAll(".kc-header-actions > *")[focused]?.focus();
}

/* ---- Welcome ---- */
function questionCard(item, focus) {
  const title = el("h3", { id: "kc-topic-card-title", tabindex: -1, class: "kc-topic-card-title", dir: "auto" }, el("span", { class: "kc-tile-icon" }, topicIcon(item.icon)), item.title);
  const card = el("section", { class: "kc-topic-card", "aria-labelledby": "kc-topic-card-title", color: item.color }, title,
    el("ul", {}, item.questions.map((row) => el("li", {}, el("button", { type: "button", class: "kc-question-row", dir: "auto", onclick: () => send(row.prompt || row.label) },
      el("span", { class: "kc-row-icon" }, topicIcon(row.icon, 22)),
      el("span", { class: "kc-row-text" }, el("strong", {}, row.label), row.description && el("small", {}, row.description)),
      el("span", { class: "kc-chevron" }, icon("forward")))))));
  if (focus) requestAnimationFrame(() => { card.scrollIntoView({ behavior: calm.matches ? "auto" : "smooth", block: "nearest" }); title.focus({ preventScroll: true }); });
  return rise(card);
}
function welcome() {
  const text = copy();
  const tiles = text.topics.map((item, index) => el("button", { type: "button", class: `kc-tile${index === topic ? " kc-tile-active" : ""}`, color: item.color, "aria-pressed": String(index === topic), dir: "auto", onclick: () => pick(index) },
    el("span", { class: "kc-tile-icon" }, topicIcon(item.icon)),
    el("span", { class: "kc-tile-text" }, el("strong", {}, item.title), el("small", {}, item.subtitle)),
    el("span", { class: "kc-chevron" }, icon("forward"))));
  const pick = (index) => {
    topic = index;
    tiles.forEach((tile, at) => { tile.classList.toggle("kc-tile-active", at === index); tile.setAttribute("aria-pressed", String(at === index)); });
    section.lastElementChild.replaceWith(questionCard(text.topics[index], true));
  };
  const section = el("section", { class: "kc-welcome kc-welcome-hero", "aria-labelledby": "kc-welcome-title" },
    rise(el("div", { class: "kc-hero kc-hero-with-art" },
      el("img", { class: "kc-hero-art", src: "./assets/kai-hero.webp", alt: "" }),
      el("div", { class: "kc-hero-copy" },
        el("div", { class: "kc-hero-brand" }, el("img", { class: "kc-hero-logo kc-hero-logo-icon", src: "./assets/kai-logo.png", alt: "" }), el("p", { class: "kc-hero-tagline", dir: "auto" }, text.subtitle)),
        el("div", { class: "kc-hero-bubble" },
          el("p", { class: "kc-hero-greeting", dir: "auto" }, text.greeting),
          el("h2", { id: "kc-welcome-title", class: "kc-welcome-title", dir: "auto" }, text.welcome_title),
          el("p", { class: "kc-welcome-body", dir: "auto" }, text.welcome_body))))),
    rise(el("section", { class: "kc-explore", "aria-labelledby": "kc-explore-title" },
      el("h3", { id: "kc-explore-title", class: "kc-explore-title" }, t("explore")),
      el("div", { class: "kc-tile-grid", role: "group", "aria-labelledby": "kc-explore-title" }, tiles)), 35),
    questionCard(text.topics[topic], false));
  return section;
}

/* ---- Sign-in (KAI Pulse): the welcome hero with the chat's own message box ---- */
function signInView() {
  const text = copy();
  const field = el("input", { id: "kc-token", type: "password", autocomplete: "off", spellcheck: "false", dir: "ltr", placeholder: t("tokenHint") });
  const problem = el("div");
  const button = el("button", { type: "submit", class: "kc-send", "aria-label": t("signIn"), title: t("signIn") }, icon("arrowUp", 26));
  const form = el("form", { class: "kc-input-wrap kc-apart", onsubmit: async (event) => {
    event.preventDefault();
    button.disabled = true;
    const ok = await signIn(field.value).catch(() => false);
    button.disabled = false;
    if (!ok) {
      problem.replaceChildren(el("div", { class: "kc-error", role: "alert" }, icon("alert", 18), el("p", {}, t("signInError"))));
      field.focus();
    }
  } },
  el("div", { class: "kc-pill" }, el("span", { class: "kc-sparkle", "aria-hidden": "true" }, icon("sparkle")),
    el("label", { for: "kc-token", class: "kc-sr-only" }, t("token")), field),
  button);
  requestAnimationFrame(() => field.focus());
  return el("section", { class: "kc-welcome kc-welcome-hero", "aria-labelledby": "kc-signin-title" },
    rise(el("div", { class: "kc-hero kc-hero-with-art" },
      el("img", { class: "kc-hero-art", src: "./assets/kai-hero.webp", alt: "" }),
      el("div", { class: "kc-hero-copy" },
        el("div", { class: "kc-hero-brand" }, el("img", { class: "kc-hero-logo kc-hero-logo-icon", src: "./assets/kai-logo.png", alt: "" }), el("p", { class: "kc-hero-tagline", dir: "auto" }, text.subtitle)),
        el("div", { class: "kc-hero-bubble" },
          el("p", { class: "kc-hero-greeting", dir: "auto" }, text.greeting),
          el("h2", { id: "kc-signin-title", class: "kc-welcome-title", dir: "auto" }, t("signInTitle")),
          el("p", { class: "kc-welcome-body", dir: "auto" }, t("signInBody")))))),
    rise(form, 35), problem);
}

/* ---- History (KAI Pulse): past conversations as rows of the topic card ---- */
const when = (iso) => new Intl.DateTimeFormat(locale === "ar" ? "ar-EG" : "en-GB", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
function historyView() {
  const rows = el("ul", {}, el("li", {}, el("p", { class: "kc-status-line" }, el("span", { class: "kc-typing", "aria-hidden": "true" }, el("i"), el("i"), el("i")), el("span", {}, t("loading")))));
  const title = el("h3", { id: "kc-history-title", tabindex: -1, class: "kc-topic-card-title", dir: "auto" }, el("span", { class: "kc-tile-icon" }, icon("history", 24)), t("history"));
  const card = el("section", { class: "kc-topic-card", "aria-labelledby": "kc-history-title", color: "#1477F8" }, title, rows,
    el("div", { class: "kc-card-actions" },
      el("button", { type: "button", class: "kc-link-btn", onclick: () => { view = "chat"; drawHeader(); drawMain(); input.focus(); } }, t("back")),
      el("button", { type: "button", class: "kc-link-btn", onclick: () => signOut() }, t("signOut"))));
  api("/api/threads").then((threads) => {
    rows.replaceChildren(...(threads.length ? threads.map((thread) => el("li", {}, el("button", { type: "button", class: "kc-question-row", dir: "auto", onclick: () => openThread(thread.id) },
      el("span", { class: "kc-row-icon" }, topicIcon("chat", 22)),
      el("span", { class: "kc-row-text" }, el("strong", {}, thread.title), el("small", {}, when(thread.updated_at))),
      el("span", { class: "kc-chevron" }, icon("forward"))))) : [el("li", {}, el("p", { class: "kc-welcome-body", dir: "auto" }, t("historyEmpty")))]));
  }).catch(() => rows.replaceChildren(el("li", {}, el("div", { class: "kc-error", role: "alert" }, icon("alert", 18), el("p", {}, t("error"))))));
  requestAnimationFrame(() => title.focus({ preventScroll: true }));
  return el("section", { class: "kc-welcome kc-welcome-hero" }, rise(card));
}
function openHistory() {
  if (run) return;
  view = "history";
  drawHeader(); drawMain();
}
async function openThread(id) {
  const data = await api(`/api/threads/${id}`).catch(() => null);
  if (!data) { error = true; drawNotices(); return; }
  const ratings = data.feedback || {};
  messages = data.messages.map((m) => (m.role === "user"
    ? { role: "user", text: m.content, files: m.attachments || [] }
    : { role: "assistant", ...answerParts(m), steps: [], status: "complete", index: m.index,
        rating: ratings[m.index] === 1 ? "up" : ratings[m.index] === -1 ? "down" : undefined }));
  if (data.interrupt) messages.push({ role: "assistant", text: "", steps: [], status: "complete", blocks: [{ type: "options", prompt: data.interrupt.question || "", options: ["PDF", "Word (.docx)"] }] });
  session.thread = id;
  view = "chat";
  error = false;
  drawHeader(); drawMain(); drawSend(); drawNotices();
  input.focus();
}

/* ---- Messages ---- */
const glyph = (status) => (status === "done" ? icon("check", 14) : status === "failed" ? icon("alert", 18) : el("span", { class: "kc-spinner", "aria-hidden": "true" }));
const seconds = (ms) => t("seconds", { seconds: number(ms / 1000, 1) });
const stepRow = (step, time) => el("li", { class: `kc-step kc-step-${step.status}` }, glyph(step.status), el("span", { dir: "auto" }, step.title), time && step.ms !== undefined && el("span", { class: "kc-step-time" }, seconds(step.ms)));
function trace(message) {
  const list = el("ol", { class: "kc-steps", hidden: true }, message.steps.map((step) => stepRow(step, true)));
  const count = message.steps.length;
  const toggle = el("button", { type: "button", class: "kc-trace-toggle", "aria-expanded": "false", onclick: () => {
    list.hidden = !list.hidden;
    toggle.setAttribute("aria-expanded", String(!list.hidden));
    box.classList.toggle("kc-trace-open", !list.hidden);
  } }, el("span", { class: "kc-trace-summary" }, `${t(count === 1 ? "step1" : count === 2 ? "step2" : "steps", { count: number(count) })} · ${seconds(message.ms)}`), icon("chevron", 14));
  const box = el("div", { class: "kc-trace" }, toggle, list);
  return box;
}
function block(item, last) {
  const off = !last || run !== null;
  const link = (text, href, primary) => el("a", { class: `kc-card-button${primary ? " kc-card-primary" : ""}`, href, target: "_blank", rel: "noopener noreferrer nofollow" }, text, icon("link", 14));
  if (item.type === "options") return el("div", { class: "kc-options" }, item.prompt && el("p", { class: "kc-options-prompt", dir: "auto" }, item.prompt),
    el("div", { class: "kc-options-list", role: "group" }, item.options.map((option) => el("button", { type: "button", class: "kc-chip", dir: "auto", disabled: off, onclick: () => send(option) }, option))));
  if (item.type === "citations") return el("div", { class: "kc-citations" }, el("p", { class: "kc-citations-title" }, t("sources")),
    el("ol", {}, item.items.map((source) => el("li", {}, safeHref(source.url) ? el("a", { href: safeHref(source.url), target: "_blank", rel: "noopener noreferrer nofollow", dir: "auto" }, source.title) : el("span", { dir: "auto" }, source.title)))));
  const docCard = (...actions) => el("section", { class: "kc-cards" }, el("article", { class: "kc-card kc-doc" },
    el("div", { class: "kc-doc-head" }, el("span", { class: "kc-doc-badge", "aria-hidden": "true" }, item.format || icon("file", 16)),
      el("div", { class: "kc-doc-names" }, el("p", { class: "kc-card-title", dir: "auto" }, item.title), item.detail && el("p", { class: "kc-card-muted" }, el("bdi", {}, item.detail)))),
    ...actions));
  if (item.type === "document" && isDownload(item.url)) { // KAI Pulse: protected download with the sign-in token
    const status = el("p", { class: "kc-card-muted", role: "status", dir: "auto" });
    const button = el("button", { type: "button", class: "kc-card-button kc-card-primary", onclick: () => {
      button.disabled = true;
      status.textContent = "";
      download(item.url, item.title).catch((failure) => { if (!(failure instanceof SignedOut)) status.textContent = t("downloadFailed"); })
        .finally(() => { button.disabled = false; });
    } }, t("download"), icon("file", 14));
    return docCard(el("div", { class: "kc-card-actions" }, button), status);
  }
  if (item.type === "document" && safeHref(item.url)) return docCard(el("div", { class: "kc-card-actions" }, link(t("open"), safeHref(item.url), true)));
  if (item.type === "kpis") return kpiCards(item.kpis, { locale, labels: { summary: t("summary"), income: t("kpiIncome"), spending: t("kpiSpending"), savings: t("kpiSavings"), dti: t("kpiDti") } }); // KAI Pulse
  if (item.type === "chart") return chartCard(item.spec, item.url, { locale, imageLabel: t("chartImage"), hideLabel: t("chartToggle") }); // KAI Pulse
  if (item.type === "cta" && last) {
    const card = el("section", { class: "kc-cta" }, el("p", { class: "kc-cta-title" }, t("ctaTitle")), el("div", { class: "kc-card-actions" },
      el("button", { type: "button", class: "kc-card-button kc-card-primary", disabled: off, onclick: () => send(t("ctaMessage")) }, t("ctaBook")),
      safeHref(CONTACT_URL) && link(t("ctaContact"), safeHref(CONTACT_URL)),
      el("button", { type: "button", class: "kc-link-btn kc-cta-dismiss", onclick: () => { item.type = "gone"; card.remove(); input.focus(); } }, t("notNow"))));
    return card;
  }
  return null;
}
function feedback(message) {
  const box = el("div", { class: "kc-feedback", role: "group" });
  const done = () => el("p", { class: "kc-feedback-thanks", role: "status" }, icon("check", 14), ` ${t("thanks")}`);
  if (message.rating) return done();
  const rate = (value) => { message.rating = value; box.replaceWith(pop(done())); sendRating(message.index, value).catch(() => {}); }; // KAI Pulse
  box.append(iconButton("up", t("helpful"), () => rate("up")), iconButton("down", t("notHelpful"), () => rate("down")));
  box.querySelectorAll("button").forEach((button) => button.classList.add("kc-small"));
  return box;
}
function messageView(message, last) {
  if (message.role === "user") return el("li", { class: "kc-msg kc-msg-user", "aria-label": t("you") }, el("div", { class: "kc-msg-body" }, el("div", { class: "kc-bubble" }, el("p", { class: "kc-user-text", dir: "auto" }, message.text || "")),
    message.files?.length ? el("p", { class: "kc-msg-meta", dir: "auto" }, `${t("attached")}: `, el("bdi", {}, message.files.join(", "))) : null)); // KAI Pulse
  const writing = message.status === "writing";
  const body = el("div", { class: "kc-msg-body" });
  if (message.text || (!writing && message.steps.length)) {
    const top = message.blocks.filter((item) => item.type === "kpis").map((item) => block(item, last)); // KAI Pulse: summary cards open the answer
    const cards = message.blocks.filter((item) => item.type !== "cta" && item.type !== "kpis").map((item) => block(item, last));
    body.append(el("div", { class: `kc-bubble${writing ? " kc-streaming" : ""}` }, ...[!writing && message.steps.length ? trace(message) : null, ...top, message.text ? markdown(message.text, writing) : null, ...cards].filter(Boolean)));
  }
  if (writing) body.append(message.steps.length
    ? el("ol", { class: "kc-steps kc-steps-live" }, message.steps.map((step) => stepRow(step)))
    : el("p", { class: "kc-status-line" }, el("span", { class: "kc-typing", "aria-hidden": "true" }, el("i"), el("i"), el("i")), el("span", {}, t("thinking"))));
  const cta = message.blocks.find((item) => item.type === "cta");
  const ctaCard = cta && !writing ? block(cta, last) : null;
  if (ctaCard) body.append(ctaCard);
  if (message.status === "stopped") body.append(el("p", { class: "kc-msg-meta" }, t("stopped")));
  if (message.status === "failed" && !message.text) body.append(el("p", { class: "kc-msg-meta" }, t("failed")));
  if (message.status === "complete") body.append(feedback(message));
  return el("li", { class: "kc-msg kc-msg-assistant" }, avatar(28), body);
}
let list = null;
let stick = true;
scroll.addEventListener("scroll", () => { stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80; }, { passive: true });
const toEnd = () => { if (stick) scroll.scrollTop = scroll.scrollHeight; };
function drawMain() {
  composer.hidden = !session.token; // KAI Pulse: signed out shows the sign-in card instead
  if (!session.token) { list = null; scroll.replaceChildren(signInView()); scroll.scrollTop = 0; return; }
  if (view === "history") { list = null; scroll.replaceChildren(historyView()); scroll.scrollTop = 0; return; } // KAI Pulse
  if (messages.length === 0) { list = null; scroll.replaceChildren(welcome()); scroll.scrollTop = 0; return; }
  list = el("ol", { class: "kc-messages", "aria-label": t("conversation") }, messages.map((message, index) => messageView(message, index === messages.length - 1)));
  scroll.replaceChildren(list);
  toEnd();
}
function redrawLast() { list.lastElementChild.replaceWith(messageView(messages.at(-1), true)); toEnd(); }

/* ---- Message box, notices, footer ---- */
function drawSend() {
  sendSlot.querySelector(".kc-send")?.remove();
  sendSlot.append(run
    ? el("button", { type: "button", class: "kc-send kc-stop", "aria-label": t("stop"), title: t("stop"), onclick: () => run?.abort() }, icon("stop"))
    : el("button", { type: "submit", class: "kc-send", "aria-label": t("send"), title: t("send") }, icon("arrowUp", 26)));
  if (run === null) updateInput();
}
function updateInput() {
  input.style.height = "auto";
  input.dir = input.value ? "auto" : document.documentElement.dir; // the hint follows the page, the text its own language
  if (input.value) input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  const left = MAX_CHARS - input.value.length;
  limit.hidden = left >= MAX_CHARS * 0.2;
  limit.textContent = t("limit", { count: number(Math.max(left, 0)) });
  limit.classList.toggle("kc-limit-over", left < 0);
  sendSlot.classList.toggle("kc-invalid", left < 0);
  const button = sendSlot.querySelector(".kc-send:not(.kc-stop)");
  if (button) button.disabled = (!input.value.trim() && !files.length) || left < 0 || !navigator.onLine; // KAI Pulse: files alone can be sent
}
function drawNotices() {
  notices.replaceChildren();
  if (!navigator.onLine) notices.append(el("p", { class: "kc-offline", role: "status" }, t("offline")));
  if (error) notices.append(el("div", { class: "kc-error", role: "alert" }, icon("alert", 18), el("p", {}, t("error")),
    el("button", { type: "button", class: "kc-link-btn", onclick: retry }, t("retry")),
    iconButton("close", t("dismiss"), () => { error = false; drawNotices(); }, { class: "kc-icon-btn kc-small" })));
}
function drawStatic() {
  inputLabel.textContent = t("message");
  attach.setAttribute("aria-label", t("attach")); attach.title = t("attach"); // KAI Pulse
  input.placeholder = copy().placeholder;
  footer.replaceChildren(
    el("span", { class: "kc-footer-parts", dir: "auto" }, copy().footer.split(" | ").map((part) => el("span", { class: "kc-footer-part" }, part.split("**").map((piece, at) => (at % 2 ? el("strong", {}, piece) : piece))))),
    el("span", { class: "kc-footer-logo kc-footer-logo-themed" }, el("img", { src: "./assets/isys-logo-blue.png", alt: "", class: "kc-for-light" }), el("img", { src: "./assets/isys-logo.png", alt: "", class: "kc-for-dark" })));
}

/* ---- Actions ---- */
async function answer(text, attached = []) {
  const controller = new AbortController();
  const message = { role: "assistant", text: "", blocks: [], steps: [], status: "writing" };
  const history = messages.slice(0, -1).map(({ role, text: said }) => ({ role, text: said }));
  const started = performance.now();
  run = controller;
  error = false;
  messages.push(message);
  stick = true;
  drawHeader(); drawMain(); drawSend(); drawNotices();
  list.querySelectorAll(".kc-msg").forEach((node, at, all) => { if (at >= all.length - 2) rise(node); });
  try {
    for await (const event of reply({ text, locale, history, signal: controller.signal, files: attached })) {
      if (controller.signal.aborted) break;
      if (event.type === "text") message.text += event.text;
      else if (event.type === "block") message.blocks.push(event.block);
      else if (event.type === "meta") Object.assign(message, event.meta); // KAI Pulse
      else if (event.type === "step") {
        const at = message.steps.findIndex((step) => step.id === event.id);
        if (at < 0) message.steps.push({ ...event }); else message.steps[at] = { ...event };
      }
      if (messages.at(-1) === message) redrawLast();
    }
    message.status = controller.signal.aborted ? "stopped" : "complete";
  } catch {
    message.status = controller.signal.aborted ? "stopped" : "failed";
    if (run === controller) error = message.status === "failed";
  }
  message.ms = performance.now() - started;
  message.steps.forEach((step) => { if (step.status === "running") step.status = message.status === "complete" ? "done" : "failed"; });
  if (run !== controller) return; // a new chat was started meanwhile
  run = null;
  redrawLast(); drawSend(); drawNotices();
  live.textContent = message.status === "complete" ? `${t("newMessage")} ${message.text}` : "";
}
function send(text, attached = []) {
  const value = text.trim();
  if ((!value && !attached.length) || run || value.length > MAX_CHARS || !navigator.onLine) return;
  view = "chat"; // KAI Pulse
  messages.push({ role: "user", text: value, files: attached.map((file) => file.name) });
  void answer(value, attached);
  input.focus();
}
function retry() {
  const question = messages.findLast((message) => message.role === "user");
  if (!question || run) return;
  messages = messages.slice(0, messages.lastIndexOf(question) + 1);
  void answer(question.text);
}
function submit() {
  const value = input.value;
  if (run || (!value.trim() && !files.length) || value.length > MAX_CHARS || !navigator.onLine) return;
  const sending = files; // KAI Pulse
  files = [];
  drawFiles();
  input.value = "";
  updateInput();
  send(value, sending);
}
function newChat() {
  const current = run;
  run = null;
  current?.abort();
  messages = [];
  error = false;
  newConversation(); // KAI Pulse
  view = "chat";
  drawHeader(); drawMain(); drawSend(); drawNotices();
  input.focus();
}
function setLocale(next) {
  locale = next;
  store("chat_lang", next);
  drawAll();
}
function setTheme(next) {
  theme = next;
  store("chat_theme", next);
  document.documentElement.dataset.theme = next;
  drawHeader();
  drawMain(); // KAI Pulse: charts take the new theme's colours
}
function drawAll() {
  Object.assign(document.documentElement, { lang: locale, dir: locale === "ar" ? "rtl" : "ltr" });
  document.documentElement.dataset.theme = theme;
  document.title = "KAI Pulse";
  drawStatic(); drawHeader(); drawMain(); drawSend(); drawNotices();
}

input.addEventListener("input", updateInput);
input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); } });
for (const name of ["online", "offline"]) addEventListener(name, () => { drawNotices(); updateInput(); });
addEventListener("keydown", (event) => {
  if (!embedded) return;
  if (event.key === "Escape") toHost("close");
  if (event.key === "Tab" && shell.dataset.compact === "true") { // covering the phone's screen: Tab stays inside
    const items = [...document.querySelectorAll("button:not([disabled]), a[href], textarea")].filter((node) => node.offsetParent);
    const edge = event.shiftKey ? items[0] : items.at(-1);
    if (document.activeElement === edge) { event.preventDefault(); (event.shiftKey ? items.at(-1) : items[0]).focus(); }
  }
});
addEventListener("message", (event) => {
  if (!embedded || event.source !== parent || event.origin !== location.origin || event.data?.ns !== NS) return;
  shell.dataset.compact = String(Boolean(event.data.compact));
  drawHeader();
  if (event.data.type === "open") input.focus();
});
onSessionChange(() => { messages = []; files = []; view = "chat"; run?.abort(); run = null; error = false; drawFiles(); drawAll(); }); // KAI Pulse
app.append(shell);
drawAll();
toHost("ready");
