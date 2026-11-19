package executor

import "strings"

// WrapLastLineInteractive wraps user code to execute in last-line-interactive
// mode.
//
// The generated wrapper uses Python's 'single' compilation mode for the last
// expression only, which automatically prints the value to stdout, mimicking
// Jupyter notebook behavior. Only the last line is affected; earlier
// expressions are not printed.
func WrapLastLineInteractive(code string) string {
	// Escape the code string for embedding in Python source.
	escaped := strings.ReplaceAll(code, `\`, `\\`)
	escaped = strings.ReplaceAll(escaped, `'`, `\'`)

	return `import ast
import sys

# User code
code = '''` + escaped + `'''

# Parse the code
tree = ast.parse(code)

# Execute all statements except the last one normally
if len(tree.body) > 0:
    for node in tree.body[:-1]:
        code_obj = compile(ast.Module(body=[node], type_ignores=[]), '<stdin>', 'exec')
        exec(code_obj)

    # For the last statement, check if it's an expression
    last_node = tree.body[-1]
    if isinstance(last_node, ast.Expr):
        # Execute in 'single' mode to print the result
        interactive = ast.Interactive(body=[last_node])
        ast.fix_missing_locations(interactive)
        code_obj = compile(interactive, '<stdin>', 'single')
        exec(code_obj)
    else:
        # Not an expression, execute normally
        code_obj = compile(ast.Module(body=[last_node], type_ignores=[]), '<stdin>', 'exec')
        exec(code_obj)
`
}
