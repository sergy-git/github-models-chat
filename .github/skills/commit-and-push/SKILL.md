---
name: commit-and-push
description: Stage, commit, and push changes to the current git branch using Conventional Commits message format. Use when the user asks to commit, commit and push, or save their changes to git.
---

# Commit and Push

## Quick start

1. Run `git status` and `git diff` (staged + unstaged) to see what changed.
2. Stage the relevant files with `git add`.
3. Commit using a Conventional Commits message.
4. Push to the current branch's upstream with `git push`.

## Workflow

- [ ] Check `git status` to see untracked/modified files.
- [ ] Review `git diff` / `git diff --staged` to understand the actual changes — don't guess at the commit message.
- [ ] Stage only files relevant to the logical change (avoid `git add -A` if unrelated changes are present).
- [ ] Write a commit message following the format below.
- [ ] Run `git commit -m "<message>"`.
- [ ] Run `git push`. If the branch has no upstream yet, use `git push -u origin <branch>`.
- [ ] Report the commit hash and push result briefly.

No confirmation prompt is needed before pushing — proceed automatically once the commit succeeds, unless the push fails or requires force (in that case, stop and ask the user).

## Commit Message Format (Conventional Commits)

```
<type>(<scope>): <short summary>

[optional body]
```

- **type**: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `style`, `perf`, `build`, `ci`
- **scope**: optional, short name of the affected area (e.g. `alpaca_api`, `client`, `docs`)
- **short summary**: imperative mood, lowercase, no trailing period, under ~72 chars
- **body**: optional, explain *why* when the change isn't self-evident from the diff

Example:
```
fix(client): handle rate-limit errors on order submission
```

## Notes

- If there are multiple unrelated changes, split them into separate commits rather than one large commit.
- Never rewrite or force-push existing history (`git commit --amend`, `git push --force`) without explicit user confirmation first — these are hard to reverse.
- If `git push` fails (e.g. diverged branch), stop and surface the error instead of force-pushing.
