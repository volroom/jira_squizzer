import chalk, { type ChalkInstance } from "chalk";
import stringWidth from "string-width";
import wrapAnsi from "wrap-ansi";
import type { Client, Column, Config, Issue, Named, PR, Transition } from "./jira.js";

// group buckets issues into lanes by status; issues in unmapped statuses are dropped, as on the Jira board.
export function group(cols: Column[], issues: Issue[]): Issue[][] {
  const lanes: Issue[][] = cols.map(() => []);
  for (const is of issues) {
    const i = cols.findIndex((c) => c.statuses.includes(is.fields.status.id));
    if (i >= 0) lanes[i].push(is);
  }
  return lanes;
}

export function findTransition(ts: Transition[], statuses: string[]): Transition | undefined {
  return ts.find((t) => statuses.includes(t.to.id));
}

export interface Board {
  sprint: string;
  cols: Column[];
  lanes: Issue[][];
}

export interface Detail {
  issue: Issue;
  prs: PR[];
  prErr: Error | null;
}

// Msg and Cmd mirror bubbletea: a Cmd is async work whose result Msg is fed back into update().
export type Msg =
  | { kind: "board"; b?: Board; err?: Error }
  | { kind: "detail"; d?: Detail; err?: Error }
  | { kind: "moved"; key?: string; to?: string; err?: Error };
export type Cmd = (() => Promise<Msg>) | "quit" | null;

async function fetchBoard(c: Client, cfg: Config): Promise<Board> {
  const id = cfg.board_id || (await c.boardID(cfg.project));
  const cols = await c.columns(id);
  const [sid, name] = await c.activeSprint(id);
  const issues = await c.sprintIssues(sid);
  return { sprint: name, cols, lanes: group(cols, issues) };
}

export class Model {
  c?: Client;
  cfg: Config = { base_url: "", username: "", project: "", api_key: "" };
  b: Board = { sprint: "", cols: [], lanes: [] };
  col = 0;
  row: number[] = []; // cursor per lane
  detail: Detail | null = null;
  scroll = 0;
  pendingG = false;
  search: string | null = null; // non-null while typing a "/" ticket search
  query = ""; // active filter on the board
  all: Issue[][] = []; // unfiltered lanes; b.lanes is the filtered view
  follow = ""; // issue key to put the cursor on after the next reload
  busy = false;
  msg = "";
  w = 0;
  h = 0;

  loadBoard(): Cmd {
    const { c, cfg } = this;
    return async () => {
      try {
        return { kind: "board", b: await fetchBoard(c!, cfg) };
      } catch (err) {
        return { kind: "board", err: err as Error };
      }
    };
  }

  loadDetail(key: string): Cmd {
    const c = this.c;
    return async () => {
      try {
        const issue = await c!.issue(key);
        const [prs, prErr] = await c!.prs(issue.id);
        return { kind: "detail", d: { issue, prs, prErr } };
      } catch (err) {
        return { kind: "detail", err: err as Error };
      }
    };
  }

  move(is: Issue, target: Column): Cmd {
    const c = this.c;
    return async () => {
      try {
        const t = findTransition(await c!.transitions(is.key), target.statuses);
        if (!t) return { kind: "moved", err: new Error(`${is.key}: no workflow transition into ${JSON.stringify(target.name)}`) };
        await c!.transition(is.key, t.id);
        return { kind: "moved", key: is.key, to: target.name };
      } catch (err) {
        return { kind: "moved", err: err as Error };
      }
    };
  }

  init(): Cmd {
    return this.loadBoard();
  }

  update(msg: Msg): Cmd {
    switch (msg.kind) {
      case "board":
        this.busy = false;
        if (msg.err) {
          this.msg = msg.err.message;
          return null;
        }
        this.b = msg.b!;
        this.all = msg.b!.lanes;
        if (this.row.length !== this.b.lanes.length) this.row = this.b.lanes.map(() => 0);
        this.filter(this.query);
        if (this.follow === "") this.msg = "";
        this.focus(this.follow);
        this.follow = "";
        return null;
      case "detail":
        this.busy = false;
        if (msg.err) {
          this.msg = msg.err.message;
          return null;
        }
        this.msg = "";
        this.detail = msg.d!;
        this.scroll = 0;
        return null;
      case "moved":
        if (msg.err) {
          this.busy = false;
          this.msg = msg.err.message;
          return null;
        }
        this.msg = msg.key + " → " + msg.to;
        this.follow = msg.key!;
        return this.loadBoard();
    }
  }

  // focus puts the board cursor on key, if it's on the board.
  focus(key: string) {
    this.b.lanes.forEach((lane, i) =>
      lane.forEach((is, j) => {
        if (is.key === key) {
          this.col = i;
          this.row[i] = j;
        }
      }),
    );
  }

  // filter shows only tickets whose key or title contain q (case-insensitive).
  filter(q: string) {
    this.query = q;
    q = q.trim().toLowerCase();
    this.b.lanes = this.all.map((lane) => lane.filter((is) => (is.key + " " + is.fields.summary).toLowerCase().includes(q)));
    for (let i = 0; i < this.row.length; i++) this.row[i] = clamp(this.row[i], 0, this.b.lanes[i].length - 1);
    this.col = clamp(this.col, 0, this.b.lanes.length - 1);
    if (this.b.lanes.length > 0 && this.b.lanes[this.col].length === 0) {
      // hop to a lane that still has tickets
      const i = this.b.lanes.findIndex((lane) => lane.length > 0);
      if (i >= 0) this.col = i;
    }
  }

  count(): number {
    return this.b.lanes.reduce((n, lane) => n + lane.length, 0);
  }

  key(k: string): Cmd {
    if (k === "ctrl+c") return "quit";
    if (this.search !== null) {
      let q = this.search;
      switch (k) {
        case "esc":
          this.search = null;
          q = "";
          break;
        case "backspace":
          q = q.slice(0, -1);
          break;
        case "enter": {
          this.search = null;
          const key = ticketKey(this.cfg.project, q);
          if (this.count() === 0 && key !== "") {
            // not on the sprint board: clear the filter and open it straight from Jira
            this.filter("");
            this.busy = true;
            this.msg = "loading " + key + "…";
            return this.loadDetail(key);
          }
          return null;
        }
        default:
          if (Buffer.byteLength(k) === 1) q += k;
      }
      if (this.search !== null) this.search = q;
      this.filter(q);
      return null;
    }
    const g = this.pendingG;
    this.pendingG = false;

    if (this.detail) {
      switch (k) {
        case "q":
        case "esc":
        case "h":
          this.detail = null;
          return null;
        case "j":
        case "down":
          this.scroll++;
          break;
        case "k":
        case "up":
          this.scroll--;
          break;
        case "ctrl+d":
          this.scroll += Math.trunc(this.h / 2);
          break;
        case "ctrl+u":
          this.scroll -= Math.trunc(this.h / 2);
          break;
        case "G":
          this.scroll = 1 << 30;
          break;
        case "g":
          if (g) this.scroll = 0;
          else this.pendingG = true;
      }
      this.scroll = clamp(this.scroll, 0, this.detailLines().length - this.h + 1);
      return null;
    }

    if (k === "q") return "quit";
    if (this.busy) return null;
    if (k === "r") {
      this.busy = true;
      this.msg = "reloading…";
      return this.loadBoard();
    }
    if (k === "/") {
      this.search = this.query;
      this.msg = "";
      return null;
    }
    if (k === "esc" && this.query !== "") {
      this.filter("");
      return null;
    }
    if (this.b.lanes.length === 0) return null;
    const lane = this.b.lanes[this.col];
    switch (k) {
      case "h":
      case "left":
      case "l":
      case "right": {
        const t = k === "l" || k === "right" ? this.col + 1 : this.col - 1;
        if (t >= 0 && t < this.b.lanes.length) {
          this.row[t] = this.sameHeight(t);
          this.col = t;
        }
        break;
      }
      case "j":
      case "down":
        this.row[this.col]++;
        break;
      case "k":
      case "up":
        this.row[this.col]--;
        break;
      case "G":
        this.row[this.col] = lane.length - 1;
        break;
      case "g":
        if (g) this.row[this.col] = 0;
        else this.pendingG = true;
        break;
      case "enter":
      case "o":
        if (lane.length > 0) {
          const key = lane[this.row[this.col]].key;
          this.busy = true;
          this.msg = "loading " + key + "…";
          return this.loadDetail(key);
        }
        break;
      case "H":
      case "L": {
        const t = k === "L" ? this.col + 1 : this.col - 1;
        if (lane.length > 0 && t >= 0 && t < this.b.cols.length) {
          const is = lane[this.row[this.col]];
          this.busy = true;
          this.msg = "moving " + is.key + "…";
          return this.move(is, this.b.cols[t]);
        }
      }
    }
    this.col = clamp(this.col, 0, this.b.lanes.length - 1);
    this.row[this.col] = clamp(this.row[this.col], 0, this.b.lanes[this.col].length - 1);
    return null;
  }

  footer(help: string): string {
    if (this.search !== null) {
      return msgStyle("/" + this.search) + faint(`█  ${this.count()} shown · number or title · enter keep · esc clear`);
    }
    if (this.msg) return msgStyle(this.msg);
    if (this.query && !this.detail) {
      return msgStyle(`filter ${JSON.stringify(this.query)} · ${this.count()} shown · / edit · esc clear`);
    }
    return faint(help);
  }

  view(): string {
    if (this.w === 0) return "";
    if (this.detail) {
      const lines = this.detailLines();
      const body = lines.slice(this.scroll, Math.min(lines.length, this.scroll + this.h - 1));
      while (body.length < this.h - 1) body.push("");
      return body.join("\n") + "\n" + this.footer(detailHelp);
    }

    const header = title(this.cfg.project + " · " + this.b.sprint);
    if (this.b.lanes.length === 0) return header + "\n\n" + this.footer(boardHelp);
    const [cw, ch] = this.dims();
    const boxes = this.b.lanes.map((lane, i) => {
      const rows = [heading(trunc(`${this.b.cols[i].name} (${lane.length})`, cw))];
      const cards = this.laneCards(i, cw);
      const start = scrollStart(cards, this.row[i], ch);
      const lines: string[] = [];
      for (let j = start; j < lane.length && lines.length < ch; j++) {
        if (j > start) lines.push("");
        lines.push(...cards[j]);
      }
      rows.push(...lines.slice(0, ch));
      return box(rows, cw, ch + 1, i === this.col ? activeBorder : laneBorder);
    });
    const joined = boxes[0].map((_, y) => boxes.map((b) => b[y]).join(""));
    return header + "\n" + joined.join("\n") + "\n" + this.footer(boardHelp);
  }

  detailLines(): string[] {
    const d = this.detail!;
    const f = d.issue.fields;
    let b = `${keyStyle(d.issue.key)}  ${title(f.summary)}\n`;
    b += `${name(f.issuetype)} · ${f.status.name} · ${name(f.priority)}\n`;
    b += `Assignee: ${name(f.assignee)}   Reporter: ${name(f.reporter)}\n`;
    b += faint(this.cfg.base_url.replace(/\/+$/, "") + "/browse/" + d.issue.key) + "\n\n";
    b += heading("Pull requests") + "\n";
    if (d.prErr) b += `  couldn't load: ${d.prErr.message}\n`;
    else if (d.prs.length === 0) b += "  none\n";
    for (const pr of d.prs) {
      const colour = prColours[pr.status];
      const status = colour ? chalk.hex(colour)("[" + pr.status + "]") : "[" + pr.status + "]";
      b += `  ${status} ${pr.repositoryName} ${pr.id} ${pr.name}\n    ${faint(pr.url)}\n`;
      const a = approvals(pr);
      if (a) b += "    " + a + "\n";
    }
    const desc = (f.description ?? "").replaceAll("\r", "") || faint("(no description)");
    b += "\n" + heading("Description") + "\n" + desc;
    return wrapAnsi(text(b), Math.max(this.w - 1, 20), { hard: true, trim: false }).split("\n");
  }

  // dims returns the card width and the number of card lines visible per lane.
  dims(): [number, number] {
    return [
      Math.max(Math.trunc(this.w / Math.max(this.b.lanes.length, 1)) - 4, 5), // 4 = border + padding
      Math.max(this.h - 5, 1), // title, footer, borders, lane header
    ];
  }

  laneCards(i: number, cw: number): string[][] {
    return this.b.lanes[i].map((is, j) => card(is, cw, i === this.col && j === this.row[i]));
  }

  // sameHeight returns the card in lane t that sits at the screen height of the current selection.
  sameHeight(t: number): number {
    const [cw, ch] = this.dims();
    const cur = this.laneCards(this.col, cw);
    let y = 0;
    for (let j = scrollStart(cur, this.row[this.col], ch); j < this.row[this.col]; j++) y += cur[j].length + 1;
    const cards = this.laneCards(t, cw);
    const start = scrollStart(cards, this.row[t], ch);
    let best = start;
    for (let j = start, top = 0; j < cards.length && top <= y; j++) {
      best = j;
      top += cards[j].length + 1;
    }
    return best;
  }
}

// ticketKey turns "123" into "PROJ-123" and "abc-9" into "ABC-9"; anything else isn't a key.
export function ticketKey(project: string, q: string): string {
  q = q.trim().toUpperCase();
  if (!q.includes("-")) q = project.toUpperCase() + "-" + q;
  return /-\d+$/.test(q) ? q : "";
}

// Gruvbox dark palette.
const gbFg = "#ebdbb2";
const gbGray = "#928374";
const gbBg2 = "#504945";
const gbBg3 = "#665c54";
const gbRed = "#fb4934";
const gbGreen = "#b8bb26";
const gbYellow = "#fabd2f";
const gbBlue = "#83a598";
const gbPurple = "#d3869b";
const gbAqua = "#8ec07c";
const gbOrange = "#fe8019";

const text = chalk.hex(gbFg);
const faint = chalk.hex(gbGray);
const title = chalk.hex(gbYellow).bold;
const heading = chalk.hex(gbAqua).bold;
const keyStyle = chalk.hex(gbBlue).bold;
const initStyle = chalk.hex(gbPurple);
const msgStyle = chalk.hex(gbOrange);
const laneBorder = chalk.hex(gbBg3);
const activeBorder = chalk.hex(gbYellow);
const prColours: Record<string, string> = { OPEN: gbGreen, MERGED: gbPurple, DECLINED: gbRed };

export const boardHelp = "h/l lane · j/k ticket · gg/G top/bottom · enter open · / filter · H/L move ticket · r reload · q quit";
export const detailHelp = "j/k scroll · ctrl+d/u page · gg/G top/bottom · q/esc back";

// box draws a rounded border with one column of padding around rows, like lipgloss' laneStyle.
function box(rows: string[], w: number, h: number, border: ChalkInstance): string[] {
  const out = [border("╭" + "─".repeat(w + 2) + "╮")];
  for (let i = 0; i < Math.max(h, rows.length); i++) out.push(border("│") + " " + pad(rows[i] ?? "", w) + " " + border("│"));
  out.push(border("╰" + "─".repeat(w + 2) + "╯"));
  return out;
}

// approvals summarises reviewer approval, e.g. "1/2 approved  ✓ Ada  · Bob".
export function approvals(pr: PR): string {
  const rs = pr.reviewers ?? [];
  if (rs.length === 0) return "";
  const names = rs.map((r) => (r.approved ? chalk.hex(gbGreen)("✓ " + r.name) : "· " + r.name));
  return `${rs.filter((r) => r.approved).length}/${names.length} approved  ${names.join("  ")}`;
}

// scrollStart picks the first visible card so sel is on screen with as many cards above it as fit.
export function scrollStart(cards: string[][], sel: number, ch: number): number {
  if (cards.length === 0) return 0;
  let start = sel;
  for (let h = cards[sel].length; start > 0 && h + cards[start - 1].length + 1 <= ch; start--) h += cards[start - 1].length + 1;
  return start;
}

// card renders an issue as "KEY ... AB" followed by the summary wrapped to w; every line is padded to w.
// Styles are applied piecewise (with the selection background on each) since nested ANSI resets would drop it.
export function card(is: Issue, w: number, sel: boolean): string[] {
  const bg = (s: ChalkInstance) => (sel ? s.bgHex(gbBg2) : s);
  const ini = initials(is.fields.assignee);
  const gap = w - [...is.key].length - [...ini].length;
  const head =
    gap >= 1
      ? bg(keyStyle)(is.key) + bg(chalk)(" ".repeat(gap)) + bg(initStyle)(ini)
      : bg(keyStyle)(pad(trunc(is.key, w), w));
  const body = wrapAnsi(is.fields.summary, w, { hard: true }).split("\n");
  return [head, ...body.map((l) => bg(text)(pad(l, w)))];
}

// initials takes the first letter of the first and last words: "Ada Byron Lovelace" → "AL".
export function initials(n?: Named | null): string {
  const words = (n?.displayName ?? "").split(/\s+/).filter(Boolean);
  if (words.length === 0) return "";
  let s = [...words[0]][0];
  if (words.length > 1) s += [...words[words.length - 1]][0];
  return s.toUpperCase();
}

export function name(n?: Named | null): string {
  if (!n) return "—";
  return n.displayName || n.name || "";
}

export function trunc(s: string, w: number): string {
  const r = [...s];
  return r.length <= w ? s : r.slice(0, w - 1).join("") + "…";
}

function pad(s: string, w: number): string {
  return s + " ".repeat(Math.max(0, w - stringWidth(s)));
}

export function clamp(x: number, lo: number, hi: number): number {
  return Math.max(Math.min(x, hi), lo);
}
