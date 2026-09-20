"""Generates the active project's CATALOG.md from its catalog_data.py, optionally stamped with the
last run's per-row status.
"""

import json
import os

from . import config

STATUS_ICON = {
    "pass": "✅ pass",
    "fail": "❌ fail",
    "skipped": "⏭️ skipped",
    "manual": "— manual",
    None: "—",
}


def _status_path():
    return os.path.join(config.REPORTS_DIR, "last_status.json")


def load_last_status():
    if not os.path.exists(_status_path()):
        return {}
    with open(_status_path()) as f:
        return json.load(f)


def save_last_status(status_by_order, run_timestamp, report_path):
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    with open(_status_path(), "w") as f:
        json.dump(
            {"run_timestamp": run_timestamp, "report_path": report_path, "status": status_by_order},
            f,
            indent=2,
        )


def render_catalog_md(status_by_order=None, run_timestamp=None):
    status_by_order = status_by_order or {}

    lines = [
        f"# {config.PROJECT_NAME} — KD-TTOOL Catalog",
        "",
        "**Generated file — do not hand-edit the table.** To change a row, edit",
        f"`projects/{config.PROJECT_NAME}/catalog_data.py` (and the matching script), then re-run",
        f"`python run.py --project {config.PROJECT_NAME}`.",
        "",
        *config.CATALOG_INTRO,
        "",
    ]

    if run_timestamp:
        lines.append(f"_Last run: {run_timestamp}_")
        lines.append("")

    lines.append(
        "| # | Script | Area | Description | Credentials | Email used | Viewport | Verifies | Last run |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")

    for row in config.CATALOG:
        status_key = "manual" if row["manual"] else status_by_order.get(row["order"])
        status_label = STATUS_ICON.get(status_key, STATUS_ICON[None])
        script_label = row["script"] if row["script"] else "_(none — manual)_"
        money_prefix = "💰 " if row["money"] else ""
        lines.append(
            "| {order} | {script} | {money}{area} | {description} | {credentials} | {email} | {viewport} | {verifies} | {status} |".format(
                order=row["order"],
                script=script_label,
                money=money_prefix,
                area=row["area"],
                description=row["description"].replace("|", "\\|"),
                credentials=row["credentials"],
                email=row["email"],
                viewport=row["viewport"],
                verifies=row["verifies"].replace("|", "\\|"),
                status=status_label,
            )
        )

    return "\n".join(lines) + "\n"


def write_catalog_md(status_by_order=None, run_timestamp=None):
    content = render_catalog_md(status_by_order=status_by_order, run_timestamp=run_timestamp)
    with open(config.CATALOG_MD_PATH, "w") as f:
        f.write(content)
    return config.CATALOG_MD_PATH


if __name__ == "__main__":
    # python -m kdttool.catalog <project>
    import sys
    config.activate(sys.argv[1] if len(sys.argv) > 1 else "example")
    last = load_last_status()
    write_catalog_md(
        status_by_order=last.get("status"),
        run_timestamp=last.get("run_timestamp"),
    )
    print(f"Wrote {config.CATALOG_MD_PATH}")
