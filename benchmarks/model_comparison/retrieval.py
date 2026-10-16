"""Execute the same actual-index retrieval suite with a recorded model config."""

from benchmarks.model_comparison.runtime import activate


def main():
    activate()
    from benchmarks.retrieval.run import main as run_retrieval
    run_retrieval()


if __name__=='__main__':
    main()
