# Issue tracker: GitHub

Issues and specs live in Charlie-McDaddy/shopwatch on GitHub.
Use the gh CLI from this repo.

- Create: gh issue create --title "..." --body-file <file>
- Read: gh issue view <number> --json number,title,body,labels,comments
- List: gh issue list --state open
- Label: gh issue edit <number> --add-label "<label>"
- Remove label: gh issue edit <number> --remove-label "<label>"
- Comment: gh issue comment <number> --body-file <file>
- Close: gh issue close <number>

When a skill says to publish to the issue tracker, create a GitHub issue.
When it says to fetch a ticket, read the corresponding GitHub issue.

PRs as a request surface: no.
