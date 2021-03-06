
# =============================================================================
# IMPORTS
# =============================================================================

import logging
import sys
import os
import datetime
import codecs

from django.conf import settings, global_settings
from django.core.management.base import BaseCommand

from otree.test import runner, client
from otree.management.cli import otree_and_django_version
from otree.session import SESSION_CONFIGS_DICT

# =============================================================================
# CONSTANTS
# =============================================================================

COVERAGE_CONSOLE = "console"
COVERAGE_HTML = "HTML"
COVERAGE_ALL = "all"
COVERAGE_CHOICES = (COVERAGE_ALL, COVERAGE_CONSOLE, COVERAGE_HTML)


# =============================================================================
# LOGGER & Other Conf
# =============================================================================

logger = logging.getLogger('otree')

settings.SSLIFY_DISABLE = True

settings.STATICFILES_STORAGE = global_settings.STATICFILES_STORAGE


# =============================================================================
# COMMAND
# =============================================================================

class Command(BaseCommand):
    help = ('Discover and run experiment tests in the specified '
            'modules or the current directory.')

    def _get_action(self, parser, signature):
        option_strings = list(signature)
        for idx, action in enumerate(parser._actions):
            if action.option_strings == option_strings:
                return parser._actions[idx]

    def add_arguments(self, parser):
        # Positional arguments
        parser.add_argument(
            'session_name', nargs='*',
            help='If omitted, all sessions in SESSION_CONFIGS are run'
        )

        coverage_choices = "|".join(COVERAGE_CHOICES)
        ahelp = ('Execute code-coverage over the code of '
                 'tested experiments [{}]').format(coverage_choices)
        parser.add_argument(
            '-c', '--coverage', action='store', dest='coverage',
            choices=COVERAGE_CHOICES, help=ahelp)
        parser.add_argument(
            '--export', nargs='?', const='auto_name',
            help=(
                'Saves the data generated by the tests. '
                'Runs the "export data" command, '
                'outputting the CSV files to the specified directory, '
                'or an auto-generated one.'),
            )
        parser.add_argument(
            '--save', nargs='?', const='auto_name',
            help=(
                'Alias for --export.'),
            )

        v_action = self._get_action(parser, ("-v", "--verbosity"))
        v_action.default = '2'
        v_action.help = (
            'Verbosity level; 0=minimal output, 1=normal output,'
            '2=verbose output (DEFAULT), 3=very verbose output')

    def execute(self, *args, **options):
        if int(options['verbosity']) > 3:
            logger = logging.getLogger('py.warnings')
            handler = logging.StreamHandler()
            logger.addHandler(handler)
        super(Command, self).execute(*args, **options)
        if int(options['verbosity']) > 3:
            logger.removeHandler(handler)

    def handle(self, **options):
        session_config_names = options["session_name"]
        if not session_config_names:
            # default to all session configs
            session_config_names = SESSION_CONFIGS_DICT.keys()

        if options['verbosity'] == 0:
            level = logging.ERROR
        elif options['verbosity'] == 1:
            level = logging.WARNING
        elif options['verbosity'] == 2:
            level = logging.INFO
        else:  # 3
            level = logging.DEBUG

        options['verbosity'] = (
            options['verbosity'] if options['verbosity'] > 2 else 1)

        logging.basicConfig(level=level)
        logging.getLogger("otree").setLevel(level)
        runner.logger.setLevel(level)
        client.logger.setLevel(level)

        export_path = options["export"] or options["save"]
        preserve_data = bool(export_path)

        test_runner = runner.OTreeExperimentTestRunner(**options)

        coverage = options["coverage"]

        if coverage:
            with runner.covering(session_config_names) as coverage_report:
                failures, data = test_runner.run_tests(
                    session_config_names, preserve_data=preserve_data)
        else:
            failures, data = test_runner.run_tests(
                session_config_names, preserve_data=preserve_data)
        if coverage:
            logger.info("Coverage Report")
            if coverage in [COVERAGE_CONSOLE, COVERAGE_ALL]:
                coverage_report.report()
            if coverage in [COVERAGE_HTML, COVERAGE_ALL]:
                html_coverage_results_dir = '_coverage_results'
                coverage_report.html_report(
                    directory=html_coverage_results_dir)
                msg = ("See '{}/index.html' for detailed results.").format(
                    html_coverage_results_dir)
                logger.info(msg)

        if preserve_data:
            now = datetime.datetime.now()

            if export_path == 'auto_name':
                export_path = now.strftime('_bots_%b%d_%Hh%Mm%S.%f')[:-5] + 's'

            if os.path.isdir(export_path):
                msg = "Directory '{}' already exists".format(export_path)
                raise IOError(msg)

            os.makedirs(export_path)

            metadata = dict(options)
            metadata.update({
                "timestamp": now.isoformat(),
                "versions": otree_and_django_version(),
                "failures": failures, "error": bool(failures)})

            sizes = {}
            for session_name, session_data in data.items():
                session_data = session_data or ""
                sizes[session_name] = len(session_data.splitlines())
                fname = "{}.csv".format(session_name)
                fpath = os.path.join(export_path, fname)
                with codecs.open(fpath, "w", encoding="utf8") as fp:
                    fp.write(session_data)

                metainfo = "\n".join(
                    ["{}: {}".format(k, v) for k, v in metadata.items()] +
                    ["sizes:"] +
                    ["\t{}: {}".format(k, v) for k, v in sizes.items()] + [""])
                fpath = os.path.join(export_path, "meta.txt")
                with codecs.open(fpath, "w", encoding="utf8") as fp:
                    fp.write(metainfo)
            logger.info('Exported CSV to folder "{}"'.format(export_path))
        else:
            logger.info('Tip: Run this command with the --export flag'
                        ' to save the data generated by bots.')

        if failures:
            sys.exit(bool(failures))
