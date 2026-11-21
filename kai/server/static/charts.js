// Interactive charts and summary cards for the KAI Pulse chat page.
// The server sends each chart as plain data (kind, labels, numbers); all
// formatting and colours live here, so nothing from the model runs as code.
// The QuickChart PNG stays available ("Image") and is what reports use.
(() => {
  "use strict";
  const LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
  const DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"];
  // A series keeps the colour of its meaning in every chart: income blue, spending orange...
  const MEANING = [
    [/income|revenue|salary|credit|دخل|إيراد|راتب/i, 0],
    [/spend|expense|cost|debit|مصروف|مصاريف|إنفاق/i, 1],
    [/saving|ادخار|مدخرات|توفير/i, 2],
    [/debt|loan|installment|دين|قرض|قسط/i, 3],
  ];
  const ARABIC = /[؀-ۿ]/;
  const live = new Set();

  const dark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
  const ink = () => (dark() ? { text: "#e6e9ef", muted: "#9aa3b2", grid: "#2b323d", tip: "#1a1f27" }
                            : { text: "#1d2330", muted: "#5b6475", grid: "#e6e9ef", tip: "#ffffff" });
  const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
  const money = (v, unit) => {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    const s = num.format(Math.abs(v));
    return (v < 0 ? "-" : "") + (unit ? unit + " " : "") + s;
  };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function palette(labels) {
    const colors = dark() ? DARK : LIGHT, used = new Set(), out = [];
    labels.forEach((label) => {
      const hit = MEANING.find(([re, i]) => re.test(label || "") && !used.has(i));
      out.push(hit ? hit[1] : -1);
      if (hit) used.add(hit[1]);
    });
    let next = 0;
    return out.map((i) => {
      if (i >= 0) return colors[i];
      while (used.has(next % colors.length) && used.size < colors.length) next++;
      used.add(next % colors.length);
      return colors[next++ % colors.length];
    });
  }

  function legend(chart, items, round) {
    const box = el("div", "chart-legend");
    items.forEach((item, i) => {
      const b = el("button", "chart-key");
      b.type = "button";
      const sw = el("span", "chart-swatch");
      sw.style.background = item.color;
      if (round) sw.style.borderRadius = "50%";
      b.append(sw, document.createTextNode(item.label));
      b.title = "Show or hide";
      b.onclick = () => {
        if (round) chart.toggleDataVisibility(i);
        else chart.setDatasetVisibility(i, !chart.isDatasetVisible(i));
        b.classList.toggle("off");
        chart.update();
      };
      box.appendChild(b);
    });
    return box;
  }

  function config(spec) {
    const c = ink(), unit = spec.unit || "", round = spec.kind === "pie" || spec.kind === "doughnut";
    const rtl = ARABIC.test([spec.title, ...spec.labels, ...spec.datasets.map((d) => d.label)].join(" "));
    const colors = round ? palette(spec.labels) : palette(spec.datasets.map((d) => d.label));
    const datasets = spec.datasets.map((d, i) => {
      const kind = d.kind || spec.kind;
      const base = { label: d.label || spec.title || "Value", data: d.data };
      if (round) {
        return { ...base, backgroundColor: colors, borderColor: dark() ? "#1a1f27" : "#ffffff",
                 borderWidth: 2, hoverOffset: 8 };
      }
      if (kind === "line") {
        return { ...base, type: "line", borderColor: colors[i], backgroundColor: colors[i] + "22",
                 fill: !!d.area, tension: 0.3, borderWidth: 2, pointRadius: d.data.length > 40 ? 0 : 3,
                 pointHoverRadius: 6, pointBackgroundColor: colors[i] };
      }
      return { ...base, type: "bar", backgroundColor: colors[i], hoverBackgroundColor: colors[i],
               borderRadius: 4, borderSkipped: "start", maxBarThickness: 28 };
    });
    const total = (ctx) => ctx.dataset.data.reduce((s, v, j) =>
      s + (v && ctx.chart.getDataVisibility(j) ? v : 0), 0);
    const tooltip = {
      rtl, textDirection: rtl ? "rtl" : "ltr",
      backgroundColor: c.tip, titleColor: c.text, bodyColor: c.text, footerColor: c.muted,
      borderColor: c.grid, borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
      callbacks: {
        label: (ctx) => {
          const v = round ? ctx.parsed : ctx.parsed[spec.horizontal ? "x" : "y"];
          const name = round ? ctx.label : ctx.dataset.label;
          const u = unit && String(name || "").includes(unit) ? "" : unit;  // "Income (EGP)" already says it
          if (round) {
            const t = total(ctx);
            return ` ${name}: ${money(v, u)}${t ? ` (${(v / t * 100).toFixed(1)}%)` : ""}`;
          }
          return ` ${name}: ${money(v, u)}`;
        },
        footer: (items) => {
          if (round || items.length < 2 || !spec.stacked) return "";
          const sum = items.reduce((s, it) => s + (it.parsed[spec.horizontal ? "x" : "y"] || 0), 0);
          return `Total: ${money(sum, unit)}`;
        },
      },
    };
    const options = {
      responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      interaction: round ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip },
    };
    if (!round) {
      const valueAxis = {
        stacked: !!spec.stacked, beginAtZero: true, grid: { color: c.grid }, border: { display: false },
        ticks: { color: c.muted, callback: (v) => compact.format(v) },
      };
      const labelAxis = {
        stacked: !!spec.stacked, grid: { display: false }, border: { color: c.grid },
        ticks: { color: c.muted, maxRotation: 45, autoSkip: spec.labels.length > 14 },
        reverse: rtl && !spec.horizontal,
      };
      options.indexAxis = spec.horizontal ? "y" : "x";
      options.scales = spec.horizontal ? { x: valueAxis, y: labelAxis } : { x: labelAxis, y: valueAxis };
    }
    const type = round ? spec.kind : (spec.datasets.every((d) => (d.kind || spec.kind) === "line") ? "line" : "bar");
    return { cfg: { type, data: { labels: spec.labels, datasets }, options }, colors, round, rtl };
  }

  function card(spec, url) {
    const wrap = el("figure", "chart-card");
    if (spec.title) wrap.appendChild(el("figcaption", "chart-title", spec.title));
    const area = el("div", "chart-area");
    const round = spec.kind === "pie" || spec.kind === "doughnut";
    const bars = spec.horizontal ? spec.labels.length : 0;
    area.style.height = (round ? 260 : Math.max(280, bars * 32 + 80)) + "px";
    const canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", spec.title || "Chart");
    canvas.textContent = spec.datasets.map((d) => `${d.label}: ` +
      spec.labels.map((l, i) => `${l} ${money(d.data[i], spec.unit)}`).join(", ")).join(". ");
    area.appendChild(canvas);
    wrap.appendChild(area);

    const { cfg, colors, rtl } = config(spec);
    const chart = new window.Chart(canvas, cfg);
    live.add(chart);
    const items = round ? spec.labels.map((l, i) => ({ label: l, color: colors[i] }))
                        : spec.datasets.map((d, i) => ({ label: d.label || spec.title, color: colors[i] }));
    if (items.length > 1) wrap.insertBefore(legend(chart, items, round), area);
    if (rtl) wrap.dir = "rtl";

    const actions = el("div", "chart-actions");
    const img = el("a", "", "Image");
    img.href = url; img.target = "_blank"; img.rel = "noopener";
    actions.appendChild(img);
    wrap.appendChild(actions);
    return wrap;
  }

  function kpiCards(k, arabic) {
    const L = arabic
      ? { income: "متوسط الدخل الشهري", spending: "متوسط المصروفات", savings: "متوسط الادخار", dti: "نسبة الدين للدخل" }
      : { income: "Avg monthly income", spending: "Avg spending", savings: "Avg savings", dti: "Debt-to-income" };
    const box = el("div", "kpis");
    if (arabic) box.dir = "rtl";
    const whole = (v) => money(Math.round(Number(v) || 0), k.currency);  // headline numbers, no decimals
    [["income", whole(k.income)], ["spending", whole(k.spending)],
     ["savings", whole(k.savings)], ["dti", `${num.format(k.dti)}%`]].forEach(([key, value]) => {
      const c = el("div", "kpi");
      c.append(el("div", "kpi-label", L[key]), el("div", "kpi-value", value));
      if (key === "savings" && k.savings < 0) c.classList.add("bad");
      if (key === "dti" && k.dti > 40) c.classList.add("bad");
      box.appendChild(c);
    });
    return box;
  }

  // Replace each chart image in a rendered reply with its interactive version.
  function enhance(div, m) {
    if (m.kpis && typeof m.kpis === "object") {
      div.insertBefore(kpiCards(m.kpis, ARABIC.test(m.content || "")), div.firstChild);
    }
    if (!window.Chart || !Array.isArray(m.charts)) return;
    for (const { url, spec } of m.charts) {
      if (!spec || !Array.isArray(spec.labels) || !Array.isArray(spec.datasets)) continue;
      const img = [...div.querySelectorAll("img")].find((i) => i.getAttribute("src") === url);
      if (!img) continue;
      try {
        img.replaceWith(card(spec, url));
        // Any other link to the same image ("Download chart", "تحميل الرسم"...) is now the card's "Image" action.
        [...div.querySelectorAll("a")].filter((a) => a.getAttribute("href") === url &&
          !a.closest(".chart-card")).forEach((a) => {
          const p = a.parentElement;
          a.remove();
          if (p && p !== div && !p.textContent.replace(/[\s⬇️]/g, "")) p.remove();
        });
      } catch (e) {
        console.warn("Interactive chart failed; keeping the image.", e);
      }
    }
  }

  function reset() {
    live.forEach((c) => c.destroy());
    live.clear();
  }

  window.KaiCharts = { enhance, reset };
})();
