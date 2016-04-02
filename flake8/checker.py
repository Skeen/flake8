"""Checker Manager and Checker classes."""
import errno
import logging
import os
import sys
import tokenize

try:
    import multiprocessing
except ImportError:
    multiprocessing = None

from flake8 import exceptions
from flake8 import processor
from flake8 import utils

LOG = logging.getLogger(__name__)

SERIAL_RETRY_ERRNOS = set([
    # ENOSPC: Added by sigmavirus24
    # > On some operating systems (OSX), multiprocessing may cause an
    # > ENOSPC error while trying to trying to create a Semaphore.
    # > In those cases, we should replace the customized Queue Report
    # > class with pep8's StandardReport class to ensure users don't run
    # > into this problem.
    # > (See also: https://gitlab.com/pycqa/flake8/issues/74)
    errno.ENOSPC,
    # NOTE(sigmavirus24): When adding to this list, include the reasoning
    # on the lines before the error code and always append your error
    # code. Further, please always add a trailing `,` to reduce the visual
    # noise in diffs.
])


class Manager(object):
    """Manage the parallelism and checker instances for each plugin and file.

    This class will be responsible for the following:

    - Determining the parallelism of Flake8, e.g.:

      * Do we use :mod:`multiprocessing` or is it unavailable?

      * Do we automatically decide on the number of jobs to use or did the
        user provide that?

    - Falling back to a serial way of processing files if we run into an
      OSError related to :mod:`multiprocessing`

    - Organizing the results of each checker so we can group the output
      together and make our output deterministic.
    """

    def __init__(self, style_guide, arguments, checker_plugins):
        """Initialize our Manager instance.

        :param style_guide:
            The instantiated style guide for this instance of Flake8.
        :type style_guide:
            flake8.style_guide.StyleGuide
        :param list arguments:
            The extra arguments parsed from the CLI (if any)
        :param checker_plugins:
            The plugins representing checks parsed from entry-points.
        :type checker_plugins:
            flake8.plugins.manager.Checkers
        """
        self.arguments = arguments
        self.style_guide = style_guide
        self.options = style_guide.options
        self.checks = checker_plugins
        self.jobs = self._job_count()
        self.process_queue = None
        self.results_queue = None
        self.using_multiprocessing = False
        self.processes = []
        self.checkers = []

        if self.jobs > 1:
            self.using_multiprocessing = True

    @staticmethod
    def _cleanup_queue(q):
        while not q.empty():
            q.get_nowait()

    def _force_cleanup(self):
        if self.using_multiprocessing:
            for proc in self.processes:
                proc.join(0.2)
            self._cleanup_queue(self.process_queue)
            self._cleanup_queue(self.results_queue)

    def _job_count(self):
        # type: () -> Union[int, NoneType]
        # First we walk through all of our error cases:
        # - multiprocessing library is not present
        # - we're running on windows in which case we know we have significant
        #   implemenation issues
        # - the user provided stdin and that's not something we can handle
        #   well
        # - we're processing a diff, which again does not work well with
        #   multiprocessing and which really shouldn't require multiprocessing
        # - the user provided some awful input
        if not multiprocessing:
            LOG.warning('The multiprocessing module is not available. '
                        'Ignoring --jobs arguments.')
            return 0

        if utils.is_windows():
            LOG.warning('The --jobs option is not available on Windows. '
                        'Ignoring --jobs arguments.')
            return 0

        if utils.is_using_stdin(self.arguments):
            LOG.warning('The --jobs option is not compatible with supplying '
                        'input using - . Ignoring --jobs arguments.')
            return 0

        if self.options.diff:
            LOG.warning('The --diff option was specified with --jobs but '
                        'they are not compatible. Ignoring --jobs arguments.')
            return 0

        jobs = self.options.jobs
        if jobs != 'auto' and not jobs.isdigit():
            LOG.warning('"%s" is not a valid parameter to --jobs. Must be one '
                        'of "auto" or a numerical value, e.g., 4.', jobs)
            return 0

        # If the value is "auto", we want to let the multiprocessing library
        # decide the number based on the number of CPUs. However, if that
        # function is not implemented for this particular value of Python we
        # default to 1
        if jobs == 'auto':
            try:
                return multiprocessing.cpu_count()
            except NotImplementedError:
                return 0

        # Otherwise, we know jobs should be an integer and we can just convert
        # it to an integer
        return int(jobs)

    def _results(self):
        seen_done = 0
        while True:
            LOG.info('Retrieving results')
            result = self.results_queue.get()
            if result == 'DONE':
                seen_done += 1
                if seen_done >= self.jobs:
                    break
                continue

            yield result

    def _report_after_parallel(self):
        style_guide = self.style_guide
        final_results = {}
        for (filename, results) in self._results():
            final_results[filename] = results

        for checker in self.checkers:
            filename = checker.filename
            results = sorted(final_results.get(filename, []),
                             key=lambda tup: (tup[1], tup[2]))
            for (error_code, line_number, column, text, line) in results:
                style_guide.handle_error(
                    code=error_code,
                    filename=filename,
                    line_number=line_number,
                    column_number=column,
                    text=text,
                    physical_line=line,
                )

    def _report_after_serial(self):
        style_guide = self.style_guide
        for checker in self.checkers:
            results = sorted(checker.results, key=lambda tup: (tup[2], tup[3]))
            filename = checker.filename
            for (error_code, line_number, column, text, line) in results:
                style_guide.handle_error(
                    code=error_code,
                    filename=filename,
                    line_number=line_number,
                    column_number=column,
                    text=text,
                    physical_line=line,
                )

    def _run_checks_from_queue(self):
        LOG.info('Running checks in parallel')
        for checker in iter(self.process_queue.get, 'DONE'):
            LOG.debug('Running checker for file "%s"', checker.filename)
            checker.run_checks(self.results_queue)
        self.results_queue.put('DONE')

    def is_path_excluded(self, path):
        # type: (str) -> bool
        """Check if a path is excluded.

        :param str path:
            Path to check against the exclude patterns.
        :returns:
            True if there are exclude patterns and the path matches,
            otherwise False.
        :rtype:
            bool
        """
        exclude = self.options.exclude
        if not exclude:
            return False
        basename = os.path.basename(path)
        if utils.fnmatch(basename, exclude):
            LOG.info('"%s" has been excluded', basename)
            return True

        absolute_path = os.path.abspath(path)
        match = utils.fnmatch(absolute_path, exclude)
        LOG.info('"%s" has %sbeen excluded', absolute_path,
                 '' if match else 'not ')
        return match

    def make_checkers(self, paths=None):
        # type: (List[str]) -> NoneType
        """Create checkers for each file."""
        if paths is None:
            paths = self.arguments
        filename_patterns = self.options.filename
        self.checkers = [
            FileChecker(filename, self.checks, self.style_guide)
            for argument in paths
            for filename in utils.filenames_from(argument,
                                                 self.is_path_excluded)
            if utils.fnmatch(filename, filename_patterns)
        ]

    def report(self):
        """Report all of the errors found in the managed file checkers.

        This iterates over each of the checkers and reports the errors sorted
        by line number.
        """
        if not self.using_multiprocessing:
            self._report_after_serial()

    def run_parallel(self):
        """Run the checkers in parallel."""
        # NOTE(sigmavirus24): Initialize Queues here to handle serial retries
        # in one place.
        self.process_queue = multiprocessing.Queue()
        self.results_queue = multiprocessing.Queue()

        LOG.info('Starting %d process workers', self.jobs - 1)
        for i in range(self.jobs - 1):
            proc = multiprocessing.Process(
                target=self._run_checks_from_queue
            )
            proc.daemon = True
            proc.start()
            self.processes.append(proc)
        proc = multiprocessing.Process(target=self._report_after_parallel)
        proc.start()
        LOG.info('Started process to report errors')
        self.processes.append(proc)

    def run_serial(self):
        """Run the checkers in serial."""
        for checker in self.checkers:
            checker.run_checks(self.results_queue)

    def run(self):
        """Run all the checkers.

        This will intelligently decide whether to run the checks in parallel
        or whether to run them in serial.

        If running the checks in parallel causes a problem (e.g.,
        https://gitlab.com/pycqa/flake8/issues/74) this also implements
        fallback to serial processing.
        """
        try:
            if self.using_multiprocessing:
                self.run_parallel()
            else:
                self.run_serial()
        except OSError as oserr:
            if oserr.errno not in SERIAL_RETRY_ERRNOS:
                LOG.exception(oserr)
                raise
            LOG.warning('Running in serial after OS exception, %r', oserr)
            self.run_serial()

    def start(self):
        """Start checking files."""
        LOG.info('Making checkers')
        self.make_checkers()
        if not self.using_multiprocessing:
            return

        LOG.info('Populating process queue')
        for checker in self.checkers:
            self.process_queue.put(checker)

    def stop(self):
        """Stop checking files."""
        for i in range(self.jobs):
            self.process_queue.put('DONE')

        for proc in self.processes:
            proc.join()


class FileChecker(object):
    """Manage running checks for a file and aggregate the results."""

    def __init__(self, filename, checks, style_guide):
        """Initialize our file checker.

        :param str filename:
            Name of the file to check.
        :param checks:
            The plugins registered to check the file.
        :type checks:
            flake8.plugins.manager.Checkers
        """
        self.filename = filename
        self.checks = checks
        self.style_guide = style_guide
        self.results = []
        self.processor = self._make_processor()

    def _make_processor(self):
        try:
            return processor.FileProcessor(self.filename,
                                           self.style_guide.options)
        except IOError:
            # If we can not read the file due to an IOError (e.g., the file
            # does not exist or we do not have the permissions to open it)
            # then we need to format that exception for the user.
            # NOTE(sigmavirus24): Historically, pep8 has always reported this
            # as an E902. We probably *want* a better error code for this
            # going forward.
            (exc_type, exception) = sys.exc_info()[:2]
            message = '{0}: {1}'.format(exc_type.__name__, exception)
            self.report('E902', 0, 0, message)
            return None

    def report(self, error_code, line_number, column, text):
        # type: (str, int, int, str) -> str
        """Report an error by storing it in the results list."""
        if error_code is None:
            error_code, text = text.split(' ', 1)
        physical_line = self.processor.line_for(line_number)
        error = (error_code, line_number, column, text, physical_line)
        self.results.append(error)
        return error_code

    def run_check(self, plugin, **arguments):
        """Run the check in a single plugin."""
        LOG.debug('Running %r with %r', plugin, arguments)
        self.processor.keyword_arguments_for(plugin.parameters, arguments)
        return plugin.execute(**arguments)

    def run_ast_checks(self):
        """Run all checks expecting an abstract syntax tree."""
        try:
            ast = self.processor.build_ast()
        except (ValueError, SyntaxError, TypeError):
            (exc_type, exception) = sys.exc_info()[:2]
            if len(exception.args) > 1:
                offset = exception.args[1]
                if len(offset) > 2:
                    offset = offset[1:3]
            else:
                offset = (1, 0)

            self.report('E999', offset[0], offset[1], '%s: %s' %
                        (exc_type.__name__, exception.args[0]))
            return

        for plugin in self.checks.ast_plugins:
            checker = self.run_check(plugin, tree=ast)
            # NOTE(sigmavirus24): If we want to allow for AST plugins that are
            # not classes exclusively, we can do the following:
            # retrieve_results = getattr(checker, 'run', lambda: checker)
            # Otherwise, we just call run on the checker
            for (line_number, offset, text, check) in checker.run():
                self.report(
                    error_code=None,
                    line_number=line_number,
                    column=offset,
                    text=text,
                )

    def run_logical_checks(self):
        """Run all checks expecting a logical line."""
        comments, logical_line, mapping = self.processor.build_logical_line()
        if not mapping:
            return
        self.processor.update_state(mapping)

        LOG.debug('Logical line: "%s"', logical_line.rstrip())

        for plugin in self.checks.logical_line_plugins:
            self.processor.update_checker_state_for(plugin)
            results = self.run_check(plugin, logical_line=logical_line) or ()
            for offset, text in results:
                offset = find_offset(offset, mapping)
                line_number, column_offset = offset
                self.report(
                    error_code=None,
                    line_number=line_number,
                    column=column_offset,
                    text=text,
                )

        self.processor.next_logical_line()

    def run_physical_checks(self, physical_line):
        """Run all checks for a given physical line."""
        for plugin in self.checks.physical_line_plugins:
            self.processor.update_checker_state_for(plugin)
            result = self.run_check(plugin, physical_line=physical_line)
            if result is not None:
                column_offset, text = result
                error_code = self.report(
                    error_code=None,
                    line_number=self.processor.line_number,
                    column=column_offset,
                    text=text,
                )

                self.processor.check_physical_error(error_code, physical_line)

    def process_tokens(self):
        """Process tokens and trigger checks.

        This can raise a :class:`flake8.exceptions.InvalidSyntax` exception.
        Instead of using this directly, you should use
        :meth:`flake8.checker.FileChecker.run_checks`.
        """
        parens = 0
        file_processor = self.processor
        for token in file_processor.generate_tokens():
            self.check_physical_eol(token)
            token_type, text = token[0:2]
            processor.log_token(LOG, token)
            if token_type == tokenize.OP:
                parens = processor.count_parentheses(parens, text)
            elif parens == 0:
                if processor.token_is_newline(token):
                    self.handle_newline(token_type)
                elif (processor.token_is_comment(token) and
                        len(file_processor.tokens) == 1):
                    self.handle_comment(token, text)

        if file_processor.tokens:
            # If any tokens are left over, process them
            self.run_physical_checks(file_processor.lines[-1])
            self.run_logical_checks()

    def run_checks(self, results_queue):
        """Run checks against the file."""
        if self.processor.should_ignore_file():
            return

        try:
            self.process_tokens()
        except exceptions.InvalidSyntax as exc:
            self.report(exc.error_code, exc.line_number, exc.column_number,
                        exc.error_message)

        self.run_ast_checks()

        if results_queue is not None:
            results_queue.put((self.filename, self.results))

    def handle_comment(self, token, token_text):
        """Handle the logic when encountering a comment token."""
        # The comment also ends a physical line
        token = list(token)
        token[1] = token_text.rstrip('\r\n')
        token[3] = (token[2][0], token[2][1] + len(token[1]))
        self.processor.tokens = [tuple(token)]
        self.run_logical_checks()

    def handle_newline(self, token_type):
        """Handle the logic when encountering a newline token."""
        if token_type == tokenize.NEWLINE:
            self.run_logical_checks()
            self.processor.reset_blank_before()
        elif len(self.processor.tokens) == 1:
            # The physical line contains only this token.
            self.processor.visited_new_blank_line()
            self.processor.delete_first_token()
        else:
            self.run_logical_checks()

    def check_physical_eol(self, token):
        """Run physical checks if and only if it is at the end of the line."""
        if processor.is_eol_token(token):
            # Obviously, a newline token ends a single physical line.
            self.run_physical_checks(token[4])
        elif processor.is_multiline_string(token):
            # Less obviously, a string that contains newlines is a
            # multiline string, either triple-quoted or with internal
            # newlines backslash-escaped. Check every physical line in the
            # string *except* for the last one: its newline is outside of
            # the multiline string, so we consider it a regular physical
            # line, and will check it like any other physical line.
            #
            # Subtleties:
            # - have to wind self.line_number back because initially it
            #   points to the last line of the string, and we want
            #   check_physical() to give accurate feedback
            line_no = token[2][0]
            with self.processor.inside_multiline(line_number=line_no):
                for line in self.processor.split_line(token):
                    self.run_physical_checks(line + '\n')


def find_offset(offset, mapping):
    """Find the offset tuple for a single offset."""
    if isinstance(offset, tuple):
        return offset

    for token_offset, position in mapping:
        if offset <= token_offset:
            break
    return (position[0], position[1] + offset - token_offset)
