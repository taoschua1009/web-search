"""Keep the layered layout honest: lower layers never import higher ones."""

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# package -> packages it must not import
FORBIDDEN = {
    "src/schemas": ("agents", "services", "workflows", "api", "tools"),
    "src/configs/research.py": ("agents", "services", "workflows", "api", "tools"),
    "src/services/research": ("agents", "workflows", "api", "tools"),
    "src/agents/email_agent.py": ("workflows", "api", "tools"),
    "src/agents/web_research_agent.py": ("workflows", "api", "tools"),
    "src/agents/company_info_agent.py": ("workflows", "api", "tools"),
    "src/agents/calendar_agent.py": ("workflows", "api", "tools"),
    "src/agents/memory_agent.py": ("workflows", "api", "tools"),
    "src/agents/report_agent.py": ("workflows", "api", "tools"),
    "src/agents/approval_agent.py": ("workflows", "api", "tools"),
    "src/agents/search_agent.py": ("workflows", "api", "tools"),
    "src/agents/fast_research_agent.py": ("workflows", "api", "tools"),
    "src/workflows": ("api", "tools"),
}


def imported_packages(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[0] == "src" and len(parts) > 1:
                yield parts[1], node.module
        elif isinstance(node, ast.ImportFrom) and node.level:
            yield "relative", "." * node.level + (node.module or "")


class ArchitectureTests(unittest.TestCase):
    def test_dependencies_only_point_downward(self):
        problems = []
        for target, banned in FORBIDDEN.items():
            root = SRC.parent / target
            files = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for path in files:
                for package, module in imported_packages(path):
                    if package in banned:
                        problems.append(f"{path.relative_to(SRC.parent)} imports {module}")
        self.assertEqual(problems, [])

    def test_research_code_uses_absolute_imports_and_old_package_is_gone(self):
        self.assertFalse((SRC / "research").exists())
        relative = []
        for folder in ("schemas", "workflows", "agents", "services/research"):
            for path in (SRC / folder).rglob("*.py"):
                relative += [
                    f"{path.relative_to(SRC)}: {module}"
                    for package, module in imported_packages(path)
                    if package == "relative"
                ]
        self.assertEqual(relative, [])

    def test_every_new_package_has_an_init(self):
        for folder in (
            "schemas",
            "workflows",
            "services/research",
            "services/research/backends",
        ):
            self.assertTrue((SRC / folder / "__init__.py").is_file(), folder)


if __name__ == "__main__":
    unittest.main()
