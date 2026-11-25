"""Jira Cloud REST client: board, columns, active sprint, issues, transitions and linked PRs."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class JiraError(Exception):
    pass


@dataclass
class Named:
    """Jira's {name} and {displayName} objects (user, priority, issuetype)."""

    name: str = ""
    display_name: str = ""


@dataclass
class Issue:
    key: str = ""
    id: str = ""
    summary: str = ""
    description: str = ""
    status_id: str = ""
    status_name: str = ""
    assignee: Named | None = None
    reporter: Named | None = None
    issue_type: Named | None = None
    priority: Named | None = None


@dataclass
class Column:
    name: str
    statuses: list[str]  # status IDs


@dataclass
class Transition:
    id: str
    to_id: str
    name: str = ""
    to_name: str = ""


@dataclass
class Reviewer:
    name: str
    approved: bool


@dataclass
class PR:
    id: str = ""
    name: str = ""
    url: str = ""
    status: str = ""
    repository_name: str = ""
    reviewers: list[Reviewer] = field(default_factory=list)


def _named(d: dict[str, Any] | None) -> Named | None:
    return Named(d.get("name") or "", d.get("displayName") or "") if d else None


def _issue(d: dict[str, Any]) -> Issue:
    f = d.get("fields") or {}
    st = f.get("status") or {}
    return Issue(
        key=d.get("key") or "",
        id=d.get("id") or "",
        summary=f.get("summary") or "",
        description=f.get("description") or "",
        status_id=st.get("id") or "",
        status_name=st.get("name") or "",
        assignee=_named(f.get("assignee")),
        reporter=_named(f.get("reporter")),
        issue_type=_named(f.get("issuetype")),
        priority=_named(f.get("priority")),
    )


def _pr(d: dict[str, Any]) -> PR:
    return PR(
        id=d.get("id") or "",
        name=d.get("name") or "",
        url=d.get("url") or "",
        status=d.get("status") or "",
        repository_name=d.get("repositoryName") or "",
        reviewers=[
            Reviewer(r.get("name") or "", bool(r.get("approved")))
            for r in d.get("reviewers") or []
        ],
    )


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class Client:
    def __init__(self, base_url: str, username: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{api_key}".encode()).decode()
        self.auth = "Basic " + token

    def do(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        url = self.base + path
        req = urllib.request.Request(url, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            msg = e.read(500).decode(errors="replace").strip()
            raise JiraError(f"{method} {path}: {e.code} {e.reason} {msg}") from e
        except (urllib.error.URLError, OSError) as e:
            raise JiraError(f'{method} "{url}": {getattr(e, "reason", e)}') from e
        return json.loads(raw) if raw else None

    def board_id(self, project: str) -> int:
        r = self.do("GET", "/rest/agile/1.0/board?projectKeyOrId=" + _q(project))
        for b in r.get("values") or []:
            if b.get("type") == "scrum":
                return int(b["id"])
        raise JiraError(
            f"no scrum board found for project {project} (set board_id in config)"
        )

    def columns(self, board: int) -> list[Column]:
        r = self.do("GET", f"/rest/agile/1.0/board/{board}/configuration")
        return [
            Column(c.get("name") or "", [s["id"] for s in c["statuses"]])
            for c in (r.get("columnConfig") or {}).get("columns") or []
            if c.get("statuses")  # skip e.g. an unmapped Backlog column
        ]

    def active_sprint(self, board: int) -> tuple[int, str]:
        r = self.do("GET", f"/rest/agile/1.0/board/{board}/sprint?state=active")
        values = r.get("values") or []
        if not values:
            raise JiraError(f"board {board} has no active sprint")
        return int(values[0]["id"]), values[0].get("name") or ""

    def sprint_issues(self, sprint: int) -> list[Issue]:
        all_issues: list[Issue] = []
        while True:
            path = (
                f"/rest/agile/1.0/sprint/{sprint}/issue"
                f"?fields=summary,status,assignee,issuetype&maxResults=100&startAt={len(all_issues)}"
            )
            r = self.do("GET", path)
            issues = r.get("issues") or []
            all_issues += [_issue(i) for i in issues]
            if not issues or len(all_issues) >= (r.get("total") or 0):
                return all_issues

    def issue(self, key: str) -> Issue:
        """Uses API v2 so description is plain text, not ADF."""
        fields = "summary,description,status,assignee,reporter,issuetype,priority"
        return _issue(self.do("GET", f"/rest/api/2/issue/{_q(key)}?fields={fields}"))

    def transitions(self, key: str) -> list[Transition]:
        r = self.do("GET", f"/rest/api/2/issue/{_q(key)}/transitions")
        return [
            Transition(
                t.get("id") or "",
                (t.get("to") or {}).get("id") or "",
                t.get("name") or "",
                (t.get("to") or {}).get("name") or "",
            )
            for t in r.get("transitions") or []
        ]

    def transition(self, key: str, id: str) -> None:
        body = {"transition": {"id": id}}
        self.do("POST", f"/rest/api/2/issue/{_q(key)}/transitions", body)

    def prs(self, issue_id: str) -> list[PR]:
        """Uses the undocumented dev-status API (what Jira's own Development panel calls)."""
        s = self.do(
            "GET", "/rest/dev-status/latest/issue/summary?issueId=" + _q(issue_id)
        )
        by_type = ((s.get("summary") or {}).get("pullrequest") or {}).get(
            "byInstanceType"
        )
        prs: list[PR] = []
        for app in by_type or {}:
            path = (
                f"/rest/dev-status/latest/issue/detail?issueId={_q(issue_id)}"
                f"&applicationType={_q(app)}&dataType=pullrequest"
            )
            d = self.do("GET", path)
            for x in d.get("detail") or []:
                prs += [_pr(p) for p in x.get("pullRequests") or []]
        return prs
