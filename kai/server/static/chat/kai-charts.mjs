// KAI Pulse: interactive charts and summary cards inside answers, in the chat's own look.
// The server sends each chart as plain data (kind, labels, numbers); colours come from the chat's
// tokens (chat.css), so both themes work. Containers are the chat's cards (.kc-card) and chips
// (.kc-chip); nothing from the answer is ever put in the page as HTML.
const ARABIC = /[؀-ۿ]/;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "class") node.className = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// A series keeps the colour of its meaning in every chart.
const MEANING = [
  [/income|revenue|salary|credit|دخل|إيراد|راتب/i, "--kc-primary"],
  [/spend|expense|cost|debit|مصروف|مصاريف|إنفاق/i, "--kc-accent"],
  [/saving|ادخار|مدخرات|توفير/i, "--kc-success"],
  [/debt|loan|installment|دين|قرض|قسط/i, "--kc-warning"],
];
// The chat's own colours, in order, for series without a meaning (tile colours from texts.mjs).
const ORDER = ["--kc-primary", "--kc-accent", "#10B4F8", "--kc-success", "#1477F8", "--kc-warning", "#7B12EE", "--kc-muted"];
const colour = (name) => (name.startsWith("--") ? token(name) : name);

function palette(labels) {
  const used = new Set();
  const picked = labels.map((label) => {
    const hit = MEANING.find(([re, name]) => re.test(label || "") && !used.has(name));
    if (hit) used.add(hit[1]);
    return hit ? hit[1] : null;
  });
  let next = 0;
  return picked.map((name) => {
    if (name) return colour(name);
    while (used.has(ORDER[next % ORDER.length]) && used.size < ORDER.length) next += 1;
    const pick = ORDER[next % ORDER.length];
    used.add(pick);
    next += 1;
    return colour(pick);
  });
}

const formatter = (locale) => new Intl.NumberFormat(locale === "ar" ? "ar-EG" : "en-GB", { maximumFractionDigits: 2 });
const compact = (locale) => new Intl.NumberFormat(locale === "ar" ? "ar-EG" : "en-GB", { notation: "compact", maximumFractionDigits: 1 });
function money(value, unit, locale) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value < 0 ? "-" : ""}${unit ? `${unit} ` : ""}${formatter(locale).format(Math.abs(value))}`;
}

function config(spec, locale) {
  const round = spec.kind === "pie" || spec.kind === "doughnut";
  const rtl = locale === "ar" || ARABIC.test([spec.title, ...spec.labels].join(" "));
  const colors = round ? palette(spec.labels) : palette(spec.datasets.map((d) => d.label));
  const ink = token("--kc-on-surface");
  const muted = token("--kc-muted");
  const line = token("--kc-border");
  const surface = token("--kc-surface");
  const font = { family: token("--kc-font") || "system-ui", size: 12 };
  const unit = spec.unit || "";
  const datasets = spec.datasets.map((d, i) => {
    const base = { label: d.label || spec.title || "Value", data: d.data };
    const kind = d.kind || spec.kind;
    if (round) return { ...base, backgroundColor: colors, borderColor: surface, borderWidth: 2, hoverOffset: 8 };
    if (kind === "line") {
      return { ...base, type: "line", borderColor: colors[i], backgroundColor: `${colors[i]}22`, fill: Boolean(d.area),
        tension: 0.3, borderWidth: 2, pointRadius: d.data.length > 40 ? 0 : 3, pointHoverRadius: 6, pointBackgroundColor: colors[i] };
    }
    return { ...base, type: "bar", backgroundColor: colors[i], hoverBackgroundColor: colors[i], borderRadius: 6, borderSkipped: "start", maxBarThickness: 28 };
  });
  const visibleTotal = (ctx) => ctx.dataset.data.reduce((sum, v, j) => sum + (v && ctx.chart.getDataVisibility(j) ? v : 0), 0);
  const tooltip = {
    rtl, textDirection: rtl ? "rtl" : "ltr", backgroundColor: surface, titleColor: ink, bodyColor: ink, footerColor: muted,
    borderColor: line, borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true, titleFont: { ...font, weight: "600" }, bodyFont: font,
    callbacks: {
      label: (ctx) => {
        const v = round ? ctx.parsed : ctx.parsed[spec.horizontal ? "x" : "y"];
        const name = round ? ctx.label : ctx.dataset.label;
        const u = unit && String(name || "").includes(unit) ? "" : unit;
        if (round) {
          const t = visibleTotal(ctx);
          return ` ${name}: ${money(v, u, locale)}${t ? ` (${formatter(locale).format((v / t) * 100)}%)` : ""}`;
        }
        return ` ${name}: ${money(v, u, locale)}`;
      },
    },
  };
  const options = {
    responsive: true, maintainAspectRatio: false, animation: { duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 500 },
    interaction: round ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip },
  };
  if (!round) {
    const values = { stacked: Boolean(spec.stacked), beginAtZero: true, grid: { color: line }, border: { display: false },
      ticks: { color: muted, font, callback: (v) => compact(locale).format(v) } };
    const labels = { stacked: Boolean(spec.stacked), grid: { display: false }, border: { color: line },
      ticks: { color: muted, font, maxRotation: 45, autoSkip: spec.labels.length > 14 }, reverse: rtl && !spec.horizontal };
    options.indexAxis = spec.horizontal ? "y" : "x";
    options.scales = spec.horizontal ? { x: values, y: labels } : { x: labels, y: values };
  }
  const type = round ? spec.kind : spec.datasets.every((d) => (d.kind || spec.kind) === "line") ? "line" : "bar";
  return { cfg: { type, data: { labels: spec.labels, datasets }, options }, colors, round, rtl };
}

const live = new Set();
function tidy() { // charts whose answer was redrawn are no longer in the page
  for (const chart of live) if (!chart.canvas.isConnected) { chart.destroy(); live.delete(chart); }
}

/** One chart as a chat card: title, series chips (press to hide), the chart, and a link to the image. */
export function chartCard(spec, url, { locale, imageLabel, hideLabel }) {
  setTimeout(tidy, 0); // after this redraw is in the page, so cards still being built are kept
  const round = spec.kind === "pie" || spec.kind === "doughnut";
  const area = el("div");
  area.style.position = "relative";
  area.style.blockSize = `${round ? 240 : Math.max(240, (spec.horizontal ? spec.labels.length * 30 : 0) + 80)}px`;
  const canvas = el("canvas", { role: "img", "aria-label": spec.title || imageLabel });
  canvas.textContent = spec.datasets.map((d) => `${d.label}: ${spec.labels.map((l, i) => `${l} ${money(d.data[i], spec.unit, locale)}`).join(", ")}`).join(". ");
  area.append(canvas);
  const card = el("article", { class: "kc-card" }, spec.title && el("p", { class: "kc-card-title", dir: "auto" }, spec.title));
  if (!window.Chart) return null; // the chart library did not load: the answer keeps its link instead
  const { cfg, colors, rtl } = config(spec, locale);
  const chart = new window.Chart(canvas, cfg);
  live.add(chart);
  const items = round ? spec.labels.map((label, i) => ({ label, color: colors[i] })) : spec.datasets.map((d, i) => ({ label: d.label || spec.title, color: colors[i] }));
  if (items.length > 1) {
    card.append(el("div", { class: "kc-options-list", role: "group" }, items.map((item, i) => {
      const dot = el("span", { "aria-hidden": "true" });
      Object.assign(dot.style, { display: "inline-block", inlineSize: "10px", blockSize: "10px", borderRadius: "50%", background: item.color, marginInlineEnd: "6px" });
      const chip = el("button", { type: "button", class: "kc-chip", dir: "auto", "aria-pressed": "true", title: hideLabel }, dot, item.label);
      chip.addEventListener("click", () => {
        if (round) chart.toggleDataVisibility(i); else chart.setDatasetVisibility(i, !chart.isDatasetVisible(i));
        const shown = round ? chart.getDataVisibility(i) : chart.isDatasetVisible(i);
        chip.setAttribute("aria-pressed", String(shown));
        chip.style.opacity = shown ? "" : "0.5";
        chart.update();
      });
      return chip;
    })));
  }
  card.append(area);
  if (rtl) card.dir = "rtl";
  card.append(el("div", { class: "kc-card-actions" }, el("a", { class: "kc-card-button", href: url, target: "_blank", rel: "noopener noreferrer nofollow" }, imageLabel)));
  return el("section", { class: "kc-cards" }, card);
}

/** The analysis summary as four chat cards: income, spending, savings, debt-to-income. */
export function kpiCards(k, { locale, labels }) {
  const whole = (v) => money(Math.round(Number(v) || 0), k.currency, locale);
  const items = [["income", whole(k.income)], ["spending", whole(k.spending)], ["savings", whole(k.savings)],
    ["dti", `${formatter(locale).format(Number(k.dti) || 0)}%`]];
  const grid = el("section", { class: "kc-cards", "aria-label": labels.summary },
    items.map(([key, value]) => {
      const bad = (key === "savings" && k.savings < 0) || (key === "dti" && k.dti > 40);
      const number = el("p", { class: "kc-card-title", dir: "ltr" }, value);
      Object.assign(number.style, { fontSize: "1.25em", fontVariantNumeric: "tabular-nums", margin: "0" });
      if (bad) number.style.color = token("--kc-danger");
      return el("article", { class: "kc-card" }, el("p", { class: "kc-card-muted", dir: "auto" }, labels[key]), number);
    }));
  grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(130px, 1fr))";
  return grid;
}
