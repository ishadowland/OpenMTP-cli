# Contributing to openmtp-cli

Thanks for your interest. This project is in an **early scaffold** — there is no
code yet, only the wishlist in [`README.md`](README.md). Please read it first;
it describes the scope we are working toward and the open design decisions (most
notably the **implementation approach** and the **language/ecosystem**).

## Before you write any code

> **Open an issue first.** The README explicitly asks for it: scope and
> architecture are not settled, so any PR that jumps straight to code is almost
> guaranteed to need rework or rejection.

Use one of the [issue templates](https://github.com/ishadowland/openmtp-cli/issues/new/choose):

| If you want to …                                  | Template                              |
| ------------------------------------------------- | ------------------------------------- |
| Propose adding (or removing) a CLI command        | **Scope discussion**                  |
| Report broken behavior in a released binary       | **Bug report** *(not useful yet — no releases)* |
| Suggest a new command not on the README wishlist  | **Feature request**                   |

The **scope discussion** template is the one to use in 99% of cases right now.
It asks for the proposed CLI surface, the implementation approach (see the
table in the README), the language/toolchain pick, and any trade-offs you are
opting into.

## Development workflow (once code lands)

1. Fork the repo and create a topic branch off `main`.
2. Make your change. Keep commits small and focused; write commit messages in
   the imperative mood ("Add `list` subcommand", not "Added").
3. Run the project's formatter / linter / tests locally — these will be
   defined once a language is picked.
4. Push the branch and open a PR against `ishadowland/openmtp-cli:main`.
5. Fill in the PR template. Reference the issue your change closes
   (`Closes #NN`).
6. Wait for review. CI must pass; at least one approving review is required
   before merge.

## Commit messages

We do not enforce a formal convention yet, but please:

- Use the imperative mood ("Fix typo", not "Fixed typo").
- Keep the subject under ~72 characters.
- Add a body when the change is non-obvious. Wrap the body at ~72 columns.
- Reference issues by number (`#NN`) where relevant.

## Code style

Will be defined per-language once the toolchain is chosen. Until then, match
the existing files (Markdown, YAML) on aesthetics: 2-space YAML indent, ATX
headings, fenced code blocks with language hints.

## Reporting security issues

Please **do not** open a public issue for security problems. Email the
maintainer (see the commit history for current contact) and we will coordinate
a fix and disclosure timeline.

## License

By submitting a contribution, you agree it will be released under the project's
[MIT License](LICENSE).