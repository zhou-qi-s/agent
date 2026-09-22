import logging
from typing import List
from core.pipeline.step import Step
from core.executor.context import ExecContext


"""
Pipeline 执行器
负责按顺序执行 Step
"""


class PipelineRunner:

    def __init__(self, steps: List[Step]):
        self.steps = steps

    def execute(self, ctx: ExecContext):

        results = []

        for step in self.steps:

            logging.info("=" * 60)
            logging.info(f"STEP START: {step.name()}")

            try:
                result = step.run(ctx)

                results.append({
                    "step": step.name(),
                    "success": True,
                    "result": result
                })

                logging.info(f"STEP SUCCESS: {step.name()}")

            except Exception as e:

                logging.error(f"STEP FAILED: {step.name()} -> {e}")

                results.append({
                    "step": step.name(),
                    "success": False,
                    "error": str(e)
                })

                break

        return results