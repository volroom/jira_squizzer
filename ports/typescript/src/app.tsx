import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Box, render, Text, useApp, useInput, type Key } from "ink";
import { useEffect, useReducer } from "react";
import { Client, type Config } from "./jira.js";
import { Model, type Cmd } from "./model.js";

// configDir matches Go's os.UserConfigDir.
function configDir(): string {
  if (process.platform === "darwin") return join(homedir(), "Library", "Application Support");
  if (process.platform === "win32") return process.env.APPDATA ?? "";
  return process.env.XDG_CONFIG_HOME || join(homedir(), ".config");
}

function loadConfig(): Config {
  const path = join(configDir(), "jira-squizzer", "config.json");
  let c: Config = { base_url: "", username: "", project: "", api_key: "" };
  let raw: string | undefined;
  try {
    raw = readFileSync(path, "utf8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  if (raw !== undefined) {
    try {
      c = { ...c, ...JSON.parse(raw) };
    } catch (e) {
      throw new Error(`${path}: ${(e as Error).message}`);
    }
  }
  const env = { JIRA_BASE_URL: "base_url", JIRA_USERNAME: "username", JIRA_PROJECT: "project", JIRA_API_KEY: "api_key" } as const;
  for (const [k, field] of Object.entries(env)) if (process.env[k]) c[field] = process.env[k];
  if (!c.base_url || !c.username || !c.project || !c.api_key) {
    throw new Error(
      `missing config: set base_url, username, project, api_key in ${path}\nor JIRA_BASE_URL, JIRA_USERNAME, JIRA_PROJECT, JIRA_API_KEY`,
    );
  }
  return c;
}

// keyName turns Ink's input into bubbletea's key strings ("ctrl+d", "esc", "enter", "left", "G", ...).
function keyName(input: string, key: Key): string {
  if (key.ctrl && input) return "ctrl+" + input;
  if (key.escape) return "esc";
  if (key.return) return "enter";
  if (key.backspace) return "backspace";
  if (key.delete) return "delete";
  if (key.upArrow) return "up";
  if (key.downArrow) return "down";
  if (key.leftArrow) return "left";
  if (key.rightArrow) return "right";
  return input;
}

function App({ m }: { m: Model }) {
  const { exit } = useApp();
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const run = (cmd: Cmd) => {
    if (cmd === "quit") return exit();
    cmd?.().then((msg) => {
      run(m.update(msg));
      redraw();
    });
  };
  useEffect(() => {
    const resize = () => {
      m.w = process.stdout.columns;
      m.h = process.stdout.rows;
      redraw();
    };
    resize();
    process.stdout.on("resize", resize);
    run(m.init());
    return () => void process.stdout.off("resize", resize);
  }, []);
  useInput((input, key) => {
    run(m.key(keyName(input, key)));
    redraw();
  });
  return (
    <Box flexDirection="column">
      {m.view().split("\n").map((l, i) => (
        <Text key={i} wrap="truncate-end">
          {l || " "}
        </Text>
      ))}
    </Box>
  );
}

let cfg: Config;
try {
  cfg = loadConfig();
} catch (e) {
  console.error((e as Error).message);
  process.exit(1);
}
const m = Object.assign(new Model(), { c: new Client(cfg), cfg, busy: true, msg: "loading board…" });
await render(<App m={m} />, { exitOnCtrlC: false, alternateScreen: true }).waitUntilExit();
