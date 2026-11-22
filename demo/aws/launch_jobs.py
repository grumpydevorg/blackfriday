"""Lambda entry points for independent, trusted reference-shop evaluations."""

import os
from time import time_ns
from typing import Any

from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python.config import ParallelConfig

from launch_wave import SCENARIO, evaluate

PLANS = ('small', 'replicas', 'large', 'cached')
MAX_COHORTS = 8
MAX_PARALLEL = 8
BASE_SEED = 42


def worker(event: dict[str, Any], context: Any) -> dict[str, Any]:
    plan, cohort = event['plan'], event['cohort']
    if (
        event['scenario'] != SCENARIO or plan not in PLANS
        or type(cohort) is not int or not 0 <= cohort < MAX_COHORTS
    ):
        raise ValueError('Invalid launch-wave evaluation request')
    seed = BASE_SEED + cohort
    start = time_ns() // 1000
    result = evaluate(plan, seed)
    end = time_ns() // 1000
    return {
        **result, 'plan': plan, 'cohort': cohort, 'seed': seed,
        'started_us': start, 'ended_us': end, 'aws_request_id': context.aws_request_id,
    }


@durable_execution
def workflow(event: dict[str, Any], context: DurableContext) -> dict[str, Any]:
    cohorts, parallel = event['cohorts'], event['parallel']
    if (
        type(cohorts) is not int or not 1 <= cohorts <= MAX_COHORTS
        or type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL
    ):
        raise ValueError('Expected 1–8 cohorts and 1–8 parallel workers')
    function = os.environ['LAUNCH_WORKER']

    def branch(plan: str, cohort: int):
        def evaluate_one(child: DurableContext) -> dict[str, Any]:
            return child.invoke(function, {'scenario': SCENARIO, 'plan': plan, 'cohort': cohort}, name='evaluate')
        return evaluate_one

    batch = context.parallel(
        [branch(plan, cohort) for cohort in range(cohorts) for plan in PLANS],
        name='launch-wave', config=ParallelConfig(max_concurrency=parallel),
    )
    batch.throw_if_error()
    jobs = sorted(batch.get_results(), key=lambda job: (job['cohort'], job['plan']))
    return {'scenario': SCENARIO, 'cohorts': cohorts, 'parallel': parallel, 'jobs': jobs}
