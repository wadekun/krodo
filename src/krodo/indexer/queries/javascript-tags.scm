;; ---------------------------------------------------------------------------
;; javascript-tags.scm — tree-sitter tag query.
;; ---------------------------------------------------------------------------
;; Source:      Aider <aider/queries/tree-sitter-language-pack/javascript-tags.scm>
;; Upstream:    https://github.com/Goldziher/tree-sitter-language-pack (MIT)
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
  (method_definition
    name: (property_identifier) @name.definition.method) @definition.method
  (#not-eq? @name.definition.method "constructor")
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.method)
)

(
  (comment)* @doc
  .
  [
    (class
      name: (_) @name.definition.class)
    (class_declaration
      name: (_) @name.definition.class)
  ] @definition.class
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.class)
)

(
  (comment)* @doc
  .
  [
    (function_expression
      name: (identifier) @name.definition.function)
    (function_declaration
      name: (identifier) @name.definition.function)
    (generator_function
      name: (identifier) @name.definition.function)
    (generator_function_declaration
      name: (identifier) @name.definition.function)
  ] @definition.function
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (variable_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(assignment_expression
  left: [
    (identifier) @name.definition.function
    (member_expression
      property: (property_identifier) @name.definition.function)
  ]
  right: [(arrow_function) (function_expression)]
) @definition.function

(pair
  key: (property_identifier) @name.definition.function
  value: [(arrow_function) (function_expression)]) @definition.function

(
  (call_expression
    function: (identifier) @name.reference.call) @reference.call
  (#not-match? @name.reference.call "^(require)$")
)

(call_expression
  function: (member_expression
    property: (property_identifier) @name.reference.call)
  arguments: (_) @reference.call)

(new_expression
  constructor: (_) @name.reference.class) @reference.class
