"""Cron jobs no longer load skills; old job records that carry them still run.

A job created before skills were removed may still have ``skill``/``skills``
in jobs.json on another instance. Such a job must keep running on its prompt
alone (with one warning per run naming the ignored skills), and a job that
had nothing but skills must fail with a clear error instead of waking an
agent on an empty prompt.
"""

from __future__ import annotations

import logging

from cron import jobs as cron_jobs
from cron import scheduler

_SKILL_BODY = "UNIQUE-SKILL-BODY-THAT-MUST-NOT-LOAD"


def _install_skill(home, name: str) -> None:
    skill_dir = home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f'---\nname = "{name}"\ndescription = "Demo skill."\n---\n\n{_SKILL_BODY}\n',
        encoding="utf-8",
    )


def _legacy_job(prompt: str, skills: list[str]) -> dict:
    """Create a job the current way, then give its record the pre-removal fields."""
    job = cron_jobs.create_job(prompt=prompt, schedule="30m", name="legacy")
    records = cron_jobs.load_jobs()
    for record in records:
        if record["id"] == job["id"]:
            record["skills"] = list(skills)
            record["skill"] = skills[0]
    cron_jobs.save_jobs(records)
    stored = cron_jobs.get_job(job["id"])
    assert stored is not None
    return stored


def _skill_warnings(caplog, skill: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == scheduler.logger.name and skill in r.getMessage()
    ]


def test_new_jobs_carry_no_skill_fields() -> None:
    job = cron_jobs.create_job(prompt="Check the disk.", schedule="30m")
    stored = cron_jobs.get_job(job["id"])
    assert "skills" not in stored and "skill" not in stored


def test_a_job_with_skills_runs_its_prompt_and_warns_once(son_of_anton_home, caplog) -> None:
    _install_skill(son_of_anton_home, "blogwatcher")
    job = _legacy_job("Summarize the overnight logs.", ["blogwatcher"])
    # Loading must not strip the fields: the scheduler is what reads them.
    assert cron_jobs.legacy_job_skills(job) == ["blogwatcher"]

    caplog.set_level(logging.WARNING, logger=scheduler.logger.name)
    prompt = scheduler._build_job_prompt(job)

    assert "Summarize the overnight logs." in prompt
    assert _SKILL_BODY not in prompt
    warnings = _skill_warnings(caplog, "blogwatcher")
    assert len(warnings) == 1
    assert job["id"] in warnings[0].getMessage()

    # Re-authoring the prompt folds the skills in and silences the warning.
    cron_jobs.update_job(job["id"], {"prompt": "Summarize the logs and flag errors."})
    edited = cron_jobs.get_job(job["id"])
    assert cron_jobs.legacy_job_skills(edited) == []
    caplog.clear()
    scheduler._build_job_prompt(edited)
    assert not _skill_warnings(caplog, "blogwatcher")


def test_a_job_with_only_skills_fails_with_a_clear_error(son_of_anton_home) -> None:
    _install_skill(son_of_anton_home, "blogwatcher")
    job = _legacy_job("", ["blogwatcher"])

    success, doc, final_response, error = scheduler.run_job(job)

    assert success is False
    assert final_response == ""
    assert error and "no prompt" in error and "blogwatcher" in error
    assert error in doc
    assert _SKILL_BODY not in doc
