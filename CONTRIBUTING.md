# Contributing

This repository is about one thing: **proving a used CMP 170HX is good, in a way
the buyer can re-run.** New instruments, better triage, failure modes we have not
hit — all welcome.

## What a good change looks like

This is a measurement tool, so the bar is evidence, not opinion.

- **Say what you measured.** A threshold change needs the numbers it came from
  and the card count behind them. "Seems better" is not reviewable.
- **Never turn an unknown into a pass.** Every guard in here exists because some
  naive version produced output indistinguishable from a real pass — a partial
  heap, a mis-attributed card, a run that died early. If a change could let one
  of those read as `PASS`, it will be rejected however much cleaner it is.
- **Keep the dashboard stdlib-only and read-only.** It runs unattended on a
  bench with no pip and no network, and it must never be able to disturb a run.
- **Comment the trap, not the syntax.** The comments in here record what a
  wrong answer looked like. That is the part worth maintaining.

## Checks

```console
$ shellcheck bin/*.sh install.sh bench-setup/*.sh
$ python3 -m py_compile bin/*.py
$ bash -n bin/*.sh install.sh bench-setup/*.sh
```

CI runs the first two on every push.

## Cards other than the 170HX

The structure generalises; the thresholds do not. If you adapt this to another
accelerator, please fork rather than adding a card-detection branch here — a
screen that silently applies 170HX bands to a different die is exactly the kind
of confidently-wrong output this suite is built to avoid.
