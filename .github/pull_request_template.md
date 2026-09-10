## What and why

<!-- What changes, and what problem it solves. One or two paragraphs. -->

Ticket: <!-- tracker id if the work has one; contributions from outside the team can leave this blank -->

## Scope

<!-- What is in, and — just as useful to a reviewer — what you deliberately left out. -->

## How this was verified

<!-- The part a reviewer cannot reconstruct. Which gates you ran and what they printed, plus anything
     you checked by hand (a live stack, a browser, a migration round-trip). Say what you could NOT
     verify rather than leaving it implied. -->

- [ ] `make lint`
- [ ] `make test`
- [ ] Docs regenerated where the change touches them (`make be-openapidump` / `be-erddump` /
      `be-permissionsdump`) and `git status` is clean

## Calls worth questioning

<!-- Optional, and the most valuable section when it is not empty: decisions you made that a reviewer
     might reasonably make differently. -->
