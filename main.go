package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type Config struct {
	BaseURL  string `json:"base_url"`
	Username string `json:"username"`
	Project  string `json:"project"`
	APIKey   string `json:"api_key"`
	BoardID  int    `json:"board_id"` // optional; defaults to the project's first scrum board
}

func loadConfig() (Config, error) {
	var c Config
	dir, _ := os.UserConfigDir()
	path := filepath.Join(dir, "jira-squizzer", "config.json")
	if b, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(b, &c); err != nil {
			return c, fmt.Errorf("%s: %w", path, err)
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return c, err
	}
	for env, dst := range map[string]*string{
		"JIRA_BASE_URL": &c.BaseURL, "JIRA_USERNAME": &c.Username,
		"JIRA_PROJECT": &c.Project, "JIRA_API_KEY": &c.APIKey,
	} {
		if v := os.Getenv(env); v != "" {
			*dst = v
		}
	}
	if c.BaseURL == "" || c.Username == "" || c.Project == "" || c.APIKey == "" {
		return c, fmt.Errorf("missing config: set base_url, username, project, api_key in %s\nor JIRA_BASE_URL, JIRA_USERNAME, JIRA_PROJECT, JIRA_API_KEY", path)
	}
	return c, nil
}

// group buckets issues into lanes by status; issues in unmapped statuses are dropped, as on the Jira board.
func group(cols []Column, issues []Issue) [][]Issue {
	lanes := make([][]Issue, len(cols))
	for _, is := range issues {
		for i, c := range cols {
			if slices.Contains(c.Statuses, is.Fields.Status.ID) {
				lanes[i] = append(lanes[i], is)
				break
			}
		}
	}
	return lanes
}

func findTransition(ts []Transition, statuses []string) (Transition, bool) {
	for _, t := range ts {
		if slices.Contains(statuses, t.To.ID) {
			return t, true
		}
	}
	return Transition{}, false
}

type board struct {
	sprint string
	cols   []Column
	lanes  [][]Issue
}

type detail struct {
	issue Issue
	prs   []PR
	prErr error
}

type boardMsg struct {
	b   board
	err error
}
type detailMsg struct {
	d   detail
	err error
}
type movedMsg struct {
	key, to string
	err     error
}

type model struct {
	c        *Client
	cfg      Config
	b        board
	col      int
	row      []int // cursor per lane
	detail   *detail
	scroll   int
	pendingG bool
	follow   string // issue key to put the cursor on after the next reload
	busy     bool
	msg      string
	w, h     int
}

func fetchBoard(c *Client, cfg Config) (board, error) {
	id := cfg.BoardID
	if id == 0 {
		var err error
		if id, err = c.BoardID(cfg.Project); err != nil {
			return board{}, err
		}
	}
	cols, err := c.Columns(id)
	if err != nil {
		return board{}, err
	}
	sid, name, err := c.ActiveSprint(id)
	if err != nil {
		return board{}, err
	}
	issues, err := c.SprintIssues(sid)
	if err != nil {
		return board{}, err
	}
	return board{name, cols, group(cols, issues)}, nil
}

func (m model) loadBoard() tea.Cmd {
	c, cfg := m.c, m.cfg
	return func() tea.Msg {
		b, err := fetchBoard(c, cfg)
		return boardMsg{b, err}
	}
}

func (m model) loadDetail(key string) tea.Cmd {
	c := m.c
	return func() tea.Msg {
		is, err := c.Issue(key)
		if err != nil {
			return detailMsg{err: err}
		}
		prs, prErr := c.PRs(is.ID)
		return detailMsg{d: detail{is, prs, prErr}}
	}
}

func (m model) move(is Issue, target Column) tea.Cmd {
	c := m.c
	return func() tea.Msg {
		ts, err := c.Transitions(is.Key)
		if err != nil {
			return movedMsg{err: err}
		}
		t, ok := findTransition(ts, target.Statuses)
		if !ok {
			return movedMsg{err: fmt.Errorf("%s: no workflow transition into %q", is.Key, target.Name)}
		}
		return movedMsg{key: is.Key, to: target.Name, err: c.Transition(is.Key, t.ID)}
	}
}

func (m model) Init() tea.Cmd { return m.loadBoard() }

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.w, m.h = msg.Width, msg.Height
	case boardMsg:
		m.busy = false
		if msg.err != nil {
			m.msg = msg.err.Error()
			return m, nil
		}
		m.b = msg.b
		if len(m.row) != len(m.b.lanes) {
			m.row = make([]int, len(m.b.lanes))
		}
		m.col = clamp(m.col, 0, len(m.b.lanes)-1)
		for i := range m.row {
			m.row[i] = clamp(m.row[i], 0, len(m.b.lanes[i])-1)
		}
		if m.follow == "" {
			m.msg = ""
		}
		for i, lane := range m.b.lanes {
			for j, is := range lane {
				if is.Key == m.follow {
					m.col, m.row[i] = i, j
				}
			}
		}
		m.follow = ""
	case detailMsg:
		m.busy = false
		if msg.err != nil {
			m.msg = msg.err.Error()
			return m, nil
		}
		m.msg, m.detail, m.scroll = "", &msg.d, 0
	case movedMsg:
		if msg.err != nil {
			m.busy, m.msg = false, msg.err.Error()
			return m, nil
		}
		m.msg, m.follow = msg.key+" → "+msg.to, msg.key
		return m, m.loadBoard()
	case tea.KeyMsg:
		return m.key(msg.String())
	}
	return m, nil
}

func (m model) key(k string) (tea.Model, tea.Cmd) {
	if k == "ctrl+c" {
		return m, tea.Quit
	}
	g := m.pendingG
	m.pendingG = false

	if m.detail != nil {
		switch k {
		case "q", "esc", "h":
			m.detail = nil
			return m, nil
		case "j", "down":
			m.scroll++
		case "k", "up":
			m.scroll--
		case "ctrl+d":
			m.scroll += m.h / 2
		case "ctrl+u":
			m.scroll -= m.h / 2
		case "G":
			m.scroll = 1 << 30
		case "g":
			if g {
				m.scroll = 0
			} else {
				m.pendingG = true
			}
		}
		m.scroll = clamp(m.scroll, 0, len(m.detailLines())-m.h+1)
		return m, nil
	}

	if k == "q" {
		return m, tea.Quit
	}
	if m.busy {
		return m, nil
	}
	if k == "r" {
		m.busy, m.msg = true, "reloading…"
		return m, m.loadBoard()
	}
	if len(m.b.lanes) == 0 {
		return m, nil
	}
	lane := m.b.lanes[m.col]
	switch k {
	case "h", "left", "l", "right":
		t := m.col - 1
		if k == "l" || k == "right" {
			t = m.col + 1
		}
		if t >= 0 && t < len(m.b.lanes) {
			m.row[t] = m.sameHeight(t)
			m.col = t
		}
	case "j", "down":
		m.row[m.col]++
	case "k", "up":
		m.row[m.col]--
	case "G":
		m.row[m.col] = len(lane) - 1
	case "g":
		if g {
			m.row[m.col] = 0
		} else {
			m.pendingG = true
		}
	case "enter", "o":
		if len(lane) > 0 {
			key := lane[m.row[m.col]].Key
			m.busy, m.msg = true, "loading "+key+"…"
			return m, m.loadDetail(key)
		}
	case "H", "L":
		t := m.col - 1
		if k == "L" {
			t = m.col + 1
		}
		if len(lane) > 0 && t >= 0 && t < len(m.b.cols) {
			is := lane[m.row[m.col]]
			m.busy, m.msg = true, "moving "+is.Key+"…"
			return m, m.move(is, m.b.cols[t])
		}
	}
	m.col = clamp(m.col, 0, len(m.b.lanes)-1)
	m.row[m.col] = clamp(m.row[m.col], 0, len(m.b.lanes[m.col])-1)
	return m, nil
}

// Gruvbox dark palette.
var (
	gbFg     = lipgloss.Color("#ebdbb2")
	gbGray   = lipgloss.Color("#928374")
	gbBg2    = lipgloss.Color("#504945")
	gbBg3    = lipgloss.Color("#665c54")
	gbRed    = lipgloss.Color("#fb4934")
	gbGreen  = lipgloss.Color("#b8bb26")
	gbYellow = lipgloss.Color("#fabd2f")
	gbBlue   = lipgloss.Color("#83a598")
	gbPurple = lipgloss.Color("#d3869b")
	gbAqua   = lipgloss.Color("#8ec07c")
	gbOrange = lipgloss.Color("#fe8019")

	text      = lipgloss.NewStyle().Foreground(gbFg)
	faint     = lipgloss.NewStyle().Foreground(gbGray)
	title     = lipgloss.NewStyle().Foreground(gbYellow).Bold(true)
	heading   = lipgloss.NewStyle().Foreground(gbAqua).Bold(true)
	keyStyle  = lipgloss.NewStyle().Foreground(gbBlue).Bold(true)
	initStyle = lipgloss.NewStyle().Foreground(gbPurple)
	msgStyle  = lipgloss.NewStyle().Foreground(gbOrange)
	laneStyle = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(gbBg3).Padding(0, 1)
	activeSt  = laneStyle.BorderForeground(gbYellow)
	prColours = map[string]lipgloss.Color{"OPEN": gbGreen, "MERGED": gbPurple, "DECLINED": gbRed}
)

const boardHelp = "h/l lane · j/k ticket · gg/G top/bottom · enter open · H/L move ticket · r reload · q quit"
const detailHelp = "j/k scroll · ctrl+d/u page · gg/G top/bottom · q/esc back"

func (m model) footer(help string) string {
	if m.msg != "" {
		return msgStyle.Render(m.msg)
	}
	return faint.Render(help)
}

func (m model) View() string {
	if m.w == 0 {
		return ""
	}
	if m.detail != nil {
		lines := m.detailLines()
		end := min(len(lines), m.scroll+m.h-1)
		body := strings.Join(lines[m.scroll:end], "\n")
		return lipgloss.NewStyle().Height(m.h-1).Render(body) + "\n" + m.footer(detailHelp)
	}

	header := title.Render(m.cfg.Project + " · " + m.b.sprint)
	if len(m.b.lanes) == 0 {
		return header + "\n\n" + m.footer(boardHelp)
	}
	cw, ch := m.dims()
	var boxes []string
	for i, lane := range m.b.lanes {
		rows := []string{heading.Render(trunc(fmt.Sprintf("%s (%d)", m.b.cols[i].Name, len(lane)), cw))}
		cards := m.laneCards(i, cw)
		start := scrollStart(cards, m.row[i], ch)
		var lines []string
		for j := start; j < len(lane) && len(lines) < ch; j++ {
			if j > start {
				lines = append(lines, "")
			}
			lines = append(lines, cards[j]...)
		}
		rows = append(rows, lines[:min(len(lines), ch)]...)
		st := laneStyle
		if i == m.col {
			st = activeSt
		}
		boxes = append(boxes, st.Width(cw+2).Height(ch+1).Render(strings.Join(rows, "\n")))
	}
	return header + "\n" + lipgloss.JoinHorizontal(lipgloss.Top, boxes...) + "\n" + m.footer(boardHelp)
}

func (m model) detailLines() []string {
	d := m.detail
	f := d.issue.Fields
	var b strings.Builder
	fmt.Fprintf(&b, "%s  %s\n", keyStyle.Render(d.issue.Key), title.Render(f.Summary))
	fmt.Fprintf(&b, "%s · %s · %s\n", name(f.IssueType), f.Status.Name, name(f.Priority))
	fmt.Fprintf(&b, "Assignee: %s   Reporter: %s\n", name(f.Assignee), name(f.Reporter))
	fmt.Fprintf(&b, "%s\n\n", faint.Render(strings.TrimRight(m.cfg.BaseURL, "/")+"/browse/"+d.issue.Key))
	b.WriteString(heading.Render("Pull requests") + "\n")
	switch {
	case d.prErr != nil:
		fmt.Fprintf(&b, "  couldn't load: %v\n", d.prErr)
	case len(d.prs) == 0:
		b.WriteString("  none\n")
	}
	for _, pr := range d.prs {
		status := lipgloss.NewStyle().Foreground(prColours[pr.Status]).Render("[" + pr.Status + "]")
		fmt.Fprintf(&b, "  %s %s %s %s\n    %s\n", status, pr.RepositoryName, pr.ID, pr.Name, faint.Render(pr.URL))
	}
	desc := strings.ReplaceAll(f.Description, "\r", "")
	if desc == "" {
		desc = faint.Render("(no description)")
	}
	b.WriteString("\n" + heading.Render("Description") + "\n" + desc)
	return strings.Split(text.Width(max(m.w-1, 20)).Render(b.String()), "\n")
}

// dims returns the card width and the number of card lines visible per lane.
func (m model) dims() (cw, ch int) {
	return max(m.w/max(len(m.b.lanes), 1)-4, 5), // 4 = border + padding
		max(m.h-5, 1) // title, footer, borders, lane header
}

func (m model) laneCards(i, cw int) [][]string {
	cards := make([][]string, len(m.b.lanes[i]))
	for j, is := range m.b.lanes[i] {
		cards[j] = card(is, cw, i == m.col && j == m.row[i])
	}
	return cards
}

// scrollStart picks the first visible card so sel is on screen with as many cards above it as fit.
func scrollStart(cards [][]string, sel, ch int) int {
	if len(cards) == 0 {
		return 0
	}
	start := sel
	for h := len(cards[sel]); start > 0 && h+len(cards[start-1])+1 <= ch; start-- {
		h += len(cards[start-1]) + 1
	}
	return start
}

// sameHeight returns the card in lane t that sits at the screen height of the current selection.
func (m model) sameHeight(t int) int {
	cw, ch := m.dims()
	cur := m.laneCards(m.col, cw)
	y := 0
	for j := scrollStart(cur, m.row[m.col], ch); j < m.row[m.col]; j++ {
		y += len(cur[j]) + 1
	}
	cards := m.laneCards(t, cw)
	start := scrollStart(cards, m.row[t], ch)
	best := start
	for j, top := start, 0; j < len(cards) && top <= y; j++ {
		best, top = j, top+len(cards[j])+1
	}
	return best
}

// card renders an issue as "KEY ... AB" followed by the summary wrapped to w; every line is padded to w.
// Styles are applied piecewise (with the selection background on each) since nested ANSI resets would drop it.
func card(is Issue, w int, sel bool) []string {
	bg := lipgloss.NewStyle()
	if sel {
		bg = bg.Background(gbBg2)
	}
	in := initials(is.Fields.Assignee)
	var head string
	if gap := w - len([]rune(is.Key)) - len([]rune(in)); gap >= 1 {
		head = keyStyle.Inherit(bg).Render(is.Key) + bg.Render(strings.Repeat(" ", gap)) + initStyle.Inherit(bg).Render(in)
	} else {
		head = keyStyle.Inherit(bg).Width(w).Render(trunc(is.Key, w))
	}
	body := text.Inherit(bg).Width(w).Render(is.Fields.Summary)
	return append([]string{head}, strings.Split(body, "\n")...)
}

// initials takes the first letter of the first and last words: "Ada Byron Lovelace" → "AL".
func initials(n *Named) string {
	if n == nil {
		return ""
	}
	words := strings.Fields(n.DisplayName)
	if len(words) == 0 {
		return ""
	}
	s := []rune(words[0])[:1]
	if len(words) > 1 {
		s = append(s, []rune(words[len(words)-1])[0])
	}
	return strings.ToUpper(string(s))
}

func name(n *Named) string {
	switch {
	case n == nil:
		return "—"
	case n.DisplayName != "":
		return n.DisplayName
	}
	return n.Name
}

func trunc(s string, w int) string {
	r := []rune(s)
	if len(r) <= w {
		return s
	}
	return string(r[:w-1]) + "…"
}

func clamp(x, lo, hi int) int {
	return max(min(x, hi), lo)
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m := model{c: NewClient(cfg), cfg: cfg, busy: true, msg: "loading board…"}
	if _, err := tea.NewProgram(m, tea.WithAltScreen()).Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
