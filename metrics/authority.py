"""Portable metadata path selection; explicit roots never fall back."""

from pathlib import Path

from universe._paths import repo_file

AUDIT_FILES = {
    "c2_report_path": ("g1-c2", "G1-C2_asof_reconstruction.yaml"),
    "g1b_rows_path": ("g1-b1", "G1-B1_disposition_rows.jsonl"),
    "g1b_report_path": ("g1-b1", "G1-B1_observed_universe_disposition.yaml"),
    "g1b_links_path": ("g1-b1", "G1-B1_reference_links.jsonl"),
}


def audit_paths(root: Path | None = None) -> dict[str, Path]:
    return {key: root / "evidence" / folder / name if root is not None
            else repo_file("evidence", folder, name)
            for key, (folder, name) in AUDIT_FILES.items()}
