package main

import "testing"

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
