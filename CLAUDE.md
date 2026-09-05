# Digital Office

**Read `SESSION_HANDOFF.md` first.** It carries the state, settled decisions, and open
items from the previous session — reading it avoids re-deriving work already done.

## Running it

```bash
./officectl start          # reads .env; ./officectl for the full command list
```

`OFFICE_BACKEND=mock` runs the whole office free, with no network and no spend. `.env`,
`data/` and `workspace/` are gitignored and hold live credentials and state — keep them
out of commits.
