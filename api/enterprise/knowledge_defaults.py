"""The standard company vault: the folders every new organisation starts with, and who may read them.

An organisation is created with these areas, so the administrator only adjusts access instead of
building the structure. The same folder names make up the starter vault a client can put in Git,
and a connected vault whose folders carry these names lands on the same areas, keeping their access.
"""
from __future__ import annotations

import io
import zipfile

from api.enterprise.directory import Actor
from api.enterprise.models import KnowledgeArea

# folder in the vault, area key, area name, what belongs there, readers, contributors (Teach Lisa)
STANDARD_FOLDERS = [
    ("01-People", "people", "People", "One note per employee: role, responsibilities, contact.",
     ["manager"], []),
    ("02-Companies", "companies", "Companies", "Customers, partners and suppliers.", ["staff", "manager"], []),
    ("03-Projects", "projects", "Projects", "One folder or note per project: goals, budget, status.",
     ["manager"], []),
    ("04-Meetings", "meetings", "Meetings", "Meeting notes and minutes.", ["staff", "manager"], []),
    ("05-Decisions", "decisions", "Decisions", "Decisions taken, with date and reason.", ["staff", "manager"], []),
    ("06-Knowledge", "knowledge", "Knowledge", "How-tos, policies, FAQs: what every employee should know.",
     ["viewer"], ["staff", "manager"]),
    ("07-Daily", "daily", "Daily", "Daily notes and logs.", ["staff", "manager"], []),
    ("08-Team-Briefing", "team-briefing", "Team briefing", "Team updates and announcements.",
     ["viewer", "staff", "manager"], []),
]
# in the starter vault, never read by Lisa
UNREAD_FOLDERS = [("00-Inbox", "New notes before they are sorted."),
                  ("90-Templates", "Obsidian templates."), ("99-Archive", "Old notes nobody needs any more.")]


def standard_grants(readers: list[str], contributors: list[str]) -> list[dict]:
    grants = [{"principal_type": "role", "principal_id": r, "level": "read"} for r in readers]
    grants += [{"principal_type": "role", "principal_id": r, "level": "contribute"} for r in contributors]
    grants.append({"principal_type": "role", "principal_id": "admin", "level": "approve"})
    return grants


def add_standard_areas(db, actor: Actor) -> list[str]:
    """Create the standard areas that do not exist yet; existing areas and their access stay as they are."""
    from api.enterprise.knowledge import save_area
    added = []
    for _folder, key, name, _about, readers, contributors in STANDARD_FOLDERS:
        if db.get(KnowledgeArea, key) is None:
            save_area(db, actor, key, name, standard_grants(readers, contributors))
            added.append(key)
    return added


def missing_standard_areas(db) -> list[str]:
    return [name for _f, key, name, *_ in STANDARD_FOLDERS if db.get(KnowledgeArea, key) is None]


def starter_vault_zip(org_name: str = "") -> bytes:
    """An empty Obsidian vault with the standard folders, ready to push to the company's Git."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        title = f"{org_name} company vault" if org_name else "Company vault"
        rows = "\n".join(f"| `{f}` | {about} |" for f, _k, _n, about, _r, _c in STANDARD_FOLDERS)
        rows += "\n" + "\n".join(f"| `{f}` | {about} Lisa does not read it. |" for f, about in UNREAD_FOLDERS)
        z.writestr("company-vault/README.md", f"# {title}\n\nOpen this folder in Obsidian, put it in a private "
                   "Git repository (GitLab or GitHub) and connect that repository in Lisa under Organization → "
                   "Knowledge → Company vault. Who may read each folder is set there.\n\n| Folder | What "
                   f"belongs there |\n| --- | --- |\n{rows}\n")
        for folder, _k, name, about, _r, _c in STANDARD_FOLDERS:
            z.writestr(f"company-vault/{folder}/{name}.md", f"# {name}\n\n{about}\n")
        for folder, about in UNREAD_FOLDERS:
            z.writestr(f"company-vault/{folder}/.gitkeep", "")
        z.writestr("company-vault/.gitignore", ".obsidian/workspace*.json\n.trash/\n")
    return buf.getvalue()
