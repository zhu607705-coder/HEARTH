"""Small offline guardrail scan. This is NOT a substitute for a vulnerability scanner."""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    findings = []
    files = sorted((root / "src").rglob("*.py")) + sorted((root / "tests").rglob("*.py")) + sorted((root / "scripts").glob("*.py"))
    for path in files:
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                findings.append(f"{path.relative_to(root)}:{node.lineno}: dynamic code evaluation")
            if any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                findings.append(f"{path.relative_to(root)}:{node.lineno}: shell=True")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany"} and node.args:
                if isinstance(node.args[0], (ast.JoinedStr, ast.BinOp)):
                    findings.append(f"{path.relative_to(root)}:{node.lineno}: interpolated SQL")
        if re.search(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)", text):
            findings.append(f"{path.relative_to(root)}: credential-like literal")
    js = root / "src/hearth/dashboard.js"
    subprocess.run(["node", "--check", str(js)], check=True)
    if any(token in js.read_text() for token in ("innerHTML", "document.write", "localStorage.setItem", "eval(")):
        findings.append("dashboard.js: unsafe DOM/storage primitive")
    result = {"python_files_parsed": len(files), "findings": findings,
              "scope": "AST guardrails + selected credential patterns + JS syntax/DOM checks",
              "dependency_cve_scan": "not_performed_offline"}
    output = root / "artifacts/static-review.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
