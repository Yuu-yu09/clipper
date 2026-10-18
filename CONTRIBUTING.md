# Git workflow

This project is under version control for every change. `data/`, `fixtures/`
and all rendered media are ignored — only source and docs are tracked.

## Everyday

```powershell
git status              # what changed
git add -A
git commit -m "short description of what and why"
git push origin main
```

## House rules

1. **One logical change per commit.** If the message needs the word "and"
   twice, it is two commits.
2. **The message says why, not what.** The diff already says what changed.
3. **Never commit generated media.** Clips, music beds and sampled frames are
   in `data/` and are ignored on purpose — they are large, reproducible, and
   contain nothing a reviewer needs.
4. **No secrets.** `.env` is ignored; `.env.example` holds the keys and their
   descriptions only.

## Before committing a change that touches the pipeline

```powershell
pip install pyflakes
python -m pyflakes momentclip run.py tools   # must be clean
python tools\selftest.py                     # 5/5 planted events
python tools\test_transcript.py              # ASR + captions
python tools\test_prompt.py                  # prompt compiler
python tools\test_render_features.py         # zoom, music, SFX
python tools\e2e_test.py                     # full pipeline
```

`pyflakes` is not optional ceremony. It caught an `UnboundLocalError` in
`server.create_job` that would have crashed **every** upload, while the
end-to-end test surfaced that class of bug one slow failure at a time.

Two `import ...  # noqa: F401` probes are expected and intentional — they test
whether `faster_whisper` and `graphify` are importable.

## Branching

```powershell
git switch -c feature/reactive-zoom
# ...work...
python -m pyflakes momentclip && python tools\e2e_test.py
git commit -am "..."
git push -u origin feature/reactive-zoom
```

Main stays green. Open a PR for anything non-trivial.

## Recovering

```powershell
git log --oneline --graph --all   # see what happened
git diff                          # uncommitted changes
git diff --staged                 # staged changes
git restore <file>                # throw away a change to one file
git revert <sha>                  # undo a commit already pushed
git reset --hard <sha>            # local only -- rewrites history
```

`reset --hard` discards uncommitted work. Read what you are about to throw
away first.

## Release

```powershell
git tag -a v0.2.0 -m "reactive zoom, adaptive music, free-AI providers"
git push origin v0.2.0
```

Tag from a commit whose full suite has passed on a clean checkout.