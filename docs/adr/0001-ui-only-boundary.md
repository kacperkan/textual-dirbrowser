# 1. UI-only package boundary with injected backends and copied CSS

Date: 2026-07-07

## Status

Accepted

## Context

The browser began life inside a larger CLI tool, where one Textual UI fronts
three storage flavors: a remote pod filesystem, S3, and local disk. It was
split out so other projects can import it.

The browser already received directory listings through caller-supplied
callbacks (`list_fn`, `parent_fn`); the storage implementations lived in the
consumers and pull heavy dependencies (kubectl subprocess, AWS tooling).
Its stylesheet was also shared by unrelated screens of the original tool.

## Decision

This package is the UI only:

- **Backends stay host-side.** The package ships no pod/S3/local listing
  code. Hosts inject backends via the callback API. Dependencies remain
  `textual` + `rich` only.
- **CSS is copied, not shared.** The package embeds its own copy of the
  stylesheet as the default look; the original tool keeps its own copy for
  its other screens. Hosts restyle by subclassing the public App classes and
  overriding `DEFAULT_CSS`.
- **Clean public API.** The App classes are public
  (`BrowserApp`, `StartBrowserApp`, `MultiSelectApp`) with an explicit
  `__all__`, so hosts and their tests never reach into private names.

## Consequences

- New consumers must write a `list_fn`/`parent_fn` pair even for plain local
  browsing — the package works out of the box only as a widget, not as a
  ready-made file picker.
- The stylesheet copies may drift from the original tool's; that is
  acceptable, and subclassing covers hosts that need a specific look.
- Host test suites mock a public, stable API across a package boundary
  instead of private classes inside the same repo.

## Alternatives considered

- **Ship backends in the package** (with optional extras for AWS/kubectl):
  rejected — couples a generic UI library to infrastructure tooling and its
  credential/config assumptions.
- **Ship only the stdlib local backend**: rejected as a half-measure; the
  callback API is small and the asymmetry (one bundled backend, two
  injected) would be more confusing than none.
- **Host imports CSS from the package**: rejected — the host's unrelated
  screens would depend on a file-browser library for styling, inverting the
  dependency direction.
