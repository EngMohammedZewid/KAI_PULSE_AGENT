// Small, safe Markdown: only text nodes and known elements are made, never HTML from the answer.
const ARABIC = /[؀-ۿ]/;
const LATIN = /[A-Za-z][\w.+#&/:@-]*(?: [A-Za-z0-9][\w.+#&/:@-]*)*/g;

export function safeHref(raw) {
  try {
    const url = new URL(String(raw).trim());
    return ["http:", "https:", "mailto:", "tel:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

const make = (tag, ...children) => { const node = document.createElement(tag); node.append(...children); return node; };

// Latin names inside Arabic text keep their own order.
function plain(text, arabic) {
  if (!arabic) return [text];
  const out = [];
  let last = 0;
  for (const match of text.matchAll(LATIN)) {
    const run = match[0].replace(/[.,:;!?]+$/, "");
    out.push(text.slice(last, match.index), make("bdi", run));
    last = match.index + run.length;
  }
  return [...out, text.slice(last)];
}

function inline(text, arabic = ARABIC.test(text)) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(/`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g)) {
    out.push(...plain(text.slice(last, m.index), arabic));
    if (m[1]) out.push(make("code", m[1]));
    else if (m[2]) out.push(make("strong", ...inline(m[2], arabic)));
    else {
      const href = safeHref(m[4]);
      const link = make(href ? "a" : "span", ...plain(m[3], arabic));
      if (href) Object.assign(link, { href, target: "_blank", rel: "noopener noreferrer nofollow" });
      out.push(link);
    }
    last = m.index + m[0].length;
  }
  return [...out, ...plain(text.slice(last), arabic)];
}

// While the answer arrives: close an open bold or code mark, hide a half-written link or table row.
function repair(text) {
  if ((text.match(/^```/gm) ?? []).length % 2) return `${text}\n\`\`\``;
  let part = text.replace(/\[[^\]\n]*\]?(\([^)\s]*)?$/, "");
  const lines = part.split("\n");
  if (/^\s*\|/.test(lines.at(-1)) && !lines.at(-1).trimEnd().endsWith("|")) part = lines.slice(0, -1).join("\n");
  const tail = part.slice(part.lastIndexOf("\n\n") + 1);
  if ((tail.match(/`/g) ?? []).length % 2) part += "`";
  if ((tail.replace(/`[^`]*`/g, "").match(/\*\*/g) ?? []).length % 2) part += "**";
  return part;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());

export function markdown(source, streaming = false) {
  const root = make("div");
  root.className = "kc-md";
  const lines = (streaming ? repair(source) : source).split("\n");
  const auto = (node) => { node.dir = "auto"; root.append(node); return node; };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    let match;
    if (!line.trim()) continue;
    if (line.startsWith("```")) {
      const code = [];
      for (i += 1; i < lines.length && !lines[i].startsWith("```"); i += 1) code.push(lines[i]);
      root.append(make("pre", make("code", code.join("\n"))));
    } else if ((match = /^(#{1,4})\s+(.*)/.exec(line))) {
      auto(make(`h${match[1].length}`, ...inline(match[2])));
    } else if (/^\s*\|/.test(line)) {
      const rows = [];
      for (; i < lines.length && /^\s*\|/.test(lines[i]); i += 1) rows.push(lines[i]);
      i -= 1;
      if (rows.length < 2 || !/^[\s|:-]+$/.test(rows[1])) continue; // the table is still arriving
      const row = (tag, text) => make("tr", ...cells(text).map((cell) => make(tag, ...inline(cell))));
      const scroll = auto(make("div", make("table", make("thead", row("th", rows[0])), make("tbody", ...rows.slice(2).map((r) => row("td", r))))));
      scroll.className = "kc-table-scroll";
    } else if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = auto(make(ordered ? "ol" : "ul"));
      for (; i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i]); i += 1) {
        const item = make("li", ...inline(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, "")));
        item.dir = "auto";
        list.append(item);
      }
      i -= 1;
    } else if (line.startsWith(">")) {
      auto(make("blockquote", ...inline(line.replace(/^>\s?/, ""))));
    } else {
      const text = [line];
      while (i + 1 < lines.length && lines[i + 1].trim() && !/^(```|#{1,4}\s|\s*\||\s*[-*]\s|\s*\d+[.)]\s|>)/.test(lines[i + 1])) text.push(lines[(i += 1)]);
      auto(make("p", ...inline(text.join(" "))));
    }
  }
  return root;
}
