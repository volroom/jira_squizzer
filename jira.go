package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	base, user, token string
	hc                *http.Client
}

func NewClient(c Config) *Client {
	return &Client{strings.TrimRight(c.BaseURL, "/"), c.Username, c.APIKey, &http.Client{Timeout: 30 * time.Second}}
}

func (c *Client) do(method, path string, body, out any) error {
	var r io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		r = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.base+path, r)
	if err != nil {
		return err
	}
	req.SetBasicAuth(c.user, c.token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 500))
		return fmt.Errorf("%s %s: %s %s", method, path, resp.Status, bytes.TrimSpace(b))
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// Named covers Jira's {name} and {displayName} objects (user, priority, issuetype).
type Named struct {
	Name        string
	DisplayName string
}

type Issue struct {
	ID     string
	Key    string
	Fields struct {
		Summary     string
		Description string
		Status      struct{ ID, Name string }
		Assignee    *Named
		Reporter    *Named
		IssueType   *Named
		Priority    *Named
	}
}

type Column struct {
	Name     string
	Statuses []string // status IDs
}

type Transition struct {
	ID   string
	Name string
	To   struct{ ID, Name string }
}

type PR struct {
	ID, Name, URL, Status, RepositoryName string
	Reviewers                             []Reviewer
}

type Reviewer struct {
	Name     string
	Approved bool
}

func (c *Client) BoardID(project string) (int, error) {
	var r struct {
		Values []struct {
			ID   int
			Type string
		}
	}
	if err := c.do("GET", "/rest/agile/1.0/board?projectKeyOrId="+url.QueryEscape(project), nil, &r); err != nil {
		return 0, err
	}
	for _, b := range r.Values {
		if b.Type == "scrum" {
			return b.ID, nil
		}
	}
	return 0, fmt.Errorf("no scrum board found for project %s (set board_id in config)", project)
}

func (c *Client) Columns(board int) ([]Column, error) {
	var r struct {
		ColumnConfig struct {
			Columns []struct {
				Name     string
				Statuses []struct{ ID string }
			}
		}
	}
	if err := c.do("GET", fmt.Sprintf("/rest/agile/1.0/board/%d/configuration", board), nil, &r); err != nil {
		return nil, err
	}
	var cols []Column
	for _, col := range r.ColumnConfig.Columns {
		if len(col.Statuses) == 0 {
			continue // e.g. an unmapped Backlog column
		}
		c := Column{Name: col.Name}
		for _, s := range col.Statuses {
			c.Statuses = append(c.Statuses, s.ID)
		}
		cols = append(cols, c)
	}
	return cols, nil
}

func (c *Client) ActiveSprint(board int) (int, string, error) {
	var r struct {
		Values []struct {
			ID   int
			Name string
		}
	}
	if err := c.do("GET", fmt.Sprintf("/rest/agile/1.0/board/%d/sprint?state=active", board), nil, &r); err != nil {
		return 0, "", err
	}
	if len(r.Values) == 0 {
		return 0, "", fmt.Errorf("board %d has no active sprint", board)
	}
	return r.Values[0].ID, r.Values[0].Name, nil
}

func (c *Client) SprintIssues(sprint int) ([]Issue, error) {
	var all []Issue
	for {
		var r struct {
			Issues []Issue
			Total  int
		}
		path := fmt.Sprintf("/rest/agile/1.0/sprint/%d/issue?fields=summary,status,assignee,issuetype&maxResults=100&startAt=%d", sprint, len(all))
		if err := c.do("GET", path, nil, &r); err != nil {
			return nil, err
		}
		all = append(all, r.Issues...)
		if len(r.Issues) == 0 || len(all) >= r.Total {
			return all, nil
		}
	}
}

// Issue uses API v2 so description is plain text, not ADF.
func (c *Client) Issue(key string) (Issue, error) {
	var i Issue
	err := c.do("GET", "/rest/api/2/issue/"+url.PathEscape(key)+"?fields=summary,description,status,assignee,reporter,issuetype,priority", nil, &i)
	return i, err
}

func (c *Client) Transitions(key string) ([]Transition, error) {
	var r struct{ Transitions []Transition }
	err := c.do("GET", "/rest/api/2/issue/"+url.PathEscape(key)+"/transitions", nil, &r)
	return r.Transitions, err
}

func (c *Client) Transition(key, id string) error {
	body := map[string]any{"transition": map[string]string{"id": id}}
	return c.do("POST", "/rest/api/2/issue/"+url.PathEscape(key)+"/transitions", body, nil)
}

// Rank puts key just before the issue before, or if that's empty, just after the issue after.
func (c *Client) Rank(key, before, after string) error {
	body := map[string]any{"issues": []string{key}}
	if before != "" {
		body["rankBeforeIssue"] = before
	} else {
		body["rankAfterIssue"] = after
	}
	// 204 with no body on success; 207 with per-issue errors on failure
	var r struct{ Entries []struct{ Errors []string } }
	if err := c.do("PUT", "/rest/agile/1.0/issue/rank", body, &r); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	for _, e := range r.Entries {
		if len(e.Errors) > 0 {
			return fmt.Errorf("rank %s: %s", key, strings.Join(e.Errors, "; "))
		}
	}
	return nil
}

// PRs uses the undocumented dev-status API (what Jira's own Development panel calls).
func (c *Client) PRs(issueID string) ([]PR, error) {
	var s struct {
		Summary struct {
			Pullrequest struct{ ByInstanceType map[string]json.RawMessage }
		}
	}
	if err := c.do("GET", "/rest/dev-status/latest/issue/summary?issueId="+url.QueryEscape(issueID), nil, &s); err != nil {
		return nil, err
	}
	var prs []PR
	for app := range s.Summary.Pullrequest.ByInstanceType {
		var d struct{ Detail []struct{ PullRequests []PR } }
		path := fmt.Sprintf("/rest/dev-status/latest/issue/detail?issueId=%s&applicationType=%s&dataType=pullrequest",
			url.QueryEscape(issueID), url.QueryEscape(app))
		if err := c.do("GET", path, nil, &d); err != nil {
			return prs, err
		}
		for _, x := range d.Detail {
			prs = append(prs, x.PullRequests...)
		}
	}
	return prs, nil
}
