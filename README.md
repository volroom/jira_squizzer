# jira-squizzer

A small terminal UI for your Jira Cloud project's active sprint board, with vim keybindings and a gruvbox colour scheme.

## Install

```sh
go install .        # or: go build
```

## Config

`~/.config/jira-squizzer/config.json`:

```json
{
  "base_url": "https://yourco.atlassian.net",
  "username": "you@yourco.com",
  "project": "ABC",
  "api_key": "<API token from id.atlassian.com/manage-profile/security/api-tokens>",
  "board_id": 0
}
```

`board_id` is optional. If it's left out or set to 0, the project's first scrum board is used.
`JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_PROJECT` and `JIRA_API_KEY` override the file, so you can keep the token out of it.

## Keys

| Board | |
|---|---|
| `h` / `l` | previous / next lane (keeps the cursor at the same height) |
| `j` / `k` | next / previous ticket |
| `gg` / `G` | top / bottom of lane |
| `Enter` / `o` | open ticket details and linked PRs |
| `m` | move mode: carry the ticket with `h`/`l` (lanes), `j`/`k` (up/down), `gg`/`G` (top/bottom), then `Enter` to drop or `Esc` to cancel |
| `H` / `L` | move ticket to previous / next lane |
| `/` | filter tickets by number or title (`Enter` keeps it, `Esc` clears) |
| `r` | reload |
| `q` | quit |

| Ticket | |
|---|---|
| `j` / `k`, `ctrl+d` / `ctrl+u` | scroll |
| `gg` / `G` | top / bottom |
| `q` / `Esc` / `h` | back to board |

## Notes

- Nothing is sent to Jira until you drop a ticket. A drop then runs a transition if the lane changed and a rank call to put it next to its new neighbour. With a filter on, that's the nearest ticket you can see.
- Moving a ticket runs the first workflow transition that leads into the target lane. If none exists, or the transition needs fields filled in, the move fails and Jira's error is shown.
- Linked PRs come from Jira's undocumented dev-status API, the one its Development panel uses.
