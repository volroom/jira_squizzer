"""Sprint board model, keys and view, plus a thin Textual shell that runs it."""

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Static

from .jira import PR, Client, Column, Issue, Named, Transition


@dataclass
class Config:
    base_url: str = ""
    username: str = ""
    project: str = ""
    api_key: str = ""
    board_id: int = 0  # optional; defaults to the project's first scrum board


def config_path() -> Path:
    # Mirrors Go's os.UserConfigDir.
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "jira-squizzer" / "config.json"


def load_config() -> Config:
    path = config_path()
    c = Config()
    try:
        d = json.loads(path.read_text())
        c = Config(**{k: d[k] for k in vars(c) if k in d})
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"{path}: {e}") from e
    for env, attr in [
        ("JIRA_BASE_URL", "base_url"),
        ("JIRA_USERNAME", "username"),
        ("JIRA_PROJECT", "project"),
        ("JIRA_API_KEY", "api_key"),
    ]:
        if v := os.environ.get(env):
            setattr(c, attr, v)
    if not (c.base_url and c.username and c.project and c.api_key):
        raise ValueError(
            f"missing config: set base_url, username, project, api_key in {path}\n"
            "or JIRA_BASE_URL, JIRA_USERNAME, JIRA_PROJECT, JIRA_API_KEY"
        )
    return c


def group(cols: list[Column], issues: list[Issue]) -> list[list[Issue]]:
    """Buckets issues into lanes by status; unmapped statuses are dropped, as on the Jira board."""
    lanes: list[list[Issue]] = [[] for _ in cols]
    for i in issues:
        for lane, c in zip(lanes, cols):
            if i.status_id in c.statuses:
                lane.append(i)
                break
    return lanes


def find_transition(ts: list[Transition], statuses: list[str]) -> Transition | None:
    return next((t for t in ts if t.to_id in statuses), None)


@dataclass
class Board:
    sprint: str = ""
    cols: list[Column] = field(default_factory=list)
    lanes: list[list[Issue]] = field(default_factory=list)


@dataclass
class Detail:
    issue: Issue
    prs: list[PR]
    pr_err: str | None = None


@dataclass
class BoardMsg:
    b: Board | None = None
    err: str | None = None


@dataclass
class DetailMsg:
    d: Detail | None = None
    err: str | None = None


@dataclass
class MovedMsg:
    key: str = ""
    to: str = ""
    err: str | None = None


type Msg = BoardMsg | DetailMsg | MovedMsg
type Cmd = Callable[[], Msg | None]


def quit_cmd() -> None:
    """Sentinel command: the shell exits when it's returned."""


def fetch_board(c: Client, cfg: Config) -> Board:
    id = cfg.board_id or c.board_id(cfg.project)
    cols = c.columns(id)
    sid, name = c.active_sprint(id)
    return Board(name, cols, group(cols, c.sprint_issues(sid)))


@dataclass
class Model:
    cfg: Config = field(default_factory=Config)
    c: Client | None = None
    b: Board = field(default_factory=Board)
    col: int = 0
    row: list[int] = field(default_factory=list)  # cursor per lane
    detail: Detail | None = None
    scroll: int = 0
    pending_g: bool = False
    search: str | None = None  # not None while typing a "/" ticket search
    query: str = ""  # active filter on the board
    all: list[list[Issue]] = field(
        default_factory=list
    )  # unfiltered; b.lanes is the view
    follow: str = ""  # issue key to put the cursor on after the next reload
    busy: bool = False
    msg: str = ""
    w: int = 0
    h: int = 0

    def load_board(self) -> Cmd:
        c, cfg = self.c, self.cfg

        def run() -> Msg:
            try:
                assert c is not None
                return BoardMsg(fetch_board(c, cfg))
            except Exception as e:  # noqa: BLE001 -- any failure becomes the error msg
                return BoardMsg(err=str(e))

        return run

    def load_detail(self, key: str) -> Cmd:
        c = self.c

        def run() -> Msg:
            try:
                assert c is not None
                i = c.issue(key)
            except Exception as e:  # noqa: BLE001 -- any failure becomes the error msg
                return DetailMsg(err=str(e))
            try:
                return DetailMsg(Detail(i, c.prs(i.id)))
            except Exception as e:  # noqa: BLE001 -- any failure becomes the error msg
                return DetailMsg(Detail(i, [], str(e)))

        return run

    def move(self, i: Issue, target: Column) -> Cmd:
        c = self.c

        def run() -> Msg:
            try:
                assert c is not None
                t = find_transition(c.transitions(i.key), target.statuses)
                if t is None:
                    to = json.dumps(target.name, ensure_ascii=False)
                    return MovedMsg(err=f"{i.key}: no workflow transition into {to}")
                c.transition(i.key, t.id)
                return MovedMsg(i.key, target.name)
            except Exception as e:  # noqa: BLE001 -- any failure becomes the error msg
                return MovedMsg(err=str(e))

        return run

    def init(self) -> Cmd:
        return self.load_board()

    def update(self, msg: Msg) -> Cmd | None:
        match msg:
            case BoardMsg(err=str(err)):
                self.busy, self.msg = False, err
            case BoardMsg(b=Board() as b):
                self.busy = False
                self.b, self.all = b, b.lanes
                if len(self.row) != len(b.lanes):
                    self.row = [0] * len(b.lanes)
                self.filter(self.query)
                if not self.follow:
                    self.msg = ""
                self.focus(self.follow)
                self.follow = ""
            case DetailMsg(err=str(err)):
                self.busy, self.msg = False, err
            case DetailMsg(d=d):
                self.busy, self.msg, self.detail, self.scroll = False, "", d, 0
            case MovedMsg(err=str(err)):
                self.busy, self.msg = False, err
            case MovedMsg(key=key, to=to):
                self.msg, self.follow = f"{key} → {to}", key
                return self.load_board()
        return None

    def focus(self, key: str) -> None:
        """Puts the board cursor on key, if it's on the board."""
        for i, lane in enumerate(self.b.lanes):
            for j, issue in enumerate(lane):
                if issue.key == key:
                    self.col, self.row[i] = i, j

    def filter(self, q: str) -> None:
        """Shows only tickets whose key or title contain q (case-insensitive)."""
        self.query = q
        q = q.strip().lower()
        self.b.lanes = [
            [i for i in lane if q in (i.key + " " + i.summary).lower()]
            for lane in self.all
        ]
        for i in range(len(self.row)):
            self.row[i] = clamp(self.row[i], 0, len(self.b.lanes[i]) - 1)
        self.col = clamp(self.col, 0, len(self.b.lanes) - 1)
        if self.b.lanes and not self.b.lanes[self.col]:  # hop to a lane with tickets
            self.col = next(
                (i for i, lane in enumerate(self.b.lanes) if lane), self.col
            )

    def count(self) -> int:
        return sum(len(lane) for lane in self.b.lanes)

    def key(self, k: str) -> Cmd | None:
        if k == "ctrl+c":
            return quit_cmd
        if self.search is not None:
            q = self.search
            match k:
                case "esc":
                    self.search, q = None, ""
                case "backspace":
                    q = q[:-1]
                case "enter":
                    self.search = None
                    key = ticket_key(self.cfg.project, q)
                    if self.count() == 0 and key:
                        # not on the sprint board: clear the filter and open it from Jira
                        self.filter("")
                        self.busy, self.msg = True, f"loading {key}…"
                        return self.load_detail(key)
                    return None
                case _ if len(k) == 1 and k.isascii():
                    q += k
            if self.search is not None:
                self.search = q
            self.filter(q)
            return None
        g, self.pending_g = self.pending_g, False

        if self.detail is not None:
            match k:
                case "q" | "esc" | "h":
                    self.detail = None
                    return None
                case "j" | "down":
                    self.scroll += 1
                case "k" | "up":
                    self.scroll -= 1
                case "ctrl+d":
                    self.scroll += self.h // 2
                case "ctrl+u":
                    self.scroll -= self.h // 2
                case "G":
                    self.scroll = 1 << 30
                case "g":
                    if g:
                        self.scroll = 0
                    else:
                        self.pending_g = True
            self.scroll = clamp(self.scroll, 0, len(self.detail_lines()) - self.h + 1)
            return None

        if k == "q":
            return quit_cmd
        if self.busy:
            return None
        if k == "r":
            self.busy, self.msg = True, "reloading…"
            return self.load_board()
        if k == "/":
            self.search, self.msg = self.query, ""
            return None
        if k == "esc" and self.query:
            self.filter("")
            return None
        if not self.b.lanes:
            return None
        lane = self.b.lanes[self.col]
        match k:
            case "h" | "left" | "l" | "right":
                t = self.col + 1 if k in ("l", "right") else self.col - 1
                if 0 <= t < len(self.b.lanes):
                    self.row[t] = self.same_height(t)
                    self.col = t
            case "j" | "down":
                self.row[self.col] += 1
            case "k" | "up":
                self.row[self.col] -= 1
            case "G":
                self.row[self.col] = len(lane) - 1
            case "g":
                if g:
                    self.row[self.col] = 0
                else:
                    self.pending_g = True
            case "enter" | "o":
                if lane:
                    key = lane[self.row[self.col]].key
                    self.busy, self.msg = True, f"loading {key}…"
                    return self.load_detail(key)
            case "H" | "L":
                t = self.col + 1 if k == "L" else self.col - 1
                if lane and 0 <= t < len(self.b.cols):
                    i = lane[self.row[self.col]]
                    self.busy, self.msg = True, f"moving {i.key}…"
                    return self.move(i, self.b.cols[t])
        self.col = clamp(self.col, 0, len(self.b.lanes) - 1)
        self.row[self.col] = clamp(
            self.row[self.col], 0, len(self.b.lanes[self.col]) - 1
        )
        return None

    def footer(self, help: str) -> Text:
        if self.search is not None:
            return Text.assemble(
                ("/" + self.search, MSG),
                (
                    f"█  {self.count()} shown · number or title · enter keep · esc clear",
                    FAINT,
                ),
            )
        if self.msg:
            return Text(self.msg, MSG)
        if self.query and self.detail is None:
            q = json.dumps(self.query, ensure_ascii=False)
            return Text(f"filter {q} · {self.count()} shown · / edit · esc clear", MSG)
        return Text(help, FAINT)

    def view(self) -> Text:
        if self.w == 0:
            return Text()
        if self.detail is not None:
            lines = self.detail_lines()[self.scroll : self.scroll + self.h - 1]
            lines += [Text()] * (self.h - 1 - len(lines))
            return Text("\n").join([*lines, self.footer(DETAIL_HELP)])

        header = Text(f"{self.cfg.project} · {self.b.sprint}", TITLE)
        if not self.b.lanes:
            return Text("\n").join([header, Text(), self.footer(BOARD_HELP)])
        cw, ch = self.dims()
        boxes: list[list[Text]] = []
        for i, lane in enumerate(self.b.lanes):
            head = trunc(f"{self.b.cols[i].name} ({len(lane)})", cw)
            rows = [Text(head, HEADING)]
            cards = self.lane_cards(i, cw)
            start = scroll_start(cards, self.row[i], ch)
            lines: list[Text] = []
            for j in range(start, len(lane)):
                if len(lines) >= ch:
                    break
                if j > start:
                    lines.append(Text())
                lines += cards[j]
            rows += lines[:ch]
            rows += [Text()] * (ch + 1 - len(rows))
            boxes.append(box(rows, cw, ACTIVE_BORDER if i == self.col else BORDER))
        out = [header] + [Text("").join(parts) for parts in zip(*boxes)]
        return Text("\n").join([*out, self.footer(BOARD_HELP)])

    def detail_lines(self) -> list[Text]:
        d = self.detail
        assert d is not None
        i = d.issue
        t = Text(style=TEXT)
        t.append(i.key, KEY).append("  ").append(i.summary, TITLE).append("\n")
        t.append(f"{name(i.issue_type)} · {i.status_name} · {name(i.priority)}\n")
        t.append(f"Assignee: {name(i.assignee)}   Reporter: {name(i.reporter)}\n")
        t.append(f"{self.cfg.base_url.rstrip('/')}/browse/{i.key}", FAINT).append(
            "\n\n"
        )
        t.append("Pull requests", HEADING).append("\n")
        if d.pr_err is not None:
            t.append(f"  couldn't load: {d.pr_err}\n")
        elif not d.prs:
            t.append("  none\n")
        for pr in d.prs:
            colour = PR_COLOURS.get(pr.status, "")
            t.append("  ").append(f"[{pr.status}]", colour)
            t.append(f" {pr.repository_name} {pr.id} {pr.name}\n    ")
            t.append(pr.url, FAINT).append("\n")
            if a := approvals(pr):
                t.append("    ").append_text(a).append("\n")
        desc = i.description.replace("\r", "")
        t.append("\n").append("Description", HEADING).append("\n")
        if desc:
            t.append(desc)
        else:
            t.append("(no description)", FAINT)
        return list(t.wrap(_console, max(self.w - 1, 20), tab_size=4))

    def dims(self) -> tuple[int, int]:
        """Card width and the number of card lines visible per lane."""
        return (
            max(self.w // max(len(self.b.lanes), 1) - 4, 5),  # 4 = border + padding
            max(self.h - 5, 1),  # title, footer, borders, lane header
        )

    def lane_cards(self, i: int, cw: int) -> list[list[Text]]:
        return [
            card(issue, cw, i == self.col and j == self.row[i])
            for j, issue in enumerate(self.b.lanes[i])
        ]

    def same_height(self, t: int) -> int:
        """The card in lane t that sits at the screen height of the current selection."""
        cw, ch = self.dims()
        cur = self.lane_cards(self.col, cw)
        y = sum(
            len(cur[j]) + 1
            for j in range(
                scroll_start(cur, self.row[self.col], ch), self.row[self.col]
            )
        )
        cards = self.lane_cards(t, cw)
        best = start = scroll_start(cards, self.row[t], ch)
        top = 0
        for j in range(start, len(cards)):
            if top > y:
                break
            best, top = j, top + len(cards[j]) + 1
        return best


def ticket_key(project: str, q: str) -> str:
    """Turns "123" into "PROJ-123" and "abc-9" into "ABC-9"; anything else isn't a key."""
    q = q.strip().upper()
    if "-" not in q:
        q = project.upper() + "-" + q
    i = q.rfind("-")
    if i == len(q) - 1 or q[i + 1 :].strip("0123456789"):
        return ""
    return q


# Gruvbox dark palette.
GB_FG = "#ebdbb2"
GB_GRAY = "#928374"
GB_BG2 = "#504945"
GB_BG3 = "#665c54"
GB_RED = "#fb4934"
GB_GREEN = "#b8bb26"
GB_YELLOW = "#fabd2f"
GB_BLUE = "#83a598"
GB_PURPLE = "#d3869b"
GB_AQUA = "#8ec07c"
GB_ORANGE = "#fe8019"

TEXT = Style(color=GB_FG)
FAINT = Style(color=GB_GRAY)
TITLE = Style(color=GB_YELLOW, bold=True)
HEADING = Style(color=GB_AQUA, bold=True)
KEY = Style(color=GB_BLUE, bold=True)
INIT = Style(color=GB_PURPLE)
MSG = Style(color=GB_ORANGE)
BORDER = Style(color=GB_BG3)
ACTIVE_BORDER = Style(color=GB_YELLOW)
PR_COLOURS = {"OPEN": GB_GREEN, "MERGED": GB_PURPLE, "DECLINED": GB_RED}

BOARD_HELP = "h/l lane · j/k ticket · gg/G top/bottom · enter open · / filter · H/L move ticket · r reload · q quit"
DETAIL_HELP = "j/k scroll · ctrl+d/u page · gg/G top/bottom · q/esc back"

_console = Console()  # Text.wrap wants one; it isn't written to


def box(rows: list[Text], w: int, border: Style) -> list[Text]:
    """Draws a rounded border with one column of padding around rows, each padded to w."""
    edge = "─" * (w + 2)
    out = [Text(f"╭{edge}╮", border)]
    for r in rows:
        r = r.copy()
        r.truncate(w, pad=True)
        out.append(Text.assemble(("│ ", border), r, (" │", border)))
    out.append(Text(f"╰{edge}╯", border))
    return out


def approvals(pr: PR) -> Text:
    """Summarises reviewer approval, e.g. "1/2 approved  ✓ Ada  · Bob"."""
    if not pr.reviewers:
        return Text()
    names = [
        Text("✓ " + r.name, GB_GREEN) if r.approved else Text("· " + r.name)
        for r in pr.reviewers
    ]
    n = sum(r.approved for r in pr.reviewers)
    return Text(f"{n}/{len(names)} approved  ") + Text("  ").join(names)


def scroll_start(cards: list[list[Text]], sel: int, ch: int) -> int:
    """The first visible card so sel is on screen with as many cards above it as fit."""
    if not cards:
        return 0
    start, h = sel, len(cards[sel])
    while start > 0 and h + len(cards[start - 1]) + 1 <= ch:
        h += len(cards[start - 1]) + 1
        start -= 1
    return start


def card(i: Issue, w: int, sel: bool) -> list[Text]:
    """An issue as "KEY ... AB" then the summary wrapped to w; every line is padded to w."""
    bg = Style(bgcolor=GB_BG2) if sel else Style()
    ini = initials(i.assignee)
    gap = w - len(i.key) - len(ini)
    if gap >= 1:
        head = Text.assemble((i.key, KEY + bg), (" " * gap, bg), (ini, INIT + bg))
    else:
        head = Text(trunc(i.key, w), KEY + bg)
        head.truncate(w, pad=True)
    body = Text(i.summary, TEXT + bg).wrap(_console, w, justify="left")
    return [head, *body]


def initials(n: Named | None) -> str:
    """First letter of the first and last words: "Ada Byron Lovelace" → "AL"."""
    words = n.display_name.split() if n else []
    if not words:
        return ""
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def name(n: Named | None) -> str:
    if n is None:
        return "—"
    return n.display_name or n.name


def trunc(s: str, w: int) -> str:
    return s if len(s) <= w else s[: w - 1] + "…"


def clamp(x: int, lo: int, hi: int) -> int:
    return max(min(x, hi), lo)


# Textual key names that differ from bubbletea's.
KEY_NAMES = {"escape": "esc"}


class Squizzer(App[None]):
    """A thin shell: forwards keys to the Model, runs its Cmds in threads, shows its view."""

    ENABLE_COMMAND_PALETTE = False

    def __init__(self, model: Model) -> None:
        super().__init__()
        self.model = model
        self.theme = (
            "ansi-dark"  # use the terminal's own background, like the Go version
        )

    def compose(self) -> ComposeResult:
        yield Static()

    def on_mount(self) -> None:
        self.run_cmd(self.model.init())

    def on_resize(self, event: events.Resize) -> None:
        self.model.w, self.model.h = event.size
        self.redraw()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        c = event.character
        k = (
            c
            if c and len(c) == 1 and c.isprintable()
            else KEY_NAMES.get(event.key, event.key)
        )
        self.run_cmd(self.model.key(k))
        self.redraw()

    def action_help_quit(self) -> None:  # Textual binds ctrl+c to this
        self.run_cmd(self.model.key("ctrl+c"))

    def run_cmd(self, cmd: Cmd | None) -> None:
        if cmd is quit_cmd:
            self.exit()
        elif cmd is not None:
            self.run_worker(
                lambda: self.call_from_thread(self.deliver, cmd()), thread=True
            )

    def deliver(self, msg: Msg | None) -> None:
        if msg is not None:
            self.run_cmd(self.model.update(msg))
        self.redraw()

    def redraw(self) -> None:
        t = self.model.view()
        t.no_wrap, t.overflow = True, "crop"
        self.query_one(Static).update(t)


def main() -> None:
    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        sys.exit(str(e))
    m = Model(cfg, Client(cfg.base_url, cfg.username, cfg.api_key))
    m.busy, m.msg = True, "loading board…"
    Squizzer(m).run()
    # Don't wait on an in-flight request's worker thread; Go just exits too.
    os._exit(0)
