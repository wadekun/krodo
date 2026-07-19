# Third-Party Notices

This file documents third-party software whose source is **vendored** (copied
into this repository), as required by the upstream licenses and Apache-2.0
§4(d). Runtime dependencies declared in `pyproject.toml` are listed by the
installed package metadata and are not repeated here.

## tree-sitter tag queries (`src/krodo/indexer/queries/*.scm`)

The tree-sitter query files used to extract symbol definitions and references
are adapted from the **Aider** project.

| File in krodo | Upstream source | License |
|---|---|---|
| `python-tags.scm` | `aider/queries/tree-sitter-language-pack/python-tags.scm` | MIT |
| `javascript-tags.scm` | `aider/queries/tree-sitter-language-pack/javascript-tags.scm` | MIT |
| `typescript-tags.scm` | adapted (see below) | MIT |
| `go-tags.scm` | `aider/queries/tree-sitter-language-pack/go-tags.scm` | MIT |

- **Aider repository**: https://github.com/Aider-AI/aider (Apache-2.0)
- **Aider commit pinned at time of vendoring**:
  `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`
- **Upstream grammar source**: the underlying tree-sitter grammars are
  distributed by
  [xberg-io/tree-sitter-language-pack](https://github.com/xberg-io/tree-sitter-language-pack),
  which documents each grammar's license. Aider's `queries/README.md` (same
  commit) attributes them there. The Python/JavaScript/TypeScript/Go grammars
  are MIT-licensed and compatible with krodo's Apache-2.0.

### Local modifications

Each `.scm` file carries a header comment recording its provenance and any
local modifications. In summary:

- `python-tags.scm` — the module-level constant pattern was relaxed from
  `(module (expression_statement (assignment ...)))` to
  `(module (assignment ...))`, because the tree-sitter-language-pack grammar
  exposes module-level assignments as direct children of `module` (no
  `expression_statement` wrapper), so the verbatim upstream pattern matched
  nothing. Scope and capture names are unchanged.
- `javascript-tags.scm`, `go-tags.scm` — verbatim from upstream. The
  `#strip!`, `#set-adjacent!`, and `#select-adjacent!` directives emitted by
  upstream are inert under the standard tree-sitter runtime (treated as
  unknown predicates) and do not filter results.
- `typescript-tags.scm` — Aider ships no TypeScript query; this file reuses
  the `javascript-tags.scm` patterns verbatim (the TypeScript grammar is a
  superset of the JavaScript grammar for function/class/method/call nodes)
  and appends TypeScript-only patterns for `interface_declaration`,
  `type_alias_declaration`, and `enum_declaration`.

The capture-name scheme follows the Aider convention
(`@name.definition.<kind>` / `@name.reference.<kind>`); krodo reads only the
`@name.*` captures.

### Copyright

```
Copyright (c) the Aider contributors
Copyright (c) the respective tree-sitter grammar authors
```

Licensed under the MIT License (upstream grammars). A copy of the MIT License
text is available at https://opensource.org/licenses/MIT.
