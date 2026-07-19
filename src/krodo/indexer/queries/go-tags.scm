;; ---------------------------------------------------------------------------
;; go-tags.scm — tree-sitter tag query.
;; ---------------------------------------------------------------------------
;; Source:      Aider <aider/queries/tree-sitter-language-pack/go-tags.scm>
;; Upstream:    https://github.com/xberg-io/tree-sitter-language-pack (MIT)
;; Aider:       https://github.com/Aider-AI/aider (Apache-2.0)
;; Aider commit 5dc9490bb35f9729ef2c95d00a19ccd30c26339c
;; License:     MIT (upstream grammar) — compatible with krodo Apache-2.0.
;; Local modifications: none (verbatim from upstream).
;;
;; Capture scheme (Aider convention):
;;   @name.definition.<kind>  — the identifier node (name + line)
;;   @name.reference.<kind>   — a reference identifier (call / import site)
;; krodo reads only the @name.* captures; @definition.* / @reference.* /
;; @doc are emitted by upstream but unused here. The #strip! / #set-adjacent!
;; / #select-adjacent! directives are inert under the standard tree-sitter
;; runtime (treated as unknown predicates) and do not filter results.
;; ---------------------------------------------------------------------------

(
  (comment)* @doc
  .
  (function_declaration
    name: (identifier) @name.definition.function) @definition.function
  (#strip! @doc "^//\\s*")
  (#set-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (method_declaration
    name: (field_identifier) @name.definition.method) @definition.method
  (#strip! @doc "^//\\s*")
  (#set-adjacent! @doc @definition.method)
)

(call_expression
  function: [
    (identifier) @name.reference.call
    (parenthesized_expression (identifier) @name.reference.call)
    (selector_expression field: (field_identifier) @name.reference.call)
    (parenthesized_expression (selector_expression field: (field_identifier) @name.reference.call))
  ]) @reference.call

(type_spec
  name: (type_identifier) @name.definition.type) @definition.type

(type_identifier) @name.reference.type @reference.type

(package_clause "package" (package_identifier) @name.definition.module)

(type_declaration (type_spec name: (type_identifier) @name.definition.interface type: (interface_type)))

(type_declaration (type_spec name: (type_identifier) @name.definition.class type: (struct_type)))

(import_declaration (import_spec) @name.reference.module)

(var_declaration (var_spec name: (identifier) @name.definition.variable))

(const_declaration (const_spec name: (identifier) @name.definition.constant))
