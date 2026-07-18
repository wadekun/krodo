;; ---------------------------------------------------------------------------
;; python-tags.scm — tree-sitter tag query.
;; ---------------------------------------------------------------------------
;; Source:      Aider <aider/queries/tree-sitter-language-pack/python-tags.scm>
;; Upstream:    https://github.com/xberg-io/tree-sitter-language-pack (MIT)
;; Aider:       https://github.com/Aider-AI/aider (Apache-2.0)
;; Aider commit 5dc9490bb35f9729ef2c95d00a19ccd30c26339c
;; License:     MIT (upstream grammar) — compatible with krodo Apache-2.0.
;; Local modifications: the module-level constant pattern was relaxed from
;;   (module (expression_statement (assignment ...)))
;; to
;;   (module (assignment ...))
;; because the tree-sitter-language-pack grammar exposes module-level
;; assignments as direct children of `module` (no `expression_statement`
;; wrapper), so the verbatim upstream pattern matched nothing. Scope and
;; capture names are unchanged.
;;
;; Capture scheme (Aider convention):
;;   @name.definition.<kind>  — the identifier node (name + line)
;;   @name.reference.<kind>   — a reference identifier (call / import site)
;; krodo reads only the @name.* captures; @definition.* / @reference.* /
;; @doc are emitted by upstream but unused here. The #strip! / #set-adjacent!
;; / #select-adjacent! directives are inert under the standard tree-sitter
;; runtime (treated as unknown predicates) and do not filter results.
;; ---------------------------------------------------------------------------

(module (assignment left: (identifier) @name.definition.constant) @definition.constant)

(class_definition
  name: (identifier) @name.definition.class) @definition.class

(function_definition
  name: (identifier) @name.definition.function) @definition.function

(call
  function: [
      (identifier) @name.reference.call
      (attribute
        attribute: (identifier) @name.reference.call)
  ]) @reference.call
