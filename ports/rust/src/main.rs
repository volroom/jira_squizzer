mod jira;
#[cfg(test)]
mod tests;

use jira::{Client, Column, Issue, Named, PR, Res, Transition};
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Padding, Paragraph, Wrap};
use ratatui::{DefaultTerminal, Frame};
use serde::Deserialize;
use std::sync::mpsc::{self, Sender};
use std::{env, fs, io, thread};

#[derive(Clone, Default, Deserialize)]
#[serde(default)]
struct Config {
    base_url: String,
    username: String,
    project: String,
    api_key: String,
    board_id: i64, // optional; defaults to the project's first scrum board
}

fn load_config() -> Result<Config, String> {
    // Mirrors Go's os.UserConfigDir on Linux.
    let dir = env::var("XDG_CONFIG_HOME")
        .ok()
        .filter(|d| !d.is_empty())
        .unwrap_or_else(|| env::var("HOME").unwrap_or_default() + "/.config");
    let path = format!("{dir}/jira-squizzer/config.json");
    let mut c = match fs::read_to_string(&path) {
        Ok(s) => serde_json::from_str(&s).map_err(|e| format!("{path}: {e}"))?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => Config::default(),
        Err(e) => return Err(format!("open {path}: {e}")),
    };
    for (var, dst) in [
        ("JIRA_BASE_URL", &mut c.base_url),
        ("JIRA_USERNAME", &mut c.username),
        ("JIRA_PROJECT", &mut c.project),
        ("JIRA_API_KEY", &mut c.api_key),
    ] {
        if let Ok(v) = env::var(var)
            && !v.is_empty()
        {
            *dst = v;
        }
    }
    if c.base_url.is_empty()
        || c.username.is_empty()
        || c.project.is_empty()
        || c.api_key.is_empty()
    {
        return Err(format!(
            "missing config: set base_url, username, project, api_key in {path}\nor JIRA_BASE_URL, JIRA_USERNAME, JIRA_PROJECT, JIRA_API_KEY"
        ));
    }
    Ok(c)
}

/// group buckets issues into lanes by status; issues in unmapped statuses are dropped, as on the Jira board.
fn group(cols: &[Column], issues: Vec<Issue>) -> Vec<Vec<Issue>> {
    let mut lanes = vec![Vec::new(); cols.len()];
    for is in issues {
        if let Some(i) = cols
            .iter()
            .position(|c| c.statuses.contains(&is.fields.status.id))
        {
            lanes[i].push(is);
        }
    }
    lanes
}

fn find_transition<'a>(ts: &'a [Transition], statuses: &[String]) -> Option<&'a Transition> {
    ts.iter().find(|t| statuses.contains(&t.to.id))
}

#[derive(Default)]
struct Board {
    sprint: String,
    cols: Vec<Column>,
    lanes: Vec<Vec<Issue>>,
}

struct Detail {
    issue: Issue,
    prs: Vec<PR>,
    pr_err: Option<String>,
}

/// Msg is everything the model reacts to: terminal events and finished background loads.
enum Msg {
    Size(usize, usize),
    Key(String),
    Board(Res<Board>),
    Detail(Res<Box<Detail>>),
    Moved(Res<(String, String)>), // (key, lane name)
}

/// Cmd is work the model asks the event loop to do; loads run on a background thread.
#[derive(Debug)]
enum Cmd {
    Quit,
    LoadBoard,
    LoadDetail(String),
    Move(String, Column), // issue key, target lane
}

#[derive(Default)]
struct Model {
    cfg: Config,
    b: Board,
    col: usize,
    row: Vec<usize>, // cursor per lane
    detail: Option<Detail>,
    scroll: usize,
    pending_g: bool,
    search: Option<String>, // Some while typing a "/" ticket search
    query: String,          // active filter on the board
    all: Vec<Vec<Issue>>,   // unfiltered lanes; b.lanes is the filtered view
    follow: String,         // issue key to put the cursor on after the next reload
    busy: bool,
    msg: String,
    w: usize,
    h: usize,
}

fn fetch_board(c: &Client, cfg: &Config) -> Res<Board> {
    let id = if cfg.board_id == 0 {
        c.board_id(&cfg.project)?
    } else {
        cfg.board_id
    };
    let cols = c.columns(id)?;
    let (sid, sprint) = c.active_sprint(id)?;
    let lanes = group(&cols, c.sprint_issues(sid)?);
    Ok(Board {
        sprint,
        cols,
        lanes,
    })
}

fn move_issue(c: &Client, key: &str, target: &Column) -> Res<(String, String)> {
    let ts = c.transitions(key)?;
    let t = find_transition(&ts, &target.statuses)
        .ok_or_else(|| format!("{key}: no workflow transition into {:?}", target.name))?;
    c.transition(key, &t.id)?;
    Ok((key.to_string(), target.name.clone()))
}

/// spawn runs a Cmd on a background thread and sends its result back as a Msg.
fn spawn(cmd: Cmd, c: &Client, cfg: &Config, tx: &Sender<Msg>) {
    let (c, cfg, tx) = (c.clone(), cfg.clone(), tx.clone());
    thread::spawn(move || {
        let msg = match cmd {
            Cmd::Quit => return,
            Cmd::LoadBoard => Msg::Board(fetch_board(&c, &cfg)),
            Cmd::LoadDetail(key) => Msg::Detail(c.issue(&key).map(|issue| {
                let (prs, pr_err) = c.prs(&issue.id);
                Box::new(Detail { issue, prs, pr_err })
            })),
            Cmd::Move(key, target) => Msg::Moved(move_issue(&c, &key, &target)),
        };
        let _ = tx.send(msg);
    });
}

impl Model {
    fn update(&mut self, msg: Msg) -> Option<Cmd> {
        match msg {
            Msg::Size(w, h) => (self.w, self.h) = (w, h),
            Msg::Key(k) => return self.key(&k),
            Msg::Board(r) => {
                self.busy = false;
                match r {
                    Err(e) => self.msg = e,
                    Ok(b) => {
                        self.all = b.lanes.clone();
                        self.b = b;
                        if self.row.len() != self.b.lanes.len() {
                            self.row = vec![0; self.b.lanes.len()];
                        }
                        self.filter(&self.query.clone());
                        if self.follow.is_empty() {
                            self.msg.clear();
                        }
                        let follow = std::mem::take(&mut self.follow);
                        self.focus(&follow);
                    }
                }
            }
            Msg::Detail(r) => {
                self.busy = false;
                match r {
                    Err(e) => self.msg = e,
                    Ok(d) => (self.msg, self.detail, self.scroll) = (String::new(), Some(*d), 0),
                }
            }
            Msg::Moved(Err(e)) => (self.busy, self.msg) = (false, e),
            Msg::Moved(Ok((key, to))) => {
                self.msg = format!("{key} → {to}");
                self.follow = key;
                return Some(Cmd::LoadBoard);
            }
        }
        None
    }

    /// focus puts the board cursor on key, if it's on the board.
    fn focus(&mut self, key: &str) {
        for (i, lane) in self.b.lanes.iter().enumerate() {
            for (j, is) in lane.iter().enumerate() {
                if is.key == key {
                    (self.col, self.row[i]) = (i, j);
                }
            }
        }
    }

    /// filter shows only tickets whose key or title contain q (case-insensitive).
    fn filter(&mut self, q: &str) {
        self.query = q.to_string();
        let q = q.trim().to_lowercase();
        self.b.lanes = self
            .all
            .iter()
            .map(|lane| {
                lane.iter()
                    .filter(|is| {
                        format!("{} {}", is.key, is.fields.summary)
                            .to_lowercase()
                            .contains(&q)
                    })
                    .cloned()
                    .collect()
            })
            .collect();
        for (r, lane) in self.row.iter_mut().zip(&self.b.lanes) {
            *r = (*r).min(lane.len().saturating_sub(1));
        }
        self.col = self.col.min(self.b.lanes.len().saturating_sub(1));
        if self.b.lanes.get(self.col).is_some_and(|l| l.is_empty()) {
            // hop to a lane that still has tickets
            if let Some(i) = self.b.lanes.iter().position(|l| !l.is_empty()) {
                self.col = i;
            }
        }
    }

    fn count(&self) -> usize {
        self.b.lanes.iter().map(Vec::len).sum()
    }

    fn key(&mut self, k: &str) -> Option<Cmd> {
        if k == "ctrl+c" {
            return Some(Cmd::Quit);
        }
        if let Some(mut q) = self.search.clone() {
            match k {
                "esc" => (self.search, q) = (None, String::new()),
                "backspace" => _ = q.pop(),
                "enter" => {
                    self.search = None;
                    let key = ticket_key(&self.cfg.project, &q);
                    if self.count() == 0 && !key.is_empty() {
                        // not on the sprint board: clear the filter and open it straight from Jira
                        self.filter("");
                        (self.busy, self.msg) = (true, format!("loading {key}…"));
                        return Some(Cmd::LoadDetail(key));
                    }
                    return None;
                }
                _ if k.len() == 1 => q.push_str(k),
                _ => {}
            }
            if self.search.is_some() {
                self.search = Some(q.clone());
            }
            self.filter(&q);
            return None;
        }
        let g = std::mem::take(&mut self.pending_g);

        if self.detail.is_some() {
            match k {
                "q" | "esc" | "h" => {
                    self.detail = None;
                    return None;
                }
                "j" | "down" => self.scroll += 1,
                "k" | "up" => self.scroll = self.scroll.saturating_sub(1),
                "ctrl+d" => self.scroll += self.h / 2,
                "ctrl+u" => self.scroll = self.scroll.saturating_sub(self.h / 2),
                "G" => self.scroll = 1 << 30,
                "g" if g => self.scroll = 0,
                "g" => self.pending_g = true,
                _ => {}
            }
            let n = self.detail_text().line_count(self.detail_width());
            self.scroll = self.scroll.min((n + 1).saturating_sub(self.h));
            return None;
        }

        if k == "q" {
            return Some(Cmd::Quit);
        }
        if self.busy {
            return None;
        }
        if k == "r" {
            (self.busy, self.msg) = (true, "reloading…".into());
            return Some(Cmd::LoadBoard);
        }
        if k == "/" {
            (self.search, self.msg) = (Some(self.query.clone()), String::new());
            return None;
        }
        if k == "esc" && !self.query.is_empty() {
            self.filter("");
            return None;
        }
        if self.b.lanes.is_empty() {
            return None;
        }
        let (col, len) = (self.col, self.b.lanes[self.col].len());
        let target = |next: bool| {
            if next {
                Some(col + 1)
            } else {
                col.checked_sub(1)
            }
        };
        match k {
            "h" | "left" | "l" | "right" => {
                if let Some(t) =
                    target(k == "l" || k == "right").filter(|&t| t < self.b.lanes.len())
                {
                    self.row[t] = self.same_height(t);
                    self.col = t;
                }
            }
            "j" | "down" => self.row[col] += 1,
            "k" | "up" => self.row[col] = self.row[col].saturating_sub(1),
            "G" => self.row[col] = len.saturating_sub(1),
            "g" if g => self.row[col] = 0,
            "g" => self.pending_g = true,
            "enter" | "o" if len > 0 => {
                let key = self.b.lanes[col][self.row[col]].key.clone();
                (self.busy, self.msg) = (true, format!("loading {key}…"));
                return Some(Cmd::LoadDetail(key));
            }
            "H" | "L" => {
                if let Some(t) = target(k == "L").filter(|&t| len > 0 && t < self.b.cols.len()) {
                    let key = self.b.lanes[col][self.row[col]].key.clone();
                    (self.busy, self.msg) = (true, format!("moving {key}…"));
                    return Some(Cmd::Move(key, self.b.cols[t].clone()));
                }
            }
            _ => {}
        }
        let n = self.b.lanes[self.col].len();
        self.row[self.col] = self.row[self.col].min(n.saturating_sub(1));
        None
    }
}

/// ticket_key turns "123" into "PROJ-123" and "abc-9" into "ABC-9"; anything else isn't a key.
fn ticket_key(project: &str, q: &str) -> String {
    let mut q = q.trim().to_uppercase();
    if !q.contains('-') {
        q = format!("{}-{q}", project.to_uppercase());
    }
    let num = &q[q.rfind('-').map_or(0, |i| i + 1)..];
    if num.is_empty() || !num.bytes().all(|b| b.is_ascii_digit()) {
        return String::new();
    }
    q
}

// Gruvbox dark palette.
const FG: Color = Color::Rgb(0xeb, 0xdb, 0xb2);
const GRAY: Color = Color::Rgb(0x92, 0x83, 0x74);
const BG2: Color = Color::Rgb(0x50, 0x49, 0x45);
const BG3: Color = Color::Rgb(0x66, 0x5c, 0x54);
const RED: Color = Color::Rgb(0xfb, 0x49, 0x34);
const GREEN: Color = Color::Rgb(0xb8, 0xbb, 0x26);
const YELLOW: Color = Color::Rgb(0xfa, 0xbd, 0x2f);
const BLUE: Color = Color::Rgb(0x83, 0xa5, 0x98);
const PURPLE: Color = Color::Rgb(0xd3, 0x86, 0x9b);
const AQUA: Color = Color::Rgb(0x8e, 0xc0, 0x7c);
const ORANGE: Color = Color::Rgb(0xfe, 0x80, 0x19);

const TEXT: Style = Style::new().fg(FG);
const FAINT: Style = Style::new().fg(GRAY);
const TITLE: Style = Style::new().fg(YELLOW).add_modifier(Modifier::BOLD);
const HEADING: Style = Style::new().fg(AQUA).add_modifier(Modifier::BOLD);
const KEY: Style = Style::new().fg(BLUE).add_modifier(Modifier::BOLD);
const INIT: Style = Style::new().fg(PURPLE);
const MSG: Style = Style::new().fg(ORANGE);

fn pr_colour(status: &str) -> Style {
    match status {
        "OPEN" => Style::new().fg(GREEN),
        "MERGED" => Style::new().fg(PURPLE),
        "DECLINED" => Style::new().fg(RED),
        _ => Style::new(),
    }
}

const BOARD_HELP: &str = "h/l lane · j/k ticket · gg/G top/bottom · enter open · / filter · H/L move ticket · r reload · q quit";
const DETAIL_HELP: &str = "j/k scroll · ctrl+d/u page · gg/G top/bottom · q/esc back";

impl Model {
    fn footer(&self, help: &str) -> Line<'static> {
        if let Some(s) = &self.search {
            return Line::from(vec![
                Span::styled(format!("/{s}"), MSG),
                Span::styled(
                    format!(
                        "█  {} shown · number or title · enter keep · esc clear",
                        self.count()
                    ),
                    FAINT,
                ),
            ]);
        }
        if !self.msg.is_empty() {
            return Line::styled(self.msg.clone(), MSG);
        }
        if !self.query.is_empty() && self.detail.is_none() {
            return Line::styled(
                format!(
                    "filter {:?} · {} shown · / edit · esc clear",
                    self.query,
                    self.count()
                ),
                MSG,
            );
        }
        Line::styled(help.to_string(), FAINT)
    }

    fn view(&self, f: &mut Frame) {
        if self.w == 0 {
            return;
        }
        let area = f.area();
        let line_at = |y: usize| Rect::new(0, y as u16, area.width, 1).intersection(area);
        if self.detail.is_some() {
            let h = self.h.saturating_sub(1);
            let body = Rect::new(0, 0, self.detail_width(), h as u16).intersection(area);
            f.render_widget(self.detail_text().scroll((self.scroll as u16, 0)), body);
            f.render_widget(self.footer(DETAIL_HELP), line_at(h));
            return;
        }

        f.render_widget(
            Line::styled(format!("{} · {}", self.cfg.project, self.b.sprint), TITLE),
            line_at(0),
        );
        if self.b.lanes.is_empty() {
            f.render_widget(self.footer(BOARD_HELP), line_at(2));
            return;
        }
        let (cw, ch) = self.dims();
        for (i, lane) in self.b.lanes.iter().enumerate() {
            let mut rows = vec![Line::styled(
                trunc(&format!("{} ({})", self.b.cols[i].name, lane.len()), cw),
                HEADING,
            )];
            let cards = self.lane_cards(i, cw);
            let start = scroll_start(&cards, self.row[i], ch);
            let mut lines = Vec::new();
            for (j, card) in cards.into_iter().enumerate().skip(start) {
                if lines.len() >= ch {
                    break;
                }
                if j > start {
                    lines.push(Line::default());
                }
                lines.extend(card);
            }
            lines.truncate(ch);
            rows.extend(lines);
            let border = if i == self.col { YELLOW } else { BG3 };
            let block = Block::bordered()
                .border_type(BorderType::Rounded)
                .border_style(Style::new().fg(border))
                .padding(Padding::horizontal(1));
            let r = Rect::new((i * (cw + 4)) as u16, 1, (cw + 4) as u16, (ch + 3) as u16)
                .intersection(area);
            f.render_widget(Paragraph::new(rows).block(block), r);
        }
        f.render_widget(self.footer(BOARD_HELP), line_at(ch + 4));
    }

    fn detail_width(&self) -> u16 {
        self.w.saturating_sub(1).max(20) as u16
    }

    fn detail_text(&self) -> Paragraph<'static> {
        let Some(d) = &self.detail else {
            return Paragraph::default();
        };
        let f = &d.issue.fields;
        let mut lines = vec![
            Line::from(vec![
                Span::styled(d.issue.key.clone(), KEY),
                "  ".into(),
                Span::styled(f.summary.clone(), TITLE),
            ]),
            Line::raw(format!(
                "{} · {} · {}",
                name(&f.issuetype),
                f.status.name,
                name(&f.priority)
            )),
            Line::raw(format!(
                "Assignee: {}   Reporter: {}",
                name(&f.assignee),
                name(&f.reporter)
            )),
            Line::styled(
                format!(
                    "{}/browse/{}",
                    self.cfg.base_url.trim_end_matches('/'),
                    d.issue.key
                ),
                FAINT,
            ),
            Line::default(),
            Line::styled("Pull requests", HEADING),
        ];
        if let Some(e) = &d.pr_err {
            lines.push(Line::raw(format!("  couldn't load: {e}")));
        } else if d.prs.is_empty() {
            lines.push(Line::raw("  none"));
        }
        for pr in &d.prs {
            lines.push(Line::from(vec![
                "  ".into(),
                Span::styled(format!("[{}]", pr.status), pr_colour(&pr.status)),
                format!(" {} {} {}", pr.repository_name, pr.id, pr.name).into(),
            ]));
            lines.push(Line::from(vec![
                "    ".into(),
                Span::styled(pr.url.clone(), FAINT),
            ]));
            let mut a = approvals(pr);
            if !a.spans.is_empty() {
                a.spans.insert(0, "    ".into());
                lines.push(a);
            }
        }
        lines.extend([Line::default(), Line::styled("Description", HEADING)]);
        let desc = f.description.replace('\r', "");
        if desc.is_empty() {
            lines.push(Line::styled("(no description)", FAINT));
        } else {
            lines.extend(desc.split('\n').map(|l| Line::raw(l.to_string())));
        }
        Paragraph::new(lines).style(TEXT).wrap(Wrap { trim: false })
    }

    /// dims returns the card width and the number of card lines visible per lane.
    fn dims(&self) -> (usize, usize) {
        let cw = (self.w / self.b.lanes.len().max(1))
            .saturating_sub(4)
            .max(5); // 4 = border + padding
        (cw, self.h.saturating_sub(5).max(1)) // title, footer, borders, lane header
    }

    fn lane_cards(&self, i: usize, cw: usize) -> Vec<Vec<Line<'static>>> {
        let lane = &self.b.lanes[i];
        lane.iter()
            .enumerate()
            .map(|(j, is)| card(is, cw, i == self.col && j == self.row[i]))
            .collect()
    }

    /// same_height returns the card in lane t that sits at the screen height of the current selection.
    fn same_height(&self, t: usize) -> usize {
        let (cw, ch) = self.dims();
        let (cur, sel) = (self.lane_cards(self.col, cw), self.row[self.col]);
        let y: usize = (scroll_start(&cur, sel, ch)..sel)
            .map(|j| cur[j].len() + 1)
            .sum();
        let cards = self.lane_cards(t, cw);
        let start = scroll_start(&cards, self.row[t], ch);
        let (mut best, mut top) = (start, 0);
        for (j, c) in cards.iter().enumerate().skip(start) {
            if top > y {
                break;
            }
            (best, top) = (j, top + c.len() + 1);
        }
        best
    }
}

/// approvals summarises reviewer approval, e.g. "1/2 approved  ✓ Ada  · Bob".
fn approvals(pr: &PR) -> Line<'static> {
    if pr.reviewers.is_empty() {
        return Line::default();
    }
    let n = pr.reviewers.iter().filter(|r| r.approved).count();
    let mut spans = vec![Span::raw(format!("{n}/{} approved", pr.reviewers.len()))];
    for r in &pr.reviewers {
        spans.push("  ".into());
        spans.push(match r.approved {
            true => Span::styled(format!("✓ {}", r.name), Style::new().fg(GREEN)),
            false => Span::raw(format!("· {}", r.name)),
        });
    }
    Line::from(spans)
}

/// scroll_start picks the first visible card so sel is on screen with as many cards above it as fit.
fn scroll_start(cards: &[Vec<Line>], sel: usize, ch: usize) -> usize {
    if cards.is_empty() {
        return 0;
    }
    let (mut start, mut h) = (sel, cards[sel].len());
    while start > 0 && h + cards[start - 1].len() < ch {
        h += cards[start - 1].len() + 1;
        start -= 1;
    }
    start
}

/// card renders an issue as "KEY ... AB" followed by the summary wrapped to w; every line is padded to w.
fn card(is: &Issue, w: usize, sel: bool) -> Vec<Line<'static>> {
    let bg = if sel {
        Style::new().bg(BG2)
    } else {
        Style::new()
    };
    let inits = initials(&is.fields.assignee);
    let gap = w as isize - is.key.chars().count() as isize - inits.chars().count() as isize;
    let head = if gap >= 1 {
        Line::from(vec![
            Span::styled(is.key.clone(), KEY.patch(bg)),
            Span::styled(" ".repeat(gap as usize), bg),
            Span::styled(inits, INIT.patch(bg)),
        ])
    } else {
        Line::styled(format!("{:<w$}", trunc(&is.key, w)), KEY.patch(bg))
    };
    let body = wrap(&is.fields.summary, w)
        .into_iter()
        .map(|l| Line::styled(format!("{l:<w$}"), TEXT.patch(bg)));
    std::iter::once(head).chain(body).collect()
}

/// wrap word-wraps s to w columns, hard-breaking words longer than w.
fn wrap(s: &str, w: usize) -> Vec<String> {
    let mut out = Vec::new();
    for para in s.split('\n') {
        let (mut line, mut n) = (String::new(), 0);
        for word in para.split_whitespace() {
            let mut word: Vec<char> = word.chars().collect();
            if n > 0 && n + 1 + word.len() > w {
                out.push(std::mem::take(&mut line));
                n = 0;
            }
            if n > 0 {
                line.push(' ');
                n += 1;
            }
            while n + word.len() > w {
                line.extend(word.drain(..w - n));
                out.push(std::mem::take(&mut line));
                n = 0;
            }
            n += word.len();
            line.extend(word);
        }
        out.push(line);
    }
    out
}

/// initials takes the first letter of the first and last words: "Ada Byron Lovelace" → "AL".
fn initials(n: &Option<Named>) -> String {
    let Some(n) = n else { return String::new() };
    let words: Vec<&str> = n.display_name.split_whitespace().collect();
    let first = |w: &str| w.chars().next().into_iter();
    match words[..] {
        [] => String::new(),
        [a] => first(a).collect(),
        [a, .., b] => first(a).chain(first(b)).collect(),
    }
    .to_uppercase()
}

fn name(n: &Option<Named>) -> String {
    match n {
        None => "—".into(),
        Some(n) if !n.display_name.is_empty() => n.display_name.clone(),
        Some(n) => n.name.clone(),
    }
}

fn trunc(s: &str, w: usize) -> String {
    if s.chars().count() <= w {
        return s.to_string();
    }
    s.chars().take(w.saturating_sub(1)).chain(['…']).collect()
}

/// key_name converts a crossterm key to bubbletea's key string ("j", "ctrl+d", "enter", ...).
fn key_name(k: KeyEvent) -> Option<String> {
    Some(match k.code {
        KeyCode::Char(c) if k.modifiers.contains(KeyModifiers::CONTROL) => format!("ctrl+{c}"),
        KeyCode::Char(c) if k.modifiers.contains(KeyModifiers::ALT) => format!("alt+{c}"),
        KeyCode::Char(c) => c.to_string(),
        KeyCode::Enter => "enter".into(),
        KeyCode::Esc => "esc".into(),
        KeyCode::Backspace => "backspace".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        _ => return None,
    })
}

fn run(term: &mut DefaultTerminal, m: &mut Model, c: &Client) -> io::Result<()> {
    let (tx, rx) = mpsc::channel();
    let size = term.size()?;
    m.update(Msg::Size(size.width.into(), size.height.into()));
    let keys = tx.clone();
    thread::spawn(move || {
        loop {
            let msg = match event::read() {
                Ok(Event::Key(k)) if k.kind == KeyEventKind::Press => key_name(k).map(Msg::Key),
                Ok(Event::Resize(w, h)) => Some(Msg::Size(w.into(), h.into())),
                Ok(_) => None,
                Err(_) => return,
            };
            if let Some(msg) = msg
                && keys.send(msg).is_err()
            {
                return;
            }
        }
    });
    let mut cmd = Some(Cmd::LoadBoard);
    loop {
        match cmd {
            Some(Cmd::Quit) => return Ok(()),
            Some(cmd) => spawn(cmd, c, &m.cfg, &tx),
            None => {}
        }
        term.draw(|f| m.view(f))?;
        let Ok(msg) = rx.recv() else { return Ok(()) };
        cmd = m.update(msg);
    }
}

fn main() {
    let cfg = load_config().unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(1)
    });
    let client = Client::new(&cfg.base_url, &cfg.username, &cfg.api_key);
    let mut m = Model {
        cfg,
        busy: true,
        msg: "loading board…".into(),
        ..Default::default()
    };
    let mut term = ratatui::init();
    let res = run(&mut term, &mut m, &client);
    ratatui::restore();
    if let Err(e) = res {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
