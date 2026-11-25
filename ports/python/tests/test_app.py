from jira_squizzer.app import (
    Config,
    Model,
    approvals,
    card,
    find_transition,
    group,
    ticket_key,
)
from jira_squizzer.jira import PR, Column, Issue, Named, Reviewer, Transition


def issue(key: str, status: str, summary: str = "") -> Issue:
    return Issue(key=key, status_id=status, summary=summary)


def test_group_and_transition() -> None:
    cols = [Column("To Do", ["1"]), Column("Doing", ["3", "4"]), Column("Done", ["5"])]
    lanes = group(
        cols, [issue("A", "1"), issue("B", "4"), issue("C", "99"), issue("D", "3")]
    )
    assert [len(lane) for lane in lanes] == [1, 2, 0] and lanes[1][1].key == "D", lanes

    ts = [
        Transition("11", "1", to_name="To Do"),
        Transition("21", "4", to_name="Review"),
    ]
    tr = find_transition(ts, cols[1].statuses)
    assert tr is not None and tr.id == "21", tr
    assert find_transition(ts, cols[2].statuses) is None, (
        "expected no transition into Done"
    )


def test_card() -> None:
    i = issue("CHK-1", "1", "a fairly long summary that must wrap")
    i.assignee = Named(display_name="Ada Byron Lovelace")
    c = card(i, 14, True)
    assert c[0].plain == "CHK-1       AL" and len(c) >= 3, c
    for line in c:
        assert len(line.plain) == 14, f"line not padded to width: {line.plain!r}"


def test_same_height() -> None:
    def short(k: str) -> Issue:
        return issue(k, "1", "x")

    def long(k: str) -> Issue:
        return issue(k, "1", "a summary long enough to wrap onto several lines")

    # width 40 over 2 lanes → 16-wide cards. Short cards are 2 lines, long ones 4+.
    m = Model(w=40, h=40, row=[3, 0])
    m.b.lanes = [
        [short("A"), short("B"), short("C"), short("D")],  # D's top at y=9
        [long("E"), long("F"), long("G")],  # F covers y=9
    ]
    assert m.same_height(1) == 1, "want F (1)"
    m.col, m.row = 1, [0, 2]  # G's top is lower than any card in lane 0 → last card
    assert m.same_height(0) == 3, "want D (3)"


def test_approvals() -> None:
    pr = PR()
    assert approvals(pr).plain == "", "no reviewers should render nothing"
    pr.reviewers = [Reviewer("Ada", True), Reviewer("Bob", False)]
    got = approvals(pr).plain
    assert got.startswith("1/2 approved") and "· Bob" in got, got


def test_filter() -> None:
    cases = {" 123 ": "CHK-123", "abc-9": "ABC-9", "login": "", "": "", "CHK-": ""}
    for q, want in cases.items():
        assert ticket_key("chk", q) == want, q

    m = Model(cfg=Config(project="CHK"), row=[0, 0])
    m.all = [
        [issue("CHK-1", "1", "Fix Login page")],
        [issue("CHK-2", "1", "cleanup"), issue("CHK-42", "1", "Login redirect")],
    ]
    m.filter("")
    for keys, shown in [
        (["/", "l", "o", "g"], ["CHK-1", "CHK-42"]),  # title, case-insensitive, live
        (["enter"], ["CHK-1", "CHK-42"]),  # filter sticks
        (["/", "backspace", "backspace", "backspace", "4", "2", "enter"], ["CHK-42"]),
        (["esc"], ["CHK-1", "CHK-2", "CHK-42"]),  # cleared
    ]:
        for k in keys:
            m.key(k)
        assert [i.key for lane in m.b.lanes for i in lane] == shown, keys
    # 42 filtered: cursor hopped out of the now-empty first lane
    for k in ["/", "4", "2"]:
        m.key(k)
    assert m.col == 1, "cursor should move to the lane with the match"
    # a number that isn't on the board is fetched from Jira
    m.key("esc")
    m.key("/")
    for k in ["9", "9", "enter"]:
        cmd = m.key(k)
    assert m.busy and cmd is not None and m.query == "", (
        "expected CHK-99 to be loaded from Jira with the filter cleared"
    )
