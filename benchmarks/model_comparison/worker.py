"""Activate isolated configuration, then execute the original worker unchanged."""

from benchmarks.model_comparison.runtime import activate


def main():
    activate()
    from benchmarks.evaluation.worker import main as execute_original_worker
    execute_original_worker()


if __name__ == '__main__':
    main()
