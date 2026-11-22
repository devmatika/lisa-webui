"""Knowledge tab must ship the nested-folder tree (not the old flat path-subtitle list)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static" / "company_knowledge.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_company_knowledge_js_builds_nested_folder_tree():
    assert "function docsToTree(" in JS
    assert "data-ck-folder=" in JS
    assert "function renderNode(" in JS
    assert "seedFirstLevelFolders" in JS
    # Old flat list rendered path crumbs under each note; nested UI must not fall back to that.
    assert "ck-doc-folder" not in JS.split("function areaList")[1].split("function resultsList")[0]


def test_company_knowledge_css_indents_nested_folders():
    assert ".ck-folder-head" in CSS
    assert "--ck-depth" in CSS


def test_docs_to_tree_groups_nested_project_paths():
    """Mirror of docsToTree(): area/sub/.../note.md → folder nodes, not a flat note list."""
    docs = [
        {"path": "projects/README.md", "title": "README"},
        {"path": "projects/bysusann/Project.md", "title": "Project"},
        {"path": "projects/henry/Tasks/a.md", "title": "a"},
        {"path": "projects/henry/Tasks/b.md", "title": "b"},
        {"path": "projects/henry/Knowledge/c.md", "title": "c"},
    ]
    root = {"folders": {}, "docs": []}
    for d in docs:
        parts = [p for p in d["path"].split("/") if p]
        if len(parts) < 2:
            continue
        rel = parts[1:]
        node = root
        for name in rel[:-1]:
            node["folders"].setdefault(name, {"name": name, "folders": {}, "docs": []})
            node = node["folders"][name]
        node["docs"].append(d)
    assert set(root["folders"]) == {"bysusann", "henry"}
    assert [d["title"] for d in root["docs"]] == ["README"]
    assert set(root["folders"]["henry"]["folders"]) == {"Tasks", "Knowledge"}
    assert len(root["folders"]["henry"]["folders"]["Tasks"]["docs"]) == 2
