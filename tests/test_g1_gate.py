"""Fail-closed gate bookkeeping counterexamples; no provider inputs."""

from metrics.g1_gate import EXPECTED_JOBS, ci_ready, evaluate


def receipt():
    return {"status": "completed", "conclusion": "success",
            "target_sha": "a" * 40,
            "jobs": [{"name": name, "conclusion": "success", "checkout_assertion": "success",
                      "checkout_sha": "a" * 40} for name in sorted(EXPECTED_JOBS)]}


def test_pending_failed_or_missing_matrix_leg_cannot_verify_gate():
    good = receipt()
    assert ci_ready([good])
    assert not ci_ready([])
    assert not ci_ready([{**good, "status": "queued"}])
    assert not ci_ready([{**good, "conclusion": "failure"}])
    assert not ci_ready([{**good, "jobs": good["jobs"][:-1]}])
    assert not ci_ready([{**good, "target_sha": "b" * 40}])
    assert not ci_ready([{**good, "jobs": good["jobs"] + good["jobs"][:1]}])


def test_empty_evidence_cannot_pass_even_with_green_ci(tmp_path):
    report = evaluate(tmp_path, ci=[receipt()], current_tape={})
    assert report["verdict"] == "REVIEW_REQUIRED"
    assert len(report["criteria"]) == 10
    assert report["errors"]


def test_green_summary_cannot_hide_failed_job():
    bad = receipt()
    bad["jobs"][0]["conclusion"] = "failure"
    assert not ci_ready([bad])
