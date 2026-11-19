package main

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func issue(key, status string) Issue {
	var i Issue
	i.Key = key
	i.Fields.Status.ID = status
	return i
}

func TestGroupAndTransition(t *testing.T) {
	cols := []Column{{"To Do", []string{"1"}}, {"Doing", []string{"3", "4"}}, {"Done", []string{"5"}}}
	lanes := group(cols, []Issue{issue("A", "1"), issue("B", "4"), issue("C", "99"), issue("D", "3")})
	if len(lanes[0]) != 1 || len(lanes[1]) != 2 || len(lanes[2]) != 0 || lanes[1][1].Key != "D" {
		t.Fatalf("bad grouping: %+v", lanes)
	}

	ts := []Transition{{ID: "11", To: struct{ ID, Name string }{"1", "To Do"}}, {ID: "21", To: struct{ ID, Name string }{"4", "Review"}}}
	if tr, ok := findTransition(ts, cols[1].Statuses); !ok || tr.ID != "21" {
		t.Fatalf("want transition 21, got %+v %v", tr, ok)
	}
	if _, ok := findTransition(ts, cols[2].Statuses); ok {
		t.Fatal("expected no transition into Done")
	}
}

func TestCard(t *testing.T) {
	is := issue("CHK-1", "1")
	is.Fields.Summary = "a fairly long summary that must wrap"
	is.Fields.Assignee = &Named{DisplayName: "Ada Byron Lovelace"}
	c := card(is, 14, true)
	if c[0] != "CHK-1       AL" || len(c) < 3 {
		t.Fatalf("bad card: %q", c)
	}
	for _, l := range c {
		if len([]rune(l)) != 14 {
			t.Fatalf("line not padded to width: %q", l)
		}
	}
}

func TestSameHeight(t *testing.T) {
	short := func(k string) Issue { i := issue(k, "1"); i.Fields.Summary = "x"; return i }
	long := func(k string) Issue {
		i := issue(k, "1")
		i.Fields.Summary = "a summary long enough to wrap onto several lines"
		return i
	}
	// width 40 over 2 lanes → 16-wide cards. Short cards are 2 lines, long ones 4+.
	m := model{w: 40, h: 40, row: []int{3, 0}}
	m.b.lanes = [][]Issue{
		{short("A"), short("B"), short("C"), short("D")}, // D's top at y=9
		{long("E"), long("F"), long("G")},                // F covers y=9
	}
	if got := m.sameHeight(1); got != 1 {
		t.Fatalf("want F (1), got %d", got)
	}
	m.col, m.row = 1, []int{0, 2} // G's top is lower than any card in lane 0 → last card
	if got := m.sameHeight(0); got != 3 {
		t.Fatalf("want D (3), got %d", got)
	}
}

func TestApprovals(t *testing.T) {
	var pr PR
	if approvals(pr) != "" {
		t.Fatal("no reviewers should render nothing")
	}
	pr.Reviewers = []Reviewer{{"Ada", true}, {"Bob", false}}
	if got := approvals(pr); !strings.HasPrefix(got, "1/2 approved") || !strings.Contains(got, "· Bob") {
		t.Fatalf("bad approvals: %q", got)
	}
}

func TestFilter(t *testing.T) {
	for q, want := range map[string]string{" 123 ": "CHK-123", "abc-9": "ABC-9", "login": "", "": "", "CHK-": ""} {
		if got := ticketKey("chk", q); got != want {
			t.Fatalf("ticketKey(%q) = %q, want %q", q, got, want)
		}
	}
	titled := func(k, s string) Issue { i := issue(k, "1"); i.Fields.Summary = s; return i }
	m := model{cfg: Config{Project: "CHK"}, row: []int{0, 0}}
	m.all = [][]Issue{
		{titled("CHK-1", "Fix Login page")},
		{titled("CHK-2", "cleanup"), titled("CHK-42", "Login redirect")},
	}
	m.filter("")
	type step struct {
		keys  []string
		shown []string
	}
	var tm tea.Model = m
	for _, st := range []step{
		{[]string{"/", "l", "o", "g"}, []string{"CHK-1", "CHK-42"}}, // title, case-insensitive, live
		{[]string{"enter"}, []string{"CHK-1", "CHK-42"}},            // filter sticks
		{[]string{"/", "backspace", "backspace", "backspace", "4", "2", "enter"}, []string{"CHK-42"}},
		{[]string{"esc"}, []string{"CHK-1", "CHK-2", "CHK-42"}}, // cleared
	} {
		for _, k := range st.keys {
			tm, _ = tm.(model).key(k)
		}
		var shown []string
		for _, lane := range tm.(model).b.lanes {
			for _, is := range lane {
				shown = append(shown, is.Key)
			}
		}
		if strings.Join(shown, ",") != strings.Join(st.shown, ",") {
			t.Fatalf("after %v: shown %v, want %v", st.keys, shown, st.shown)
		}
	}
	// 42 filtered: cursor hopped out of the now-empty first lane
	tm, _ = tm.(model).key("/")
	for _, k := range []string{"4", "2"} {
		tm, _ = tm.(model).key(k)
	}
	if tm.(model).col != 1 {
		t.Fatal("cursor should move to the lane with the match")
	}
	// a number that isn't on the board is fetched from Jira
	tm, _ = tm.(model).key("esc")
	tm, _ = tm.(model).key("/")
	var cmd tea.Cmd
	for _, k := range []string{"9", "9", "enter"} {
		tm, cmd = tm.(model).key(k)
	}
	if !tm.(model).busy || cmd == nil || tm.(model).query != "" {
		t.Fatal("expected CHK-99 to be loaded from Jira with the filter cleared")
	}
}
