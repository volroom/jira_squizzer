import assert from "node:assert/strict";
import { test } from "node:test";
import chalk from "chalk";
import type { Cmd } from "./model.js";
import type { Column, Config, Issue, PR, Transition } from "./jira.js";
import { approvals, card, findTransition, group, Model, ticketKey } from "./model.js";

// node --test forces colour in a TTY; go test never has one, so lipgloss renders plain there.
chalk.level = 0;

function issue(key: string, status: string, summary = ""): Issue {
  return { id: "", key, fields: { summary, status: { id: status, name: "" } } };
}

test("GroupAndTransition", () => {
  const cols: Column[] = [
    { name: "To Do", statuses: ["1"] },
    { name: "Doing", statuses: ["3", "4"] },
    { name: "Done", statuses: ["5"] },
  ];
  const lanes = group(cols, [issue("A", "1"), issue("B", "4"), issue("C", "99"), issue("D", "3")]);
  assert.ok(lanes[0].length === 1 && lanes[1].length === 2 && lanes[2].length === 0 && lanes[1][1].key === "D", "bad grouping");

  const ts: Transition[] = [
    { id: "11", name: "", to: { id: "1", name: "To Do" } },
    { id: "21", name: "", to: { id: "4", name: "Review" } },
  ];
  assert.equal(findTransition(ts, cols[1].statuses)?.id, "21");
  assert.equal(findTransition(ts, cols[2].statuses), undefined, "expected no transition into Done");
});

test("Card", () => {
  const is = issue("CHK-1", "1", "a fairly long summary that must wrap");
  is.fields.assignee = { displayName: "Ada Byron Lovelace" };
  const c = card(is, 14, true);
  assert.ok(c[0] === "CHK-1       AL" && c.length >= 3, `bad card: ${JSON.stringify(c)}`);
  for (const l of c) assert.equal([...l].length, 14, `line not padded to width: ${JSON.stringify(l)}`);
});

test("SameHeight", () => {
  const short = (k: string) => issue(k, "1", "x");
  const long = (k: string) => issue(k, "1", "a summary long enough to wrap onto several lines");
  // width 40 over 2 lanes → 16-wide cards. Short cards are 2 lines, long ones 4+.
  const m = Object.assign(new Model(), { w: 40, h: 40, row: [3, 0] });
  m.b.lanes = [
    [short("A"), short("B"), short("C"), short("D")], // D's top at y=9
    [long("E"), long("F"), long("G")], // F covers y=9
  ];
  assert.equal(m.sameHeight(1), 1, "want F (1)");
  m.col = 1;
  m.row = [0, 2]; // G's top is lower than any card in lane 0 → last card
  assert.equal(m.sameHeight(0), 3, "want D (3)");
});

test("Approvals", () => {
  const pr: PR = { id: "", name: "", url: "", status: "", repositoryName: "" };
  assert.equal(approvals(pr), "", "no reviewers should render nothing");
  pr.reviewers = [
    { name: "Ada", approved: true },
    { name: "Bob", approved: false },
  ];
  const got = approvals(pr);
  assert.ok(got.startsWith("1/2 approved") && got.includes("· Bob"), `bad approvals: ${JSON.stringify(got)}`);
});

test("Filter", () => {
  for (const [q, want] of Object.entries({ " 123 ": "CHK-123", "abc-9": "ABC-9", login: "", "": "", "CHK-": "" })) {
    assert.equal(ticketKey("chk", q), want, `ticketKey(${JSON.stringify(q)})`);
  }
  const titled = (k: string, s: string) => issue(k, "1", s);
  const m = Object.assign(new Model(), { cfg: { project: "CHK" } as Config, row: [0, 0] });
  m.all = [[titled("CHK-1", "Fix Login page")], [titled("CHK-2", "cleanup"), titled("CHK-42", "Login redirect")]];
  m.filter("");
  const steps: [string[], string[]][] = [
    [["/", "l", "o", "g"], ["CHK-1", "CHK-42"]], // title, case-insensitive, live
    [["enter"], ["CHK-1", "CHK-42"]], // filter sticks
    [["/", "backspace", "backspace", "backspace", "4", "2", "enter"], ["CHK-42"]],
    [["esc"], ["CHK-1", "CHK-2", "CHK-42"]], // cleared
  ];
  for (const [keys, shown] of steps) {
    for (const k of keys) m.key(k);
    assert.deepEqual(m.b.lanes.flat().map((is) => is.key), shown, `after ${keys}`);
  }
  // 42 filtered: cursor hopped out of the now-empty first lane
  m.key("/");
  for (const k of ["4", "2"]) m.key(k);
  assert.equal(m.col, 1, "cursor should move to the lane with the match");
  // a number that isn't on the board is fetched from Jira
  m.key("esc");
  m.key("/");
  let cmd: Cmd = null;
  for (const k of ["9", "9", "enter"]) cmd = m.key(k);
  assert.ok(m.busy && cmd !== null && m.query === "", "expected CHK-99 to be loaded from Jira with the filter cleared");
});
