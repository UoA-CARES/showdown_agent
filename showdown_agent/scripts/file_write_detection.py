import ast
from pathlib import Path


class FileWriteAttemptDetector(ast.NodeVisitor):
    def __init__(self):
        self.findings = []

    @staticmethod
    def _is_str_constant(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    def _record(self, node, message):
        self.findings.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    def visit_Call(self, node):
        # Detect open(..., mode=...) where mode writes/appends/creates/updates files.
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            mode_node = None
            if len(node.args) >= 2:
                mode_node = node.args[1]
            else:
                for keyword in node.keywords:
                    if keyword.arg == "mode":
                        mode_node = keyword.value
                        break

            if mode_node is not None and self._is_str_constant(mode_node):
                mode_value = mode_node.value
                if any(flag in mode_value for flag in ("w", "a", "x", "+")):
                    self._record(node, f"open in write mode '{mode_value}'")

        # Detect common write APIs on paths/files/dataframes.
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "write",
            "writelines",
            "write_text",
            "write_bytes",
            "to_csv",
            "to_json",
            "dump",
            "dump_all",
            "save",
            "savetxt",
            "writerow",
            "writerows",
        }:
            self._record(node, f"call to '{node.func.attr}'")

        self.generic_visit(node)


def find_file_write_attempts(file_path: Path):
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except Exception as e:
        return [f"could not analyze file: {e}"]

    detector = FileWriteAttemptDetector()
    detector.visit(tree)
    return detector.findings
