// KAI Pulse connection: sends the message to the KAI Pulse server and yields the chat's events.
//   { type: "step", id, title, status, ms }      while the agent works
//   { type: "meta", meta: { index } }             the server's index of the reply (for thumbs up/down)
//   { type: "text", text }                        the reply, in Markdown
//   { type: "block", block }                      options ("PDF or Word?") and document cards
// Throw to show the error bar with "Try again". The API token never appears in these files:
// it is pasted by the user at sign-in and kept only in this browser.
import { api, session } from "./session.mjs";
import { TEXTS } from "./texts.mjs";

const FORMAT_OPTIONS = { en: ["PDF", "Word (.docx)"], ar: ["PDF", "Word (.docx)"] };
const FILE_LINK = /\[([^\]]+)\]\(((?:https?|file):\/\/[^)\s]+\.(pdf|docx|xlsx|csv)(?:\?[^)\s]*)?)\)/gi;

/** The agent writes some HTML (chart images, links); the chat shows Markdown only.
 *  Links to the charts in `hide` are dropped: those charts are drawn as interactive cards. */
export function toMarkdown(content, hide = new Set()) {
  const drop = (url, keep) => (hide.has(url.replace(/&amp;/g, "&")) ? "" : keep);
  return String(content || "")
    .replace(/<img\s[^>]*?src="([^"]+)"[^>]*\/?>/gi, (m, src) => drop(src, m))
    .replace(/<a\s[^>]*?href="([^"]+)"[^>]*>[\s\S]*?<\/a>/gi, (m, href) => drop(href, m))
    .replace(/!?\[[^\]]*\]\(([^)\s]+)\)/g, (m, url) => drop(url, m))
    .split("\n").filter((line, i, all) => line.trim() || (i > 0 && all[i - 1].trim())).join("\n")
    .replace(/<img\s[^>]*?src="([^"]+)"[^>]*?alt="([^"]*)"[^>]*\/?>/gi, (_m, src, alt) => `[${alt || "Chart"}](${src})`)
    .replace(/<img\s[^>]*?src="([^"]+)"[^>]*\/?>/gi, (_m, src) => `[Chart](${src})`)
    .replace(/<a\s[^>]*?href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (_m, href, text) => `[${text.replace(/<[^>]+>/g, "").trim() || href}](${href})`)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt, src) => `[${alt || "Chart"}](${src})`)
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/&amp;/g, "&").replace(/&#11015;&#xFE0F;/g, "⬇️");
}

/** Download links to generated files become document cards under the answer. */
export function documents(markdownText) {
  const seen = new Set();
  const cards = [];
  for (const m of markdownText.matchAll(FILE_LINK)) {
    if (seen.has(m[2])) continue;
    seen.add(m[2]);
    cards.push({ type: "document", title: m[1].replace(/^[^\p{L}\p{N}]+/u, "").trim() || m[2], format: m[3].toUpperCase(), url: m[2] });
  }
  return cards;
}

/** One server reply as the chat's text and blocks: summary cards, interactive charts, documents. */
export function answerParts(m) {
  const charts = (m.charts || []).filter((c) => c.spec);
  const text = toMarkdown(m.content, new Set(charts.map((c) => c.url)));
  const blocks = [];
  if (m.kpis) blocks.push({ type: "kpis", kpis: m.kpis });
  for (const c of charts) blocks.push({ type: "chart", url: c.url, spec: c.spec });
  blocks.push(...documents(text));
  return { text, blocks };
}

export async function* reply({ text, locale, signal, files = [] }) {
  const title = TEXTS[locale].ui.working;
  const started = performance.now();
  yield { type: "step", id: 1, title, status: "running" };
  if (!session.thread) session.thread = (await api("/api/threads", { method: "POST", json: {}, signal })).id;
  const form = new FormData();
  form.append("text", text);
  for (const file of files) form.append("files", file, file.name);
  const out = await api(`/api/threads/${session.thread}/messages`, { method: "POST", body: form, signal });
  yield { type: "step", id: 1, title, status: "done", ms: performance.now() - started };

  const replies = out.messages || [];
  const last = replies.at(-1);
  if (last && Number.isInteger(last.index)) yield { type: "meta", meta: { index: last.index } };
  const parts = replies.map(answerParts);
  const body = parts.map((p) => p.text).filter(Boolean).join("\n\n");
  if (body) yield { type: "text", text: body };
  for (const block of parts.flatMap((p) => p.blocks)) yield { type: "block", block };
  if (out.interrupt) {
    yield { type: "block", block: { type: "options", prompt: out.interrupt.question || "", options: FORMAT_OPTIONS[locale] } };
  }
}
