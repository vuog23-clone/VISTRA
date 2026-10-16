"""Run fixed bilingual development measurements, compare runs, or apply human reviews."""

import argparse
from pathlib import Path
import sys

from benchmarks.measurement.engine import (compare, invalidate, load_run, output_directory, read, run,
                                          suite_cases, write)
from benchmarks.measurement.metrics import summarize
from benchmarks.measurement.reporting import comparison_report, report


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    execute = commands.add_parser('run', help='Create a new immutable measurement run.')
    execute.add_argument('--output', required=True, type=Path)
    execute.add_argument('--profile', choices=('offline','controlled'), default='offline')
    execute.add_argument('--live', action='store_true')
    execute.add_argument('--repeats', type=int, default=3)
    execute.add_argument('--seed', type=int, default=17)
    execute.add_argument('--deadline', type=float, default=300)
    execute.add_argument('--families', nargs='+')
    execute.add_argument('--pricing', type=Path)
    paired = commands.add_parser('compare', help='Reject incompatible runs; list exact check regressions.')
    paired.add_argument('--baseline', type=Path, required=True)
    paired.add_argument('--candidate', type=Path, required=True)
    paired.add_argument('--output', type=Path, required=True)
    review = commands.add_parser('review', help='Create a separate scored human-review report.')
    review.add_argument('--run', type=Path, required=True)
    review.add_argument('--labels', type=Path, required=True)
    review.add_argument('--output', type=Path, required=True)
    invalid = commands.add_parser('invalidate',help='Mark a broken harness/fixture run unusable without changing raw results.')
    invalid.add_argument('--run',type=Path,required=True)
    invalid.add_argument('--reason',required=True)
    commands.add_parser('list', help='Show fixed families without calling providers.')
    args = parser.parse_args()
    try:
        if args.command == 'list':
            for profile in ('offline','controlled'):
                _, _, cases = suite_cases(profile)
                print(profile+': '+str(len(cases))+' bilingual cases; '+str(len(cases)*3)+' executions with default repeats')
                for case in cases:
                    if case['language'] == 'en':
                        print('  '+case['id']+' ['+case['area']+']')
            return 0
        if args.command == 'run':
            result = run(args.output, profile=args.profile, live=args.live, repeats=args.repeats, seed=args.seed,
                         deadline=args.deadline, families=args.families, pricing_path=args.pricing)
            print(f"Automatic checks: {result['overall']['automatic_passed']}/{result['overall']['planned_executions']}; complete={result['complete']}")
            return 2 if not result['complete'] else 1 if result['overall']['automatic_pass_rate'] != 1 else 0
        if args.command == 'compare':
            result = compare(args.baseline,args.candidate)
            output = output_directory(args.output)
            write(output/'comparison.json',result)
            (output/'REPORT.md').write_text(comparison_report(result),encoding='utf-8')
            print(f"Paired check regressions: {len(result['regressions'])}; improvements: {len(result['improvements'])}")
            return 1 if result['regressions'] else 0
        if args.command == 'invalidate':
            invalidate(args.run,args.reason)
            return 0
        manifest, rows, original = load_run(args.run)
        if not original['complete']:
            raise ValueError('Human review cannot certify an incomplete measurement run.')
        labels = read(args.labels)
        result = summarize(manifest,rows,manifest.get('pricing'),labels)
        output = output_directory(args.output)
        write(output/'summary.json',result)
        write(output/'reviews.json',labels)
        write(output/'provenance.json',{'source_run':str(args.run.resolve()),'suite_version':manifest['suite_version']})
        (output/'REPORT.md').write_text(report(manifest,result),encoding='utf-8')
        return 0
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
