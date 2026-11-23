use super::*;
use jira::{Reviewer, Status};

fn issue(key: &str, status: &str) -> Issue {
    let mut i = Issue {
        key: key.into(),
        ..Default::default()
    };
    i.fields.status.id = status.into();
    i
}

fn col(name: &str, statuses: &[&str]) -> Column {
    Column {
        name: name.into(),
        statuses: statuses.iter().map(|s| s.to_string()).collect(),
    }
}

#[test]
fn test_group_and_transition() {
    let cols = [
        col("To Do", &["1"]),
        col("Doing", &["3", "4"]),
        col("Done", &["5"]),
    ];
    let lanes = group(
        &cols,
        vec![
            issue("A", "1"),
            issue("B", "4"),
            issue("C", "99"),
            issue("D", "3"),
        ],
    );
    assert!(
        lanes[0].len() == 1 && lanes[1].len() == 2 && lanes[2].is_empty() && lanes[1][1].key == "D",
        "bad grouping: {lanes:?}"
    );

    let to = |id: &str, name: &str| Status {
        id: id.into(),
        name: name.into(),
    };
    let ts = [
        Transition {
            id: "11".into(),
            to: to("1", "To Do"),
            ..Default::default()
        },
        Transition {
            id: "21".into(),
            to: to("4", "Review"),
            ..Default::default()
        },
    ];
    let tr = find_transition(&ts, &cols[1].statuses);
    assert_eq!(tr.map(|t| t.id.as_str()), Some("21"), "want transition 21");
    assert!(
        find_transition(&ts, &cols[2].statuses).is_none(),
        "expected no transition into Done"
    );
}

#[test]
fn test_card() {
    let mut is = issue("CHK-1", "1");
    is.fields.summary = "a fairly long summary that must wrap".into();
    is.fields.assignee = Some(Named {
        display_name: "Ada Byron Lovelace".into(),
        ..Default::default()
    });
    let c: Vec<String> = card(&is, 14, true).iter().map(|l| l.to_string()).collect();
    assert!(c[0] == "CHK-1       AL" && c.len() >= 3, "bad card: {c:?}");
    for l in &c {
        assert_eq!(l.chars().count(), 14, "line not padded to width: {l:?}");
    }
}

#[test]
fn test_same_height() {
    let short = |k: &str| {
        let mut i = issue(k, "1");
        i.fields.summary = "x".into();
        i
    };
    let long = |k: &str| {
        let mut i = issue(k, "1");
        i.fields.summary = "a summary long enough to wrap onto several lines".into();
        i
    };
    // width 40 over 2 lanes → 16-wide cards. Short cards are 2 lines, long ones 4+.
    let mut m = Model {
        w: 40,
        h: 40,
        row: vec![3, 0],
        ..Default::default()
    };
    m.b.lanes = vec![
        vec![short("A"), short("B"), short("C"), short("D")], // D's top at y=9
        vec![long("E"), long("F"), long("G")],                // F covers y=9
    ];
    assert_eq!(m.same_height(1), 1, "want F (1)");
    (m.col, m.row) = (1, vec![0, 2]); // G's top is lower than any card in lane 0 → last card
    assert_eq!(m.same_height(0), 3, "want D (3)");
}

#[test]
fn test_approvals() {
    let mut pr = PR::default();
    assert_eq!(
        approvals(&pr).to_string(),
        "",
        "no reviewers should render nothing"
    );
    pr.reviewers = vec![
        Reviewer {
            name: "Ada".into(),
            approved: true,
        },
        Reviewer {
            name: "Bob".into(),
            approved: false,
        },
    ];
    let got = approvals(&pr).to_string();
    assert!(
        got.starts_with("1/2 approved") && got.contains("· Bob"),
        "bad approvals: {got:?}"
    );
}

#[test]
fn test_filter() {
    for (q, want) in [
        (" 123 ", "CHK-123"),
        ("abc-9", "ABC-9"),
        ("login", ""),
        ("", ""),
        ("CHK-", ""),
    ] {
        assert_eq!(ticket_key("chk", q), want, "ticket_key({q:?})");
    }
    let titled = |k: &str, s: &str| {
        let mut i = issue(k, "1");
        i.fields.summary = s.into();
        i
    };
    let mut m = Model {
        row: vec![0, 0],
        ..Default::default()
    };
    m.cfg.project = "CHK".into();
    m.all = vec![
        vec![titled("CHK-1", "Fix Login page")],
        vec![
            titled("CHK-2", "cleanup"),
            titled("CHK-42", "Login redirect"),
        ],
    ];
    m.filter("");
    let steps: [(&[&str], &[&str]); 4] = [
        (&["/", "l", "o", "g"], &["CHK-1", "CHK-42"]), // title, case-insensitive, live
        (&["enter"], &["CHK-1", "CHK-42"]),            // filter sticks
        (
            &[
                "/",
                "backspace",
                "backspace",
                "backspace",
                "4",
                "2",
                "enter",
            ],
            &["CHK-42"],
        ),
        (&["esc"], &["CHK-1", "CHK-2", "CHK-42"]), // cleared
    ];
    for (keys, want) in steps {
        for k in keys {
            m.key(k);
        }
        let shown: Vec<&str> =
            m.b.lanes
                .iter()
                .flatten()
                .map(|is| is.key.as_str())
                .collect();
        assert_eq!(shown, want, "after {keys:?}");
    }
    // 42 filtered: cursor hopped out of the now-empty first lane
    for k in ["/", "4", "2"] {
        m.key(k);
    }
    assert_eq!(m.col, 1, "cursor should move to the lane with the match");
    // a number that isn't on the board is fetched from Jira
    m.key("esc");
    m.key("/");
    let mut cmd = None;
    for k in ["9", "9", "enter"] {
        cmd = m.key(k);
    }
    assert!(
        m.busy
            && matches!(cmd, Some(Cmd::LoadDetail(ref k)) if k == "CHK-99")
            && m.query.is_empty(),
        "expected CHK-99 to be loaded from Jira with the filter cleared"
    );
}
