# AGENTS instructions

Conventions for `alma-certify`, the certification suite. The web application in `lumina` has its own
`AGENTS.md`; where both say something, they say the same thing.

## Hard constraints

- **Standard library only.** No third-party dependencies, ever. The suite is the first thing that
  runs on a bare AlmaLinux install, from an RPM, sometimes in a kickstart `%post`, on a machine with
  no extra repositories enabled. Anything it needs, the maintainers package.
- **Python 3.9 is the floor**, which is what AlmaLinux 8 offers through the `python39` module.
- **Nothing is downloaded at run time.** Data files ship in the package.
- **No repository is added and nothing is installed without consent**, and nothing may block waiting
  for input where there is no terminal: CI, cron, and kickstart `%post` all have to complete.
- **The collector makes no decisions.** It records what it measured; what that means for
  certification is `lumina`'s to decide, so it can be revised without shipping a new release to
  every certifying partner.

## Writing style

These rules apply to everything with words in it: user-facing strings, comments, docstrings,
commit messages, documentation, and templates.

- **Serial comma, always.** Write "validate, benchmark, and submit", not "validate, benchmark and
  submit". Three or more items take a comma before the final `and` or `or`. Two items do not: "the
  driver and the toolkit" is correct as it stands. This is a house rule with no exceptions, so a
  reviewer never has to decide.
- **American English.** "behavior", not "behaviour". "canceled", not "cancelled".
- **No em-dashes.** Use a comma, a colon, or a full stop. A hyphen surrounded by spaces is fine
  where a dash is genuinely wanted.
- Follow the AlmaLinux brand book for product names and capitalization.

## Tests

- Every behavior gets a test, and every test gets checked by breaking the code it covers. A test
  that passes against the mutation it was written for is not a test.
- Docstrings say *why* the behavior is what it is, especially where it was once something else, but keep comments as minimal as possible while still providing clarity.  Fewer words are better.
