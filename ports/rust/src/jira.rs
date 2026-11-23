use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer};
use std::collections::HashMap;
use std::time::Duration;

pub type Res<T> = Result<T, String>;

#[derive(Clone)]
pub struct Client {
    base: String,
    auth: String,
    agent: ureq::Agent,
}

impl Client {
    pub fn new(base_url: &str, user: &str, token: &str) -> Client {
        let agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(30)))
            .http_status_as_error(false)
            .build()
            .into();
        let auth = "Basic ".to_string() + &base64(format!("{user}:{token}").as_bytes());
        Client {
            base: base_url.trim_end_matches('/').to_string(),
            auth,
            agent,
        }
    }

    fn send(&self, method: &str, path: &str, body: Option<String>) -> Res<String> {
        let url = self.base.clone() + path;
        let err = |e: ureq::Error| e.to_string();
        let resp = if let Some(b) = body {
            let req = self
                .agent
                .post(&url)
                .header("Authorization", &self.auth)
                .header("Accept", "application/json");
            req.header("Content-Type", "application/json")
                .send(b)
                .map_err(err)?
        } else {
            self.agent
                .get(&url)
                .header("Authorization", &self.auth)
                .header("Accept", "application/json")
                .call()
                .map_err(err)?
        };
        let status = resp.status();
        let text = resp.into_body().read_to_string().map_err(err)?;
        if status.as_u16() >= 300 {
            let mut end = text.len().min(500);
            while !text.is_char_boundary(end) {
                end -= 1;
            }
            return Err(format!("{method} {path}: {status} {}", text[..end].trim()));
        }
        Ok(text)
    }

    fn get<T: DeserializeOwned>(&self, path: &str) -> Res<T> {
        serde_json::from_str(&self.send("GET", path, None)?).map_err(|e| e.to_string())
    }

    pub fn board_id(&self, project: &str) -> Res<i64> {
        #[derive(Deserialize)]
        struct B {
            id: i64,
            #[serde(rename = "type")]
            kind: String,
        }
        #[derive(Deserialize)]
        struct R {
            values: Vec<B>,
        }
        let r: R = self.get(&format!(
            "/rest/agile/1.0/board?projectKeyOrId={}",
            esc(project)
        ))?;
        r.values
            .into_iter()
            .find(|b| b.kind == "scrum")
            .map(|b| b.id)
            .ok_or_else(|| {
                format!("no scrum board found for project {project} (set board_id in config)")
            })
    }

    pub fn columns(&self, board: i64) -> Res<Vec<Column>> {
        #[derive(Deserialize)]
        struct S {
            id: String,
        }
        #[derive(Deserialize)]
        struct C {
            name: String,
            statuses: Vec<S>,
        }
        #[derive(Deserialize)]
        struct Cfg {
            columns: Vec<C>,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct R {
            column_config: Cfg,
        }
        let r: R = self.get(&format!("/rest/agile/1.0/board/{board}/configuration"))?;
        Ok(r.column_config
            .columns
            .into_iter()
            .filter(|c| !c.statuses.is_empty()) // e.g. an unmapped Backlog column
            .map(|c| Column {
                name: c.name,
                statuses: c.statuses.into_iter().map(|s| s.id).collect(),
            })
            .collect())
    }

    pub fn active_sprint(&self, board: i64) -> Res<(i64, String)> {
        #[derive(Deserialize)]
        struct S {
            id: i64,
            name: String,
        }
        #[derive(Deserialize)]
        struct R {
            values: Vec<S>,
        }
        let r: R = self.get(&format!(
            "/rest/agile/1.0/board/{board}/sprint?state=active"
        ))?;
        let s = r
            .values
            .into_iter()
            .next()
            .ok_or(format!("board {board} has no active sprint"))?;
        Ok((s.id, s.name))
    }

    pub fn sprint_issues(&self, sprint: i64) -> Res<Vec<Issue>> {
        #[derive(Deserialize)]
        struct R {
            issues: Vec<Issue>,
            total: usize,
        }
        let mut all = Vec::new();
        loop {
            let path = format!(
                "/rest/agile/1.0/sprint/{sprint}/issue?fields=summary,status,assignee,issuetype&maxResults=100&startAt={}",
                all.len()
            );
            let r: R = self.get(&path)?;
            let done = r.issues.is_empty();
            all.extend(r.issues);
            if done || all.len() >= r.total {
                return Ok(all);
            }
        }
    }

    /// Uses API v2 so description is plain text, not ADF.
    pub fn issue(&self, key: &str) -> Res<Issue> {
        self.get(&format!("/rest/api/2/issue/{}?fields=summary,description,status,assignee,reporter,issuetype,priority", esc(key)))
    }

    pub fn transitions(&self, key: &str) -> Res<Vec<Transition>> {
        #[derive(Deserialize)]
        struct R {
            transitions: Vec<Transition>,
        }
        Ok(self
            .get::<R>(&format!("/rest/api/2/issue/{}/transitions", esc(key)))?
            .transitions)
    }

    pub fn transition(&self, key: &str, id: &str) -> Res<()> {
        let body = serde_json::json!({"transition": {"id": id}}).to_string();
        self.send(
            "POST",
            &format!("/rest/api/2/issue/{}/transitions", esc(key)),
            Some(body),
        )
        .map(|_| ())
    }

    /// Uses the undocumented dev-status API (what Jira's own Development panel calls).
    /// Like the Go version, PRs fetched before an error are returned alongside it.
    pub fn prs(&self, issue_id: &str) -> (Vec<PR>, Option<String>) {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct Pr {
            by_instance_type: HashMap<String, serde_json::Value>,
        }
        #[derive(Deserialize)]
        struct Sum {
            pullrequest: Pr,
        }
        #[derive(Deserialize)]
        struct S {
            summary: Sum,
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct D {
            pull_requests: Vec<PR>,
        }
        #[derive(Deserialize)]
        struct R {
            detail: Vec<D>,
        }
        let s: S = match self.get(&format!(
            "/rest/dev-status/latest/issue/summary?issueId={}",
            esc(issue_id)
        )) {
            Ok(s) => s,
            Err(e) => return (Vec::new(), Some(e)),
        };
        let mut prs = Vec::new();
        for app in s.summary.pullrequest.by_instance_type.keys() {
            let path = format!(
                "/rest/dev-status/latest/issue/detail?issueId={}&applicationType={}&dataType=pullrequest",
                esc(issue_id),
                esc(app)
            );
            match self.get::<R>(&path) {
                Ok(r) => prs.extend(r.detail.into_iter().flat_map(|d| d.pull_requests)),
                Err(e) => return (prs, Some(e)),
            }
        }
        (prs, None)
    }
}

/// Named covers Jira's {name} and {displayName} objects (user, priority, issuetype).
#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Named {
    #[serde(deserialize_with = "nullable")]
    pub name: String,
    #[serde(deserialize_with = "nullable")]
    pub display_name: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Status {
    pub id: String,
    pub name: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Fields {
    #[serde(deserialize_with = "nullable")]
    pub summary: String,
    #[serde(deserialize_with = "nullable")]
    pub description: String,
    pub status: Status,
    pub assignee: Option<Named>,
    pub reporter: Option<Named>,
    pub issuetype: Option<Named>,
    pub priority: Option<Named>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Issue {
    pub id: String,
    pub key: String,
    pub fields: Fields,
}

#[derive(Clone, Debug, Default)]
pub struct Column {
    pub name: String,
    pub statuses: Vec<String>, // status IDs
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Transition {
    pub id: String,
    pub name: String,
    pub to: Status,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct PR {
    #[serde(deserialize_with = "nullable")]
    pub id: String,
    #[serde(deserialize_with = "nullable")]
    pub name: String,
    #[serde(deserialize_with = "nullable")]
    pub url: String,
    #[serde(deserialize_with = "nullable")]
    pub status: String,
    #[serde(deserialize_with = "nullable")]
    pub repository_name: String,
    #[serde(deserialize_with = "nullable")]
    pub reviewers: Vec<Reviewer>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Reviewer {
    #[serde(deserialize_with = "nullable")]
    pub name: String,
    pub approved: bool,
}

/// nullable decodes JSON null as the default value, as Go's encoding/json does.
fn nullable<'de, D: Deserializer<'de>, T: Default + Deserialize<'de>>(d: D) -> Result<T, D::Error> {
    Ok(Option::deserialize(d)?.unwrap_or_default())
}

/// esc percent-encodes everything but RFC 3986 unreserved characters.
fn esc(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn base64(input: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in input.chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            out.push(if i <= c.len() {
                T[(n >> (18 - 6 * i) & 63) as usize] as char
            } else {
                '='
            });
        }
    }
    out
}
