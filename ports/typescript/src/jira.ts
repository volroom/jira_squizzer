export interface Config {
  base_url: string;
  username: string;
  project: string;
  api_key: string;
  board_id?: number; // optional; defaults to the project's first scrum board
}

// Named covers Jira's {name} and {displayName} objects (user, priority, issuetype).
export interface Named {
  name?: string;
  displayName?: string;
}

export interface Issue {
  id: string;
  key: string;
  fields: {
    summary: string;
    description?: string | null;
    status: { id: string; name: string };
    assignee?: Named | null;
    reporter?: Named | null;
    issuetype?: Named | null;
    priority?: Named | null;
  };
}

export interface Column {
  name: string;
  statuses: string[]; // status IDs
}

export interface Transition {
  id: string;
  name: string;
  to: { id: string; name: string };
}

export interface PR {
  id: string;
  name: string;
  url: string;
  status: string;
  repositoryName: string;
  reviewers?: Reviewer[];
}

export interface Reviewer {
  name: string;
  approved: boolean;
}

const q = encodeURIComponent;

export class Client {
  private base: string;
  private auth: string;

  constructor(c: Config) {
    this.base = c.base_url.replace(/\/+$/, "");
    this.auth = "Basic " + Buffer.from(`${c.username}:${c.api_key}`).toString("base64");
  }

  private async do<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Authorization: this.auth, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(this.base + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    }).catch((e: Error) => {
      // fetch's own message is just "fetch failed"; the cause says why, like Go's url.Error.
      throw new Error(`${method} "${this.base + path}": ${(e.cause as Error | undefined)?.message ?? e.message}`);
    });
    if (resp.status >= 300) {
      const b = Buffer.from(await resp.arrayBuffer()).subarray(0, 500).toString().trim();
      throw new Error(`${method} ${path}: ${resp.status} ${resp.statusText} ${b}`);
    }
    const text = await resp.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  async boardID(project: string): Promise<number> {
    const r = await this.do<{ values?: { id: number; type: string }[] }>("GET", "/rest/agile/1.0/board?projectKeyOrId=" + q(project));
    const b = r.values?.find((b) => b.type === "scrum");
    if (!b) throw new Error(`no scrum board found for project ${project} (set board_id in config)`);
    return b.id;
  }

  async columns(board: number): Promise<Column[]> {
    const r = await this.do<{ columnConfig?: { columns?: { name: string; statuses?: { id: string }[] }[] } }>(
      "GET",
      `/rest/agile/1.0/board/${board}/configuration`,
    );
    return (r.columnConfig?.columns ?? [])
      .filter((col) => col.statuses?.length) // e.g. an unmapped Backlog column
      .map((col) => ({ name: col.name, statuses: col.statuses!.map((s) => s.id) }));
  }

  async activeSprint(board: number): Promise<[number, string]> {
    const r = await this.do<{ values?: { id: number; name: string }[] }>("GET", `/rest/agile/1.0/board/${board}/sprint?state=active`);
    if (!r.values?.length) throw new Error(`board ${board} has no active sprint`);
    return [r.values[0].id, r.values[0].name];
  }

  async sprintIssues(sprint: number): Promise<Issue[]> {
    const all: Issue[] = [];
    for (;;) {
      const path = `/rest/agile/1.0/sprint/${sprint}/issue?fields=summary,status,assignee,issuetype&maxResults=100&startAt=${all.length}`;
      const r = await this.do<{ issues?: Issue[]; total?: number }>("GET", path);
      const issues = r.issues ?? [];
      all.push(...issues);
      if (issues.length === 0 || all.length >= (r.total ?? 0)) return all;
    }
  }

  // issue uses API v2 so description is plain text, not ADF.
  issue(key: string): Promise<Issue> {
    return this.do("GET", "/rest/api/2/issue/" + q(key) + "?fields=summary,description,status,assignee,reporter,issuetype,priority");
  }

  async transitions(key: string): Promise<Transition[]> {
    const r = await this.do<{ transitions?: Transition[] }>("GET", "/rest/api/2/issue/" + q(key) + "/transitions");
    return r.transitions ?? [];
  }

  async transition(key: string, id: string): Promise<void> {
    await this.do("POST", "/rest/api/2/issue/" + q(key) + "/transitions", { transition: { id } });
  }

  // prs uses the undocumented dev-status API (what Jira's own Development panel calls).
  // Like the Go version, an error returns the PRs collected so far alongside it.
  async prs(issueID: string): Promise<[PR[], Error | null]> {
    const prs: PR[] = [];
    try {
      const s = await this.do<{ summary?: { pullrequest?: { byInstanceType?: Record<string, unknown> } } }>(
        "GET",
        "/rest/dev-status/latest/issue/summary?issueId=" + q(issueID),
      );
      for (const app of Object.keys(s.summary?.pullrequest?.byInstanceType ?? {})) {
        const path = `/rest/dev-status/latest/issue/detail?issueId=${q(issueID)}&applicationType=${q(app)}&dataType=pullrequest`;
        const d = await this.do<{ detail?: { pullRequests?: PR[] }[] }>("GET", path);
        for (const x of d.detail ?? []) prs.push(...(x.pullRequests ?? []));
      }
    } catch (e) {
      return [prs, e as Error];
    }
    return [prs, null];
  }
}
