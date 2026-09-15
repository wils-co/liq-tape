# PR numbering

GitHub numbers pull requests sequentially repo-wide. The project's PR numbers
are the build plan's scope sequence. PR1 has no GitHub PR — it landed as the
repository's first commit, before the remote existed, so there was nothing to
open a pull request against. Everything else has one.

## The mapping

| Project PR | Scope                          | GitHub PR |
|------------|--------------------------------|-----------|
| PR1        | Foundation sampler             | — (pre-repo commit) |
| PR2        | Server + OI×price quadrant     | #1        |
| PR3        | L2 depth + funding/premium     | #2        |
| PR4        | Structure levels + UI config   | #3        |
| PR3.5      | CI doctrine walls              | #4        |
| PR5        | Board daemon, README, docs     | #5        |
| PR6        | Volume profile + notable prints | #6        |
| PR7        | Walls, tape/CVD, layer chrome  | #7        |
| PR8        | Panel ⑧ time × mark, one-screen layout | #8        |
| PR9        | Real liq map (top 200 liquidationPx) | #9        |
| PR9.5      | Second liq account set (active by turnover) | #10       |
| —          | docs: architecture diagrams | #11       |
| PR9.6      | Retention: archive rows older than 10 days | #12       |

## The offset

For whole-number project PRs, **GitHub PR number = project PR number − 1**.
That held from PR2 through PR4. PR3.5 landed out of scope order and took
GitHub #4 — the number PR5 would otherwise have had. The offset does not
re-establish; from PR6 on, number GitHub PRs as they come and record them
here. This file is the only place the mapping lives — read it, don't
re-derive it.
