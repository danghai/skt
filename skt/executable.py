# Copyright (c) 2017 Red Hat, Inc. All rights reserved. This copyrighted
# material is made available to anyone wishing to use, modify, copy, or
# redistribute it subject to the terms and conditions of the GNU General
# Public License v.2 or later.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

from __future__ import print_function
import ConfigParser
import argparse
import ast
import datetime
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import junit_xml

import skt
import skt.publisher
import skt.reporter
import skt.runner
from skt.kernelbuilder import KernelBuilder, CommandTimeoutError, ParsingError, verify_target
from skt.kerneltree import KernelTree, PatchApplicationError
from skt.misc import SKT_SUCCESS, SKT_FAIL, SKT_ERROR

DEFAULTRC = "~/.sktrc"
LOGGER = logging.getLogger()
retcode = SKT_SUCCESS


def full_path(path):
    """Get an absolute path to a file."""
    return os.path.abspath(os.path.expanduser(path))


def report_results(result_path, result_string, report_path, report_string):
    """
    Write report and results to the machine and human-readable forms.

    Args:
        result_path:    A full path of result log.
        result_string:  The content of string result.
        report_path:    A full path of report log.
        report_string:  The content of string report.
    """
    with os.fdopen(os.open(result_path, os.O_CREAT | os.O_WRONLY),
                   'w') as result_file:
        result_file.write(result_string)
    with os.fdopen(os.open(report_path, os.O_CREAT | os.O_WRONLY),
                   'w') as report_file:
        report_file.write(report_string)


def save_state(cfg, state):
    """
    Merge state to cfg, and then save cfg.

    Args:
        cfg:    A dictionary of skt configuration.
        state:  A dictionary of skt current state.
    """

    for (key, val) in state.iteritems():
        cfg[key] = val

    if not cfg.get('state'):
        return

    config = cfg.get('_parser')
    if not config.has_section("state"):
        config.add_section("state")

    for (key, val) in state.iteritems():
        if val is not None:
            logging.debug("state: %s -> %s", key, val)
            config.set('state', key, val)

    with open(cfg.get('rc'), 'w') as fileh:
        config.write(fileh)


def junit(func):
    """
    Create a function accepting a configuration object and passing it to
    the specified function, putting the call results into a JUnit test case,
    if configuration has JUnit result directory specified, simply calling the
    function otherwise.

    The generated test case is named "skt.<function-name>". The case stdout is
    set to JSON representation of the configuration object after the function
    call has completed. The created test case is appended to the "_testcases"
    list in the configuration object after that. Sets the global "retcode" to
    SKT_SUCCESS in case of test success, SKT_FAIL in case of test failure, and
    SKT_ERROR and above in case of infrastructure failure or skt problem (eg.
    Beaker server is unreachable).

    Args:
        func:   The function to call in the created function. Must accept
                a configuration object as the argument. Return value would be
                ignored. Can set the global "retcode" to indicate success
                (SKT_SUCCESS), test failure (SKT_FAIL) or infrastructure
                failure (SKT_ERROR and above).

    Return:
        The created function.
    """
    def wrapper(cfg):
        global retcode
        if cfg.get('junit'):
            tstart = time.time()
            testcase = junit_xml.TestCase(func.__name__, classname="skt")

            try:
                func(cfg)
            except Exception:
                logging.error("Unexpected exception caught, probably an "
                              "infrastructure failure or skt bug: %s",
                              traceback.format_exc())
                testcase.add_error_info(traceback.format_exc())
                retcode = SKT_ERROR

            if retcode == SKT_FAIL:
                # Tests failed
                testcase.add_failure_info("Step finished with retcode: %d" %
                                          retcode)
            elif retcode >= SKT_ERROR:
                testcase.add_error_info(
                    "Infrastructure issue or skt bug detected, retcode: %d" %
                    retcode
                )

            testcase.stdout = json.dumps(cfg, default=str)
            testcase.elapsed_sec = time.time() - tstart
            cfg['_testcases'].append(testcase)
        else:
            func(cfg)
    return wrapper


@junit
def cmd_merge(cfg):
    """
    Fetch a kernel repository, checkout particular references, and optionally
    apply patches from patchwork instances.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    global retcode
    utypes = []
    idx = 0
    ktree = KernelTree(
        cfg.get('baserepo'),
        ref=cfg.get('ref'),
        wdir=cfg.get('workdir'),
        fetch_depth=cfg.get('fetch_depth')
    )
    bhead = ktree.checkout()
    commitdate = ktree.get_commit_date(bhead)
    save_state(cfg, {'baserepo': cfg.get('baserepo'),
                     'basehead': bhead,
                     'commitdate': commitdate})

    # Remove existing results from previous runs (if any)
    try:
        for filename in os.listdir(cfg.get('output_dir')):
            if filename.startswith('merge.'):
                os.unlink(skt.join_with_slash(cfg.get('output_dir'), filename))
    except OSError:
        pass

    report_string = '\n'.join(['We cloned the git tree and checked out %s '
                               'from the repository at' % bhead[:12],
                               '  %s' % cfg.get('baserepo')])
    merge_result_path = skt.join_with_slash(cfg.get('output_dir'),
                                            'merge.result')
    merge_report_path = skt.join_with_slash(cfg.get('output_dir'),
                                            'merge.report')

    try:
        if cfg.get('merge_ref'):
            report_string = '{}\n{}'.format(
                report_string,
                'We merged the following references into the tree:'
            )
            for mb in cfg.get('merge_ref'):
                save_state(cfg, {'mergerepo_%02d' % idx: mb[0],
                                 'mergehead_%02d' % idx: bhead})
                (retcode, bhead) = ktree.merge_git_ref(*mb)

                utypes.append("[git]")
                if retcode:
                    if retcode != SKT_ERROR:
                        report_results(merge_result_path, 'false',
                                       merge_report_path, report_string)

                    return

                report_string += '  - %s\n    into commit %s' % (mb[0],
                                                                 bhead[:12])
                idx += 1

        elif cfg.get('patch') or cfg.get('pw'):
            report_string = 'We applied the following patch(es):\n'

            if cfg.get('patch'):
                utypes.append("[local patch]")
                for patch in cfg.get('patch'):
                    patch = os.path.abspath(patch)
                    save_state(cfg, {'localpatch_%02d' % idx: patch})
                    report_string += '  - %s\n' % patch

                    ktree.merge_patch_file(patch)
                    idx += 1

            elif cfg.get('pw'):
                utypes.append("[patchwork]")
                for patch in cfg.get('pw'):
                    save_state(cfg, {'patchwork_%02d' % idx: patch})
                    report_string += '  - %s,\n' % skt.get_patch_name(
                        skt.get_patch_mbox(patch)
                    )
                    report_string += '    grabbed from %s\n' % patch

                    ktree.merge_patchwork_patch(patch)
                    idx += 1

            report_string += '\n'.join([
                'on top of commit %s from the repository at' % bhead[:12],
                '  %s' % cfg.get('baserepo')
            ])
    except PatchApplicationError as patch_exc:
        retcode = SKT_FAIL
        logging.error(patch_exc)
        save_state(cfg, {'mergelog': ktree.mergelog})

        report_string += '\n'.join([
            '\nHowever, the application of the last patch above failed with'
            ' the',
            'following output:\n\n'
        ])
        with open(ktree.mergelog, 'r') as mergelog:
            for line in mergelog:
                # Skip the useless part of the 'git am' output
                if ("The copy of the patch" in line) \
                        or ('see the failed patch' in line):
                    break
                report_string += '    %s\n' % line.strip()

        report_string += '\n'.join([
            '\nPlease note that if there are subsequent patches in the series,'
            ' they weren\'t',
            'applied because of the error message stated above.\n'
        ])
        report_results(merge_result_path, 'false',
                       merge_report_path, report_string)

        return
    except Exception:
        (exc, exc_type, trace) = sys.exc_info()
        raise exc, exc_type, trace

    uid = "[baseline]"
    if utypes:
        uid = " ".join(utypes)

    kpath = ktree.getpath()
    buildhead = ktree.get_commit_hash()

    save_state(cfg, {'workdir': kpath,
                     'buildhead': buildhead,
                     'uid': uid})

    report_results(merge_result_path, 'true',
                   merge_report_path, report_string)


@junit
def cmd_build(cfg):
    """
    Build the kernel with specified configuration and put it into a tarball.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    global retcode
    tstamp = datetime.datetime.strftime(datetime.datetime.now(),
                                        "%Y%m%d%H%M%S")

    # Remove existing results from previous runs (if any)
    try:
        for filename in os.listdir(cfg.get('output_dir')):
            if filename.startswith('build.'):
                os.unlink(skt.join_with_slash(cfg.get('output_dir'), filename))
    except OSError:
        pass

    build_result_path = skt.join_with_slash(cfg.get('output_dir'),
                                            'build.result')
    build_report_path = skt.join_with_slash(cfg.get('output_dir'),
                                            'build.report')

    tgz = None
    builder = KernelBuilder(
        source_dir=cfg.get('workdir'),
        basecfg=cfg.get('baseconfig'),
        cfgtype=cfg.get('cfgtype'),
        extra_make_args=cfg.get('makeopts'),
        enable_debuginfo=cfg.get('enable_debuginfo'),
        target=cfg.get('target')
    )

    # Clean the kernel source with 'make mrproper' if requested.
    if cfg.get('wipe'):
        builder.clean_kernel_source()

    try:
        tgz = builder.mktgz()
    except (CommandTimeoutError, subprocess.CalledProcessError, ParsingError,
            IOError) as exc:
        logging.error(exc)
        save_state(cfg, {'buildlog': builder.buildlog})
        retcode = SKT_FAIL
    except Exception:
        (exc, exc_type, trace) = sys.exc_info()
        raise exc, exc_type, trace

    if tgz:
        if cfg.get('buildhead'):
            ttgz = "%s.tar.gz" % cfg.get('buildhead')
        else:
            ttgz = addtstamp(tgz, tstamp)
        shutil.move(tgz, ttgz)
        logging.info("tarball path: %s", ttgz)
        save_state(cfg, {'tarpkg': ttgz})

    tconfig = '%s.csv.config' % cfg.get('buildhead')
    shutil.copyfile(builder.get_cfgpath(), tconfig)

    krelease = builder.getrelease()
    kernel_arch = builder.build_arch

    save_state(cfg, {'buildconf': tconfig,
                     'krelease': krelease,
                     'kernel_arch': kernel_arch})

    report_string = '{} ({{{}}})'.format(
        'The kernel was built with the attached configuration',
        tconfig
    )
    if retcode:
        report_string += '\n'.join([
            '\nHowever, the build failed. We are attaching the build output '
            'for',
            'more information ({{{}}})'.format(
                os.path.basename(builder.buildlog)
            )
        ])

    report_results(build_result_path, 'true' if not retcode else 'false',
                   build_report_path, report_string)


@junit
def cmd_publish(cfg):
    """
    Publish (copy) the kernel tarball and configuration to the specified
    location, generating their resulting URLs, using the specified "publisher".
    Only "cp" and "scp" pusblishers are supported at the moment.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    publisher = skt.publisher.getpublisher(*cfg.get('publisher'))

    if not cfg.get('tarpkg'):
        raise Exception("skt publish is missing \"--tarpkg <path>\" option")

    cfgurl = None

    url = publisher.publish(cfg.get('tarpkg'))
    logging.info("published url: %s", url)

    if cfg.get('buildconf'):
        cfgurl = publisher.publish(cfg.get('buildconf'))

    save_state(cfg, {'buildurl': url,
                     'cfgurl': cfgurl})


@junit
def cmd_run(cfg):
    """
    Run tests on a built kernel using the specified "runner". Only "Beaker"
    runner is currently supported.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    global retcode

    # Remove existing results from previous runs (if any)
    try:
        for filename in os.listdir(cfg.get('output_dir')):
            if filename.startswith('run.'):
                os.unlink(skt.join_with_slash(cfg.get('output_dir'), filename))
    except OSError:
        pass

    report_string = ''
    run_result_path = skt.join_with_slash(cfg.get('output_dir'), 'run.result')
    run_report_path = skt.join_with_slash(cfg.get('output_dir'), 'run.report')

    runner = skt.runner.getrunner(*cfg.get('runner'))
    retcode = runner.run(cfg.get('buildurl'), cfg.get('krelease'),
                         cfg.get('wait'), uid=cfg.get('uid'),
                         arch=cfg.get("kernel_arch"))

    idx = 0
    for job in runner.jobs:
        if cfg.get('wait') and cfg.get('junit'):
            try:
                runner.dumpjunitresults(job, cfg.get('junit'))
            except Exception as exc:
                logging.error(exc)
                retcode = SKT_ERROR
        save_state(cfg, {'jobid_%s' % (idx): job})
        idx += 1

    cfg['jobs'] = runner.jobs

    save_state(cfg, {'retcode': retcode})

    if retcode != SKT_ERROR:
        report_results(run_result_path, 'true' if retcode else 'false',
                       run_report_path, report_string)


def cmd_report(cfg):
    """
    Report build and/or test results using the specified "reporter". Currently
    results can be reported by e-mail or printed to stdout.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    if not cfg.get("reporter"):
        return

    # Attempt to import the reporter provided by the user
    try:
        module = importlib.import_module('skt.reporter')
        class_name = "{}Reporter".format(cfg['reporter']['type'].title())
        reporter_class = getattr(module, class_name)
    except AttributeError:
        sys.exit(
            "Unable to find specified reporter type: {}".format(class_name)
        )

    # FIXME We are passing the entire cfg object to the reporter class but
    # we should be passing the specific options that are needed.
    # Create a report
    reporter = reporter_class(cfg)
    reporter.report()


def cmd_cleanup(cfg):
    """
    Remove the build information file, kernel tarball. Remove state information
    from the configuration file, if saving state was enabled with the global
    --state option, and remove the whole working directory, if the global
    --wipe option was specified.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    config = cfg.get('_parser')
    if config.has_section('state'):
        config.remove_section('state')
        with open(cfg.get('rc'), 'w') as fileh:
            config.write(fileh)

    if cfg.get('tarpkg'):
        try:
            os.unlink(cfg.get('tarpkg'))
        except OSError:
            pass

    if cfg.get('wipe') and cfg.get('workdir'):
        shutil.rmtree(cfg.get('workdir'))


def cmd_all(cfg):
    """
    Run the following commands in order: merge, build, publish, run, report (if
    --wait option was specified), and cleanup.

    Args:
        cfg:    A dictionary of skt configuration.
    """
    cmd_merge(cfg)
    cmd_build(cfg)
    cmd_publish(cfg)
    cmd_run(cfg)
    if cfg.get('wait'):
        cmd_report(cfg)
    cmd_cleanup(cfg)


def addtstamp(path, tstamp):
    """
    Add time stamp to a file path.

    Args:
        path:   file path.
        tstamp: time stamp.

    Returns:
        New path with time stamp.
    """
    return skt.join_with_slash(os.path.dirname(path),
                               "%s-%s" % (tstamp, os.path.basename(path)))


def setup_logging(verbose):
    """
    Setup the root logger.

    Args:
        verbose:    Verbosity level to setup log message filtering.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)8s   %(message)s")
    LOGGER.setLevel(logging.WARNING - (verbose * 10))
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def setup_parser():
    """
    Create an skt command line parser.

    Returns:
        The created parser.
    """
    parser = argparse.ArgumentParser()

    # These arguments apply to all commands within skt
    parser.add_argument(
        "-d",
        "--workdir",
        type=str,
        help="Path to work dir"
    )
    parser.add_argument(
        "-w",
        "--wipe",
        help=(
            "Clean build (make mrproper before building) and remove workdir "
            "when finished"
        ),
        action="store_true",
        default=False
    )
    parser.add_argument(
        "--junit",
        help="Directory for storing junit XML results"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Increase verbosity level",
        action="count",
        default=0
    )
    parser.add_argument(
        "--rc",
        help="Path to rc file",
        default=DEFAULTRC
    )
    # FIXME Storing state in config file can break the whole system in case
    #       state saving aborts. It's better to save state separately.
    #       It also breaks separation of concerns, as in principle skt doesn't
    #       need to modify its own configuration otherwise.
    parser.add_argument(
        "--state",
        help=(
            "Save/read state from 'state' section of rc file"
        ),
        action="store_true",
        default=False
    )
    parser.add_argument(
        "--target",
        type=str,
        help="Set the target to execute as",
        default=None
    )

    subparsers = parser.add_subparsers()

    # These arguments apply to the 'merge' skt subcommand
    parser_merge = subparsers.add_parser("merge", add_help=False)
    parser_merge.add_argument(
        "-b",
        "--baserepo",
        type=str,
        help="Base repo URL"
    )
    parser_merge.add_argument(
        "--ref",
        type=str,
        help="Base repo ref to which patches are applied (default: master)"
    )
    parser_merge.add_argument(
        "--patch",
        type=str,
        action='append',
        help="Path to a local patch to apply "
             + "(use multiple times for multiple patches)"
    )
    parser_merge.add_argument(
        "--pw",
        type=str,
        action='append',
        help="URL to Patchwork patch to apply "
             + "(use multiple times for multiple patches)"
    )
    parser_merge.add_argument(
        "-m",
        "--merge-ref",
        action='append',
        help="Merge ref format: 'url [ref]' "
             + "(use multiple times for multiple merge refs)"
    )
    parser_merge.add_argument(
        "--fetch-depth",
        type=str,
        help=(
            "Create a shallow clone with a history truncated to the "
            "specified number of commits."
        ),
        default=None
    )

    # These arguments apply to the 'build' skt command
    parser_build = subparsers.add_parser("build", add_help=False)
    parser_build.add_argument(
        "-c",
        "--baseconfig",
        type=str,
        help="Path to kernel config to use"
    )
    parser_build.add_argument(
        "--cfgtype",
        type=str,
        help="How to process default config (default: olddefconfig)"
    )
    parser_build.add_argument(
        "--enable-debuginfo",
        type=bool,
        default=False,
        help="Build kernel with debuginfo (default: disabled)"
    )
    parser_build.add_argument(
        "--makeopts",
        type=str,
        help="Additional options to pass to make"
    )
    parser_build.add_argument(
        "--rh-configs-glob",
        type=str,
        help=(
            "Glob pattern to use when choosing the correct kernel config "
            "(required if '--cfgtype rh-configs' is used)"
        )
    )

    # These arguments apply to the 'publish' skt command
    parser_publish = subparsers.add_parser("publish", add_help=False)
    parser_publish.add_argument(
        "-p",
        "--publisher",
        type=str,
        nargs=3,
        help="Publisher config string in 'type destination baseurl' format"
    )
    parser_publish.add_argument(
        "--tarpkg",
        type=str,
        help="Path to tar pkg to publish"
    )

    # These arguments apply to the 'run' skt command
    parser_run = subparsers.add_parser("run", add_help=False)
    parser_run.add_argument(
        "-r",
        "--runner",
        nargs=2,
        type=str,
        help="Runner config in 'type \"{'key' : 'val', ...}\"' format"
    )
    parser_run.add_argument(
        "--buildurl",
        type=str,
        help="Build tarpkg url"
    )
    parser_run.add_argument(
        "--krelease",
        type=str,
        help="Kernel release version of the build"
    )
    parser_run.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Do not exit until tests are finished"
    )

    # These arguments apply to the 'report' skt subcommand
    parser_report = subparsers.add_parser("report", add_help=False)
    parser_report.add_argument(
        "--reporter",
        dest="type",
        type=str,
        choices=('stdio', 'mail'),
        help="Reporter to use"
    )
    parser_report.add_argument(
        "--mail-to",
        action='append',
        type=str,
        help="Report recipient's email address"
    )
    parser_report.add_argument(
        "--mail-from",
        type=str,
        help="Report sender's email address"
    )
    parser_report.add_argument(
        "--mail-subject",
        type=str,
        help="Subject of the report email"
    )
    parser_report.add_argument(
        "--mail-header",
        action='append',
        default=[],
        type=str,
        help=(
            "Extra headers for the report email - example: "
            "\"In-Reply-To: <messageid@example.com>\""
        )
    )
    parser_report.add_argument(
        '--result',
        action='append',
        type=str,
        help='Path to a state file to include in the report'
    )
    parser_report.add_argument(
        "--smtp-url",
        type=str,
        help='Use smtp url instead of localhost to send mail',
    )
    parser_report.set_defaults(func=cmd_report)
    parser_report.set_defaults(_name="report")

    parser_cleanup = subparsers.add_parser("cleanup", add_help=False)

    parser_all = subparsers.add_parser(
        "all",
        parents=[
            parser_merge,
            parser_build,
            parser_publish,
            parser_run,
            parser_report,
            parser_cleanup
        ]
    )

    parser_merge.add_argument(
        "-h",
        "--help",
        help="Merge sub-command help",
        action="help"
    )
    parser_build.add_argument(
        "-h",
        "--help",
        help="Build sub-command help",
        action="help"
    )
    parser_publish.add_argument(
        "-h",
        "--help",
        action="help",
        help="Publish sub-command help"
    )
    parser_run.add_argument(
        "-h",
        "--help",
        help="Run sub-command help",
        action="help"
    )
    parser_report.add_argument(
        "-h",
        "--help",
        help="Report sub-command help",
        action="help"
    )

    parser_merge.set_defaults(func=cmd_merge)
    parser_merge.set_defaults(_name="merge")
    parser_build.set_defaults(func=cmd_build)
    parser_build.set_defaults(_name="build")
    parser_publish.set_defaults(func=cmd_publish)
    parser_publish.set_defaults(_name="publish")
    parser_run.set_defaults(func=cmd_run)
    parser_run.set_defaults(_name="run")
    parser_cleanup.set_defaults(func=cmd_cleanup)
    parser_cleanup.set_defaults(_name="cleanup")
    parser_all.set_defaults(func=cmd_all)
    parser_all.set_defaults(_name="all")

    return parser


def load_config(args):
    """
    Load skt configuration from the command line and the configuration file.

    Args:
        args:   Parsed command-line configuration, including the path to the
                configuration file.

    Returns:
        Loaded configuration dictionary.
    """
    # NOTE(mhayden): The shell should do any tilde expansions on the path
    # before the rc path is provided to Python.
    config = ConfigParser.ConfigParser()
    config.read(os.path.abspath(args.rc))

    cfg = vars(args)
    cfg['_parser'] = config
    cfg['_testcases'] = []

    # Read 'state' section first so that it is not overwritten by 'config'
    # section values.
    if cfg.get('state') and config.has_section('state'):
        for (name, value) in config.items('state'):
            if not cfg.get(name):
                if name.startswith("jobid_"):
                    if "jobs" not in cfg:
                        cfg["jobs"] = set()
                    cfg["jobs"].add(value)
                elif name.startswith("mergerepo_"):
                    if "mergerepos" not in cfg:
                        cfg["mergerepos"] = list()
                    cfg["mergerepos"].append(value)
                elif name.startswith("mergehead_"):
                    if "mergeheads" not in cfg:
                        cfg["mergeheads"] = list()
                    cfg["mergeheads"].append(value)
                elif name.startswith("localpatch_"):
                    if "localpatches" not in cfg:
                        cfg["localpatches"] = list()
                    cfg["localpatches"].append(value)
                elif name.startswith("patchwork_"):
                    if "patchworks" not in cfg:
                        cfg["patchworks"] = list()
                    cfg["patchworks"].append(value)
                cfg[name] = value

    if config.has_section('config'):
        for (name, value) in config.items('config'):
            if not cfg.get(name):
                cfg[name] = value

    if config.has_section('publisher') and not cfg.get('publisher'):
        cfg['publisher'] = [config.get('publisher', 'type'),
                            config.get('publisher', 'destination'),
                            config.get('publisher', 'baseurl')]

    if config.has_section('runner') and not cfg.get('runner'):
        rcfg = {}
        for (key, val) in config.items('runner'):
            if key != 'type':
                rcfg[key] = val
        cfg['runner'] = [config.get('runner', 'type'), rcfg]
    elif cfg.get('runner'):
        cfg['runner'] = [cfg.get('runner')[0],
                         ast.literal_eval(cfg.get('runner')[1])]

    # Check if the reporter type is set on the command line
    if cfg.get('_name') == 'report' and cfg.get('type'):
        # Use the reporter configuration from the command line
        cfg['reporter'] = {
            'type': cfg.get('type'),
            'mail_to': cfg.get('mail_to'),
            'mail_from': cfg.get('mail_from'),
            'mail_subject': cfg.get('mail_subject'),
            'mail_header': cfg.get('mail_header')
        }
    elif config.has_section('reporter'):
        # Use the reporter configuration from the configuration file
        cfg['reporter'] = config.items('reporter')

    if not cfg.get('merge_ref'):
        cfg['merge_ref'] = []

    for section in config.sections():
        if section.startswith("merge-"):
            mdesc = [config.get(section, 'url')]
            if config.has_option(section, 'ref'):
                mdesc.append(config.get(section, 'ref'))
            cfg['merge_ref'].append(mdesc)

    (_, config_arch) = verify_target(cfg.get('target'))
    if config_arch is None:
        raise Exception("Unsupport target: %s" % cfg.get('target'))
    cfg['config_arch'] = config_arch

    # Get an absolute path for the work directory
    if cfg.get('workdir'):
        cfg['workdir'] = full_path(cfg.get('workdir'))
    else:
        cfg['workdir'] = tempfile.mkdtemp()

    # Get an absolute path for the kernel configuration file
    if cfg.get('basecfg'):
        cfg['basecfg'] = full_path(cfg.get('basecfg'))

    # Get an absolute path for the configuration file
    cfg['rc'] = full_path(cfg.get('rc'))

    # Get an absolute path for the buildconf
    if cfg.get('buildconf'):
        cfg['buildconf'] = full_path(cfg.get('buildconf'))

    # Get an absolute path for the tarpkg
    if cfg.get('tarpkg'):
        cfg['tarpkg'] = full_path(cfg.get('tarpkg'))

    # Get absolute paths to state files for multireport
    # Handle "result" being None if none are specified
    for idx, statefile_path in enumerate(cfg.get('result') or []):
        cfg['result'][idx] = full_path(statefile_path)

    if cfg.get('junit'):
        try:
            os.mkdir(full_path(cfg.get('junit')))
        except OSError:
            pass

    # Preparation for output directory option
    if os.access(cfg.get('workdir'), os.W_OK | os.X_OK):
        cfg['output_dir'] = cfg.get('workdir')
    else:
        cfg['output_dir'] = os.getcwd()

    return cfg


def check_args(parser, args):
    """Check the arguments provided to ensure all requirements are met.

    Args:
      parser - the parser object
      args   - the parsed arguments
    """
    # Users must specify a glob to match generated kernel config files when
    # building configs with `make rh-configs`
    if (args._name == 'build' and args.cfgtype == 'rh-configs'
            and not args.rh_configs_glob):
        parser.error("--cfgtype rh-configs requires --rh-configs-glob to set")

    # Don't allow a mix of --merge-ref, --patch and --pw options
    if args._name == 'merge':
        if (args.merge_ref and any([args.patch, args.pw])) \
                or (args.patch and args.pw):
            parser.error('--merge-ref, --patch and --pw options are mutually '
                         'exclusive!')

    # Check required arguments for 'report'
    if args._name == 'report':

        # MailReporter requires recipient and sender email addresses
        if (args.type == 'mail' and (not args.mail_to or not args.mail_from)):
            parser.error(
                "--reporter mail requires --mail-to and --mail-from to be set"
            )

        # No mail-related options should be provided for StdioReporter
        if (args.type == 'stdio'
                and any([getattr(args, x)
                         for x in dir(args) if x.startswith('mail')])):
            parser.error(
                'the stdio reporter was selected but arguments for the mail '
                'reporter were provided'
            )


def main():
    try:
        global retcode

        parser = setup_parser()
        args = parser.parse_args()
        check_args(parser, args)

        setup_logging(args.verbose)
        cfg = load_config(args)

        args.func(cfg)
        if cfg.get('junit'):
            testsuite = junit_xml.TestSuite("skt", cfg.get('_testcases'))
            with open("%s/%s.xml" % (cfg.get('junit'), args._name),
                      'w') as fileh:
                junit_xml.TestSuite.to_file(fileh, [testsuite])

        sys.exit(retcode)
    except KeyboardInterrupt:
        print("\nExited at user request.")
        sys.exit(130)
