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
# Area keys stay stable across friendly renames of the folder (e.g. knowledge ← 06-Company-Knowledge).
STANDARD_FOLDERS = [
    ("01-People", "people", "People", "One note per employee: role, responsibilities, contact.",
     ["manager"], []),
    ("02-Companies", "companies", "Companies", "Customers, partners and suppliers.", ["staff", "manager"], []),
    ("03-Projects", "projects", "Projects",
     "One folder per project (Project.md + notes + files). Do not dump loose files at the projects root.",
     ["manager"], []),
    ("04-Meetings", "meetings", "Meetings", "Meeting notes and minutes.", ["staff", "manager"], []),
    ("05-Decisions", "decisions", "Decisions", "Decisions taken, with date and reason.", ["staff", "manager"], []),
    ("06-Company-Knowledge", "knowledge", "Company Knowledge",
     "Company purpose, goals, policies, SOPs and FAQs every employee should know.",
     ["viewer"], ["staff", "manager"]),
    ("07-Daily", "daily", "Daily", "Daily notes and logs.", ["staff", "manager"], []),
    ("08-Team-Briefing", "team-briefing", "Team briefing", "Team updates and announcements.",
     ["viewer", "staff", "manager"], []),
]

# Ops / deliverables — never suggested as Knowledge areas
UNREAD_FOLDERS = [
    ("00-Inbox", "New notes before they are sorted."),
    ("90-Templates", "Obsidian templates."),
    ("99-Archive", "Old notes nobody needs any more."),
    ("10-Company-Files", "Binary and export deliverables by type (pdf/html/excel/word) and purpose "
     "(offers/analyses/tech). Not searched as Knowledge."),
]

# Created under 10-Company-Files/ in the starter vault
COMPANY_FILES_SUBDIRS = (
    "pdf", "html", "excel", "word", "markdown", "other",
    "offers", "analyses", "tech", "team",
)

# Created under 06-Company-Knowledge/ in the starter vault
COMPANY_KNOWLEDGE_STARTERS = (
    ("Company.md", "# Company\n\nWho we are, what we do, and how we work.\n\n## Mission\n\n## Positioning\n\n"),
    ("Goals.md", "# Goals\n\nCurrent company goals and OKRs.\n\n## This year\n\n## This quarter\n\n"),
    ("Policies.md", "# Policies\n\nCompany policies and rules of engagement.\n\n"),
)


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
        z.writestr("company-vault/README.md", f"# {title}\n\nThis is the **one company vault**. Open it in "
                   "Obsidian for editors, put it in a private Git repository, and connect **that same** repository "
                   "in Lisa under Organization → Knowledge → Company vault. Team members do **not** each need a "
                   "personal Obsidian with this structure — they use Lisa chat, the Knowledge tab (areas they may "
                   "read), and their private Hermes Files workspace.\n\n"
                   "**Projects:** one folder per project under `03-Projects/<slug>/` with `Project.md`.\n"
                   "**Company Knowledge:** purpose, goals, policies under `06-Company-Knowledge/`.\n"
                   "**Company Files:** PDFs/HTML/Excel/Word under `10-Company-Files/{{pdf,html,excel,word}}/` "
                   "(plus offers/analyses/tech). Private drafts stay in each user's Hermes workspace.\n\n"
                   f"| Folder | What belongs there |\n| --- | --- |\n{rows}\n")
        for folder, _k, name, about, _r, _c in STANDARD_FOLDERS:
            z.writestr(f"company-vault/{folder}/{name}.md", f"# {name}\n\n{about}\n")
        for folder, about in UNREAD_FOLDERS:
            z.writestr(f"company-vault/{folder}/.gitkeep", "")
        for sub in COMPANY_FILES_SUBDIRS:
            z.writestr(f"company-vault/10-Company-Files/{sub}/.gitkeep", "")
        for fname, body in COMPANY_KNOWLEDGE_STARTERS:
            z.writestr(f"company-vault/06-Company-Knowledge/{fname}", body)
        z.writestr("company-vault/03-Projects/README.md",
                   "# Projects\n\nOne folder per project:\n\n"
                   "```text\n03-Projects/<project-slug>/\n  Project.md      # goals, status, stakeholders\n"
                   "  notes/          # durable project notes\n"
                   "  files/          # project attachments (pdf/html/excel…)\n"
                   "    pdf/\n    html/\n    excel/\n```\n\n"
                   "Do not leave loose project files in the `03-Projects/` root.\n")
        z.writestr("company-vault/03-Projects/_example-project/Project.md",
                   "---\ntitle: Example project\nstatus: active\nowner: \"\"\n---\n\n"
                   "# Example project\n\n## Objective\n\n## Status\n\n## Stakeholders\n\n")
        z.writestr("company-vault/03-Projects/_example-project/notes/.gitkeep", "")
        for sub in ("pdf", "html", "excel", "word", "other"):
            z.writestr(f"company-vault/03-Projects/_example-project/files/{sub}/.gitkeep", "")
        z.writestr("company-vault/.gitignore", ".obsidian/workspace*.json\n.trash/\n")
    return buf.getvalue()
