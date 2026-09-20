"""Pure prerequisite checks for guarded lifecycle transitions."""

from __future__ import annotations


GUARDED_REQUIREMENTS = {
    ("planning", "execution"): (
        ("plan_artifact_missing", "Сохраните непустой артефакт плана."),
        ("planning_not_ready", "Сначала закройте критичные вопросы и подготовьте исполнимый план."),
        ("plan_not_approved", "Явно подтвердите сохранённый план."),
    ),
    ("execution", "validation"): (
        ("execution_result_missing", "Сохраните непустой результат выполнения."),
        ("implementation_not_ready", "Явно отметьте реализацию готовой к проверке."),
    ),
    ("validation", "done"): (
        ("validation_not_passed", "Явно зафиксируйте успешную валидацию."),
        ("blocking_issues_present", "Устраните все блокирующие замечания."),
    ),
}


def unmet_requirements(task: dict, target: str) -> list[dict]:
    route=(task["phase"],target)
    unmet=[]
    for code,message in GUARDED_REQUIREMENTS.get(route,()):
        failed={
            "plan_artifact_missing":not task["plan_artifact"].strip(),
            "planning_not_ready":not task["planning_ready"],
            "plan_not_approved":not task["plan_approved"],
            "execution_result_missing":not task["execution_result"].strip(),
            "implementation_not_ready":not task["implementation_ready"],
            "validation_not_passed":not task["validation_passed"],
            "blocking_issues_present":bool(task["blocking_issues"].strip()),
        }[code]
        if failed:
            unmet.append({"code":code,"message":message})
    return unmet
