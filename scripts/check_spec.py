"""Mechanical checks for the delivered design candidate (not semantic approval)."""
import ast
import json
from pathlib import Path
import re


root = Path(__file__).resolve().parents[1]
text = (root / "docs/Loom.md").read_text(encoding="utf-8")
headings = set(re.findall(r"^#{1,6}\s+(\d+(?:\.\d+)*(?:[a-z])?)", text, re.MULTILINE))
references = set(re.findall(r"§(\d+(?:\.\d+)*(?:[a-z])?)", text))
missing = sorted(references - headings)
assert not missing, f"undefined numeric sections: {missing}"
definitions = re.findall(r"^###\s+(ADR-\d{3})\s*[·：:]", text, re.MULTILINE)
assert len(definitions) == len(set(definitions)) == 10, definitions
assert set(definitions) == {f"ADR-{i:03d}" for i in range(1, 11)}
assert set(re.findall(r"ADR-\d{3}", text)) <= set(definitions)
python_blocks = re.findall(r"```python\s*\n(.*?)```", text, re.DOTALL)
json_blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
for code in python_blocks:
    ast.parse(code)
for code in json_blocks:
    json.loads(code)
resolution = (root / "docs/review-resolution.md").read_text(encoding="utf-8")
assert len(re.findall(r"^\| R\d{2} \|", resolution, re.MULTILINE)) == 24
print(json.dumps({"numeric_section_references": len(references), "unique_adrs": len(definitions),
                  "python_examples_parsed": len(python_blocks), "json_examples_parsed": len(json_blocks),
                  "review_resolution_rows": 24}, indent=2))
