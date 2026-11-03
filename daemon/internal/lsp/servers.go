package lsp

// serverDef is a built-in language server: the files it claims, the
// commands that start it (the first whose program is on PATH wins) and the
// files that mark a project root.
type serverDef struct {
	id       string
	exts     []string
	commands [][]string
	markers  []string
}

var builtin = []serverDef{
	{"pyright", []string{".py", ".pyi"},
		[][]string{{"basedpyright-langserver", "--stdio"}, {"pyright-langserver", "--stdio"}},
		[]string{"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "pyrightconfig.json"}},
	{"typescript", []string{".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"},
		[][]string{{"typescript-language-server", "--stdio"}}, []string{"package.json", "tsconfig.json"}},
	{"gopls", []string{".go"}, [][]string{{"gopls"}}, []string{"go.work", "go.mod"}},
	{"rust-analyzer", []string{".rs"}, [][]string{{"rust-analyzer"}}, []string{"Cargo.toml"}},
	{"clangd", []string{".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"},
		[][]string{{"clangd", "--background-index"}}, []string{"compile_commands.json", "compile_flags.txt", ".clangd"}},
	{"bash", []string{".sh", ".bash"}, [][]string{{"bash-language-server", "start"}}, nil},
	{"yaml", []string{".yaml", ".yml"}, [][]string{{"yaml-language-server", "--stdio"}}, nil},
	{"nixd", []string{".nix"}, [][]string{{"nixd"}}, []string{"flake.nix"}},
	{"texlab", []string{".tex", ".bib"}, [][]string{{"texlab"}}, nil},
	{"json", []string{".json", ".jsonc"}, [][]string{{"vscode-json-language-server", "--stdio"}}, nil},
}

// languageByExt is the languageId didOpen carries.
var languageByExt = map[string]string{
	".py": "python", ".pyi": "python",
	".ts": "typescript", ".tsx": "typescriptreact", ".mts": "typescript", ".cts": "typescript",
	".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
	".go": "go", ".rs": "rust",
	".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
	".sh": "shellscript", ".bash": "shellscript",
	".yaml": "yaml", ".yml": "yaml",
	".nix": "nix", ".tex": "latex", ".bib": "bibtex",
	".json": "json", ".jsonc": "jsonc",
}
