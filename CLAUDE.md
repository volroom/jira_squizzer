# jira-squizzer

A bubbletea TUI for a Jira Cloud sprint board. Everything is in package `main`: `jira.go` is the REST client, `main.go` is the model, keys and view, and `main_test.go` has the tests.

## Before committing

`gofmt -l .`, `go vet ./...` and `go test ./...` must all come back clean.

## Conventions

- Keep the code minimal: no new dependencies, no new files and no abstractions unless they're needed. Every non-trivial piece of logic gets one small test in `main_test.go`. Tests drive the model through `key(k)` and check its state; no Jira or terminal is needed.
- Keybindings live in three places that must agree: the switch in `key()`, the `boardHelp`/`detailHelp` footer strings, and the Keys tables in `README.md`.
- Colours come from the Gruvbox `gb*` colour vars in `main.go`. Reuse those rather than adding new hex values.

## Gotchas

- When a filter is active, `m.b.lanes` holds the filtered view and `m.all` holds the full lanes. Code that changes lanes has to update `m.all` and then re-run `m.filter(m.query)`. Everything else reads `m.b.lanes` as before.
- Linked PRs, and their `reviewers`/approval state, come from Jira's undocumented dev-status API (`/rest/dev-status/latest/issue/detail`). The response shape isn't guaranteed and hasn't been checked against every PR provider.
- `config.json` holds an API token and is gitignored. Never commit it or print it. The built `./jira-squizzer` binary is gitignored as well.
